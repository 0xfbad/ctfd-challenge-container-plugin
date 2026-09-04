from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypedDict, TypeVar, overload

import docker
import gevent.monkey
import gevent.threadpool
import paramiko
from docker import DockerClient
from docker.models.containers import Container
from docker.models.networks import Network

from .exceptions import ContainerUnavailableException
from .models import DockerContextModel
from .volume_policy import VolumeMetadata, docker_volume_metadata

logger = logging.getLogger(__name__)

LOCAL_CONTEXT_NAME = "local"
LOCAL_SOCKET_PATH = "/var/run/docker.sock"

DEFAULT_CLIENT_TIMEOUT = 10  # seconds of http read timeout for control plane calls
PULL_CLIENT_TIMEOUT = 300  # seconds, pulls can run for minutes
CLIENT_FAILURE_COOLDOWN = 60  # seconds a context stays negative cached after a failed client construction
SSH_CONNECT_TIMEOUT = 10  # seconds, fallback ssh connect bound when no per call value is set
THREADPOOL_SIZE = 4

_DockerRunVal = str | int | bool | list[str] | dict[str, str] | dict[str, dict[str, str]]

_T = TypeVar("_T")

_SSH_ADAPTER_PATCH_LOCK = threading.RLock()
_SSH_ADAPTER_PATCHED = False
_SSH_CONNECT_TIMEOUT = threading.local()


def _apply_ssh_connect_timeouts(params: dict[str, object], timeout: int | float) -> None:
    """the docker sdk timeout is not forwarded to SSHClient.connect
    without these a blackholed host occupies a context worker forever
    """
    bounded = max(1.0, float(timeout))
    params.update(timeout=bounded, banner_timeout=bounded, auth_timeout=bounded)


def _install_bounded_ssh_adapter() -> None:
    """process wide timeout fix for docker-py 7.x"""
    global _SSH_ADAPTER_PATCHED
    if _SSH_ADAPTER_PATCHED:
        return
    with _SSH_ADAPTER_PATCH_LOCK:
        if _SSH_ADAPTER_PATCHED:
            return
        try:
            from docker.api import client as api_client

            original_adapter = api_client.SSHHTTPAdapter
        except (ImportError, AttributeError):
            return  # unit tests stub docker without this module, the pinned dependency always has it

        # the sentinel name is shared with ctfd-remote-desktop so whichever plugin loads first wins
        if getattr(original_adapter, "_ctfd_bounded_connect", False):
            _SSH_ADAPTER_PATCHED = True
            return

        # the base is resolved at runtime so mypy cannot prove it is a class
        class BoundedSSHHTTPAdapter(original_adapter):  # type: ignore[misc, valid-type]
            _ctfd_bounded_connect = True
            _ctfd_ssh_connect_timeout = _SSH_CONNECT_TIMEOUT

            def _create_paramiko_client(self, base_url):
                super()._create_paramiko_client(base_url)
                # resolved off the class so a foreign plugin adapter keeps owning its own local
                timeout = getattr(getattr(type(self), "_ctfd_ssh_connect_timeout", None), "value", SSH_CONNECT_TIMEOUT)
                _apply_ssh_connect_timeouts(self.ssh_params, timeout)

        api_client.SSHHTTPAdapter = BoundedSSHHTTPAdapter
        _SSH_ADAPTER_PATCHED = True


def _ssh_timeout_local() -> threading.local:
    """the installed adapter may belong to another plugin, publish the per call timeout where it will read it"""
    try:
        from docker.api import client as api_client

        return getattr(api_client.SSHHTTPAdapter, "_ctfd_ssh_connect_timeout", _SSH_CONNECT_TIMEOUT)
    except (ImportError, AttributeError):
        return _SSH_CONNECT_TIMEOUT


def _new_docker_client(endpoint: str, timeout: int = DEFAULT_CLIENT_TIMEOUT) -> DockerClient:
    if not endpoint.startswith("ssh://"):
        return docker.DockerClient(base_url=endpoint, timeout=timeout)

    _install_bounded_ssh_adapter()
    local = _ssh_timeout_local()
    local.value = timeout
    try:
        return docker.DockerClient(base_url=endpoint, timeout=timeout)
    finally:
        try:
            del local.value
        except AttributeError:
            pass


def _confirm_removal_in_progress(container: Container, error: docker.errors.APIError) -> bool:
    """poll for the container to disappear, a 409 already in progress means docker auto remove owns it"""
    if getattr(error, "status_code", None) != 409:
        return False

    if "already in progress" not in str(getattr(error, "explanation", error)):
        return False

    for _attempt in range(20):
        try:
            container.reload()
        except docker.errors.NotFound:
            return True

        time.sleep(0.05)

    return False


def _run_with_port_retry(attempt: Callable[[int], _T], *, exhausted_message: str) -> _T:
    last_err: docker.errors.APIError | None = None
    for _ in range(50):
        host_port = random.randint(40000, 59999)
        try:
            return attempt(host_port)
        except docker.errors.APIError as e:
            if "port is already allocated" in str(e) or "address already in use" in str(e):
                last_err = e
                continue
            raise

    raise docker.errors.DockerException(f"{exhausted_message}: {last_err}")


class ImageInfo(TypedDict):
    id: str
    size_mb: int
    created: str


class DiscoveredContext(TypedDict):
    name: str
    endpoint: str


class ReconcileEntry(TypedDict):
    name: str
    id: str
    instance_id: str
    created_ts: float


_ContextMeta = dict[str, object]


@overload
def _scan_context_meta(context_name: str) -> _ContextMeta | None: ...


@overload
def _scan_context_meta(context_name: None = None) -> list[_ContextMeta]: ...


def _scan_context_meta(context_name: str | None = None) -> _ContextMeta | list[_ContextMeta] | None:
    # docker hashes context dir names by sha256 so there is no direct lookup by name
    contexts_dir = os.path.expanduser("~/.docker/contexts/meta")
    if not os.path.isdir(contexts_dir):
        return None if context_name else []

    results = []
    for entry in os.listdir(contexts_dir):
        meta_path = os.path.join(contexts_dir, entry, "meta.json")
        if not os.path.isfile(meta_path):
            continue

        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception:
            continue

        # valid json that is not an object must not break loading of the other contexts
        if not isinstance(meta, dict):
            continue

        if not context_name:
            results.append(meta)
            continue

        if meta.get("Name") == context_name:
            return meta

    return None if context_name else results


def _endpoint_from_context_meta(context_name: str) -> str | None:
    meta = _scan_context_meta(context_name)
    if not meta:
        return None

    endpoints = meta.get("Endpoints", {})
    if not isinstance(endpoints, dict):
        return None

    docker_ep = endpoints.get("docker", {})
    if not isinstance(docker_ep, dict):
        return None

    endpoint = docker_ep.get("Host")
    if not endpoint:
        return None

    return str(endpoint)


def _resolve_endpoint(context_name: str, hostname: str | None) -> str | None:
    endpoint = _endpoint_from_context_meta(context_name)
    if endpoint:
        return endpoint

    if hostname and "@" in hostname:
        return f"ssh://{hostname}"

    if hostname:
        return f"ssh://root@{hostname}"

    if context_name == LOCAL_CONTEXT_NAME and os.path.exists(LOCAL_SOCKET_PATH):
        return f"unix://{LOCAL_SOCKET_PATH}"

    return None


def discover_contexts() -> list[DiscoveredContext]:
    discovered: list[DiscoveredContext] = []
    for meta in _scan_context_meta():
        name = str(meta.get("Name", ""))
        endpoints = meta.get("Endpoints", {})
        docker_ep = endpoints.get("docker", {}) if isinstance(endpoints, dict) else {}
        endpoint = str(docker_ep.get("Host", "")) if isinstance(docker_ep, dict) else ""
        if not name:
            continue

        discovered.append({"name": name, "endpoint": endpoint})

    has_local = any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered)
    if not has_local and os.path.exists(LOCAL_SOCKET_PATH):
        discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def ping_endpoint(endpoint: str, timeout: int = 3) -> bool:
    client = None
    try:
        client = _new_docker_client(endpoint, timeout=timeout)
        client.ping()
        return True
    except Exception:
        return False
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


class DockerHostManager:
    def __init__(self) -> None:
        self._context_configs: dict[str, str] = {}
        # tracked apart from the configs so an unreachable host stays configured for health checks and cleanup
        self._connected_contexts: set[str] = set()
        self._pub_hostnames: dict[str, str | None] = {}
        # keyed by thread ident, paramiko channels bind their gevent events to the hub of the creating thread
        self._clients: dict[tuple[str, int], DockerClient] = {}
        self._config_generation: int = 0
        self._client_generation: int = -1
        # negative cache, a dead host costs one bounded connect per cooldown window instead of one per request
        self._client_failures: dict[str, float] = {}
        # reentrant so a wrapped op can re-enter the lock protected helpers without deadlocking
        self._lock: threading.RLock = threading.RLock()
        # one pool per context keeps paramiko blocking off the gevent hub, so a hung host cannot stall other requests
        self._threadpools: dict[str, gevent.threadpool.ThreadPool] = {}

    def _mark_connected(self, context_name: str) -> None:
        self._connected_contexts.add(context_name)
        self._client_failures.pop(context_name, None)

    def _mark_connect_failed(self, context_name: str) -> None:
        self._client_failures[context_name] = time.time()

    def _cooling_down(self, context_name: str) -> bool:
        return time.time() - self._client_failures.get(context_name, 0.0) < CLIENT_FAILURE_COOLDOWN

    def _get_threadpool(self, context_name: str) -> gevent.threadpool.ThreadPool:
        with self._lock:
            pool = self._threadpools.get(context_name)
            if pool is None:
                pool = gevent.threadpool.ThreadPool(maxsize=THREADPOOL_SIZE)
                self._threadpools[context_name] = pool
            return pool

    def _call(self, context_name: str, fn, *args, **kwargs):
        # gated in the calling greenlet so a dead host consumes no threadpool slot
        with self._lock:
            if self._cooling_down(context_name):
                raise ContainerUnavailableException(f"docker context '{context_name}' is unreachable")

        # without monkey patching there is no gevent hub and pool apply hangs in futex, flask cli hits this
        if not gevent.monkey.is_module_patched("threading"):
            return fn(*args, **kwargs)

        pool = self._get_threadpool(context_name)
        return pool.apply(fn, args=args, kwds=kwargs)

    def _get_client(self, context_name: str) -> DockerClient:
        tid = threading.get_ident()
        to_close: list[DockerClient] = []
        with self._lock:
            if self._client_generation != self._config_generation:
                to_close.extend(self._clients.values())
                self._clients = {}
                self._client_generation = self._config_generation
            else:
                live_idents = {t.ident for t in threading.enumerate()}
                dead_keys = [k for k in self._clients if k[1] not in live_idents]
                for k in dead_keys:
                    to_close.append(self._clients.pop(k))

            key = (context_name, tid)
            client = self._clients.get(key)
            if client is None:
                url = self._context_configs.get(context_name)
                if not url:
                    raise Exception(f"no client for context '{context_name}'")

                try:
                    client = _new_docker_client(url)
                except Exception:
                    self._mark_connect_failed(context_name)
                    raise
                self._mark_connected(context_name)
                self._clients[key] = client

        # close outside the lock, paramiko teardown can block on ssh for seconds
        for old in to_close:
            try:
                old.close()
            except Exception:
                pass

        return client

    def _clear_client(self, context_name: str) -> None:
        to_close: list[DockerClient] = []
        with self._lock:
            self._connected_contexts.discard(context_name)
            keys = [k for k in self._clients if k[0] == context_name]
            for k in keys:
                to_close.append(self._clients.pop(k))

        for old in to_close:
            try:
                old.close()
            except Exception:
                pass

    def _invoke_client_op(self, context_name, fn):
        try:
            result = fn()
            with self._lock:
                self._mark_connected(context_name)
            return result
        # NotFound and ImageNotFound subclass APIError, an escaped 404 means the daemon answered so keep the client
        except docker.errors.APIError:
            with self._lock:
                self._mark_connected(context_name)
            raise
        except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
            self._clear_client(context_name)
            raise
        # fn must handle its own KeyError, anything reaching here means the cached client is unusable
        except Exception:
            self._clear_client(context_name)
            raise ContainerUnavailableException(f"transient client failure on {context_name}")

    def _call_with_client_op(self, context_name, fn):
        return self._call(context_name, lambda: self._invoke_client_op(context_name, fn))

    def load_contexts(self, contexts: list[DockerContextModel]) -> None:
        """catalog only, no network io, connectivity is established by warm_up and ping"""
        new_configs = {}
        new_pub_hostnames = {}

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            new_configs[ctx.context_name] = endpoint
            new_pub_hostnames[ctx.context_name] = ctx.pub_hostname

        with self._lock:
            unchanged = {name for name, url in new_configs.items() if self._context_configs.get(name) == url}
            # bumping unconditionally would discard every cached client on each catalog refresh
            if new_configs != self._context_configs:
                self._config_generation += 1
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._connected_contexts &= unchanged
            self._client_failures = {name: at for name, at in self._client_failures.items() if name in unchanged}

    def warm_up(self) -> None:
        """serial bounded probes for contexts that are configured but not known reachable"""
        with self._lock:
            targets = [
                name
                for name in self._context_configs
                if name not in self._connected_contexts and not self._cooling_down(name)
            ]

        for name in targets:
            self.ping(name)

    def get_pub_hostname(self, context_name: str) -> str | None:
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self) -> list[str]:
        with self._lock:
            return sorted(self._connected_contexts)

    def get_configured_contexts(self) -> list[str]:
        with self._lock:
            return sorted(self._context_configs)

    def has_contexts(self) -> bool:
        """configured, not connected, reachability is tracked separately"""
        with self._lock:
            return bool(self._context_configs)

    def ping(self, context_name: str) -> bool:
        with self._lock:
            url = self._context_configs.get(context_name)

        if not url:
            return False

        # ephemeral client, a cached paramiko transport wedged on a dead socket blocks the health check for minutes
        # never gated on the cooldown, this is the recovery path
        if not ping_endpoint(url, timeout=3):
            self._clear_client(context_name)
            with self._lock:
                self._mark_connect_failed(context_name)
            return False

        with self._lock:
            self._mark_connected(context_name)

        return True

    def is_container_running(self, context_name: str, container_id: str) -> bool:
        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_id)
                return container.status == "running"
            except docker.errors.NotFound:
                return False

        return self._call_with_client_op(context_name, _do)

    def get_container_port(self, context_name: str, container_id: str) -> str | None:
        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_id)
                ports = container.attrs["NetworkSettings"]["Ports"]
                for port_mappings in ports.values():
                    if port_mappings:
                        return port_mappings[0]["HostPort"]
            except (KeyError, IndexError, docker.errors.NotFound):
                return None
            return None

        return self._call_with_client_op(context_name, _do)

    def get_running_container_ids(self, context_name: str) -> set[str]:
        def _do():
            try:
                client = self._get_client(context_name)
                # sparse skips the per container inspect, which over ssh opens a channel each and exhausts MaxSessions
                containers = client.containers.list(filters={"status": "running"}, sparse=True)
                return {c.id for c in containers}
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return set()

        return self._call(context_name, _do)

    def run_container(
        self,
        context_name: str,
        image: str,
        port: int,
        command: str,
        environment: dict[str, str | int],
        **kwargs: _DockerRunVal,
    ) -> Container:
        def _do():
            client = self._get_client(context_name)

            def attempt(host_port: int) -> Container:
                return client.containers.run(
                    image,
                    ports={str(port): host_port},
                    command=command,
                    detach=True,
                    auto_remove=True,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    pids_limit=256,
                    environment=environment,
                    **kwargs,
                )

            return _run_with_port_retry(attempt, exhausted_message="failed to find available port after retries")

        return self._call_with_client_op(context_name, _do)

    def create_network(
        self, context_name: str, network_name: str, subnet: str | None = None, labels: dict[str, str] | None = None
    ) -> Network:
        def _do():
            client = self._get_client(context_name)
            ipam_config = None
            if subnet:
                ipam_pool = docker.types.IPAMPool(subnet=subnet)
                ipam_config = docker.types.IPAMConfig(pool_configs=[ipam_pool])
            return client.networks.create(
                network_name,
                driver="bridge",
                ipam=ipam_config,
                labels=labels or {},
            )

        return self._call_with_client_op(context_name, _do)

    def run_container_on_network(
        self,
        context_name: str,
        image: str,
        network_name: str,
        container_name: str,
        command: str | None,
        environment: dict[str, str],
        ip_address: str | None = None,
        publish_port: bool | None = None,
        hostname: str | None = None,
        internal_port: int | None = None,
        **kwargs: _DockerRunVal,
    ) -> tuple[Container, int | None]:
        def _do():
            client = self._get_client(context_name)
            sec_opt = ["no-new-privileges:true"]

            def pin_static_ip(container: Container) -> None:
                if not ip_address:
                    return

                # containers.run attaches with dhcp, so reconnect to pin the address
                network = client.networks.get(network_name)
                network.disconnect(container)
                network.connect(container, ipv4_address=ip_address)

            if not publish_port or not internal_port:
                container = client.containers.run(
                    image,
                    name=container_name,
                    hostname=hostname or container_name,
                    command=command or None,
                    detach=True,
                    auto_remove=True,
                    cap_drop=["ALL"],
                    security_opt=sec_opt,
                    pids_limit=256,
                    environment=environment,
                    network=network_name,
                    **kwargs,
                )
                pin_static_ip(container)
                return container, None

            def attempt(host_port: int) -> tuple[Container, int]:
                container = client.containers.run(
                    image,
                    name=container_name,
                    hostname=hostname or container_name,
                    command=command or None,
                    detach=True,
                    auto_remove=True,
                    cap_drop=["ALL"],
                    security_opt=sec_opt,
                    pids_limit=256,
                    environment=environment,
                    network=network_name,
                    ports={str(internal_port): host_port},
                    **kwargs,
                )
                pin_static_ip(container)
                return container, host_port

            return _run_with_port_retry(attempt, exhausted_message="failed to find available port")

        return self._call_with_client_op(context_name, _do)

    def _parse_container_created(self, created_raw: str) -> float:
        if not created_raw:
            return 0.0

        try:
            iso = created_raw.replace("Z", "+00:00")
            if "." in iso:
                head, tail = iso.split(".", 1)
                tz_idx = max(tail.find("+"), tail.find("-"))
                if tz_idx == -1:
                    frac, tz_suffix = tail, ""
                else:
                    frac, tz_suffix = tail[:tz_idx], tail[tz_idx:]

                # docker emits 9 digit fractional seconds, fromisoformat accepts only 6
                iso = f"{head}.{frac[:6]}{tz_suffix}"
            return datetime.fromisoformat(iso).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    def _list_containers(self, context_name: str, filters: dict[str, str]) -> list[ReconcileEntry]:
        def _do() -> list[ReconcileEntry]:
            try:
                client = self._get_client(context_name)
                containers = client.containers.list(all=True, filters=filters)
                results: list[ReconcileEntry] = []
                for c in containers:
                    created_raw = c.attrs.get("Created", "") if c.attrs else ""
                    labels = c.attrs.get("Config", {}).get("Labels", {}) if c.attrs else {}
                    results.append(
                        {
                            "name": c.name or "",
                            "id": c.id or "",
                            "instance_id": str(labels.get("ctf.instance_id", "")),
                            "created_ts": self._parse_container_created(created_raw),
                        }
                    )
                return results
            # empty on any failure so a flapping host cannot break the sweep loop
            except Exception:
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def list_containers_by_label(self, context_name: str, label_key: str) -> list[ReconcileEntry]:
        return self._list_containers(context_name, {"label": label_key})

    def kill_stack(self, context_name: str, stack_id: str) -> int:
        # fail closed on an unconfigured context, callers keep their db rows and quota for reconciliation
        with self._lock:
            if context_name not in self._context_configs:
                raise ContainerUnavailableException(f"docker context '{context_name}' is not configured")

        def _do():
            client = self._get_client(context_name)
            killed = 0
            containers = client.containers.list(filters={"label": f"ctf.stack_id={stack_id}"}, all=True)
            for c in containers:
                try:
                    c.kill()
                    killed += 1
                except (docker.errors.NotFound, docker.errors.APIError):
                    pass
            networks = client.networks.list(filters={"label": f"ctf.stack_id={stack_id}"})
            for n in networks:
                try:
                    n.remove()
                except docker.errors.APIError:
                    pass
            return killed

        return self._call_with_client_op(context_name, _do)

    def force_remove_resources_by_label(self, context_name: str, label: str) -> tuple[int, int]:
        """remove containers and networks for one exact label, failures propagate so an empty result proves absence"""

        with self._lock:
            if context_name not in self._context_configs:
                raise ContainerUnavailableException(f"docker context '{context_name}' is not configured")

        def _do() -> tuple[int, int]:
            client = self._get_client(context_name)
            removed_containers = 0
            removed_networks = 0
            containers = client.containers.list(filters={"label": label}, all=True)
            for container in containers:
                try:
                    container.remove(force=True)
                    removed_containers += 1
                except docker.errors.NotFound:
                    pass
                except docker.errors.APIError as error:
                    if not _confirm_removal_in_progress(container, error):
                        raise
                    removed_containers += 1
            networks = client.networks.list(filters={"label": label})
            for network in networks:
                try:
                    network.remove()
                    removed_networks += 1
                except docker.errors.NotFound:
                    pass
            return removed_containers, removed_networks

        return self._call_with_client_op(context_name, _do)

    def count_resources_by_label(self, context_name: str, label: str) -> tuple[int, int]:
        """count containers and networks for one exact label, failures propagate rather than reading as zero"""

        def _do() -> tuple[int, int]:
            client = self._get_client(context_name)
            containers = client.containers.list(filters={"label": label}, all=True)
            networks = client.networks.list(filters={"label": label})
            return len(containers), len(networks)

        return self._call_with_client_op(context_name, _do)

    def get_container_logs(self, context_name: str, container_id: str, tail: int = 200) -> str:
        def _do():
            try:
                client = self._get_client(context_name)
                container = client.containers.get(container_id)
                output = container.logs(stdout=True, stderr=True, tail=tail)
                if isinstance(output, bytes):
                    return output.decode("utf-8", errors="replace")
                return output
            except docker.errors.NotFound:
                return ""

        return self._call_with_client_op(context_name, _do)

    def get_volume_metadata(self, context_name: str, docker_name: str) -> VolumeMetadata | None:
        """inspect an existing volume, never creates one"""

        def _do():
            client = self._get_client(context_name)
            return docker_volume_metadata(client, docker_name)

        return self._call_with_client_op(context_name, _do)

    def get_images(self, context_name: str) -> list[str]:
        def _do():
            try:
                client = self._get_client(context_name)
                images = client.images.list()
                tags = []
                for image in images:
                    for tag in image.tags:
                        if tag:
                            tags.append(tag)
                return sorted(tags)
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def pull_image(self, context_name: str, image: str) -> str:
        with self._lock:
            url = self._context_configs.get(context_name)

        if not url:
            raise Exception(f"no client for context '{context_name}'")

        def _do():
            client = _new_docker_client(url, timeout=PULL_CLIENT_TIMEOUT)
            try:
                client.images.pull(image)
                return "ok"
            # only a dead ssh transport implicates the cached client, api errors leave it alone to avoid churn
            except paramiko.ssh_exception.SSHException:
                self._clear_client(context_name)
                raise
            finally:
                try:
                    client.close()
                except Exception:
                    pass

        return self._call(context_name, _do)

    def get_image_info(self, context_name: str, image: str | None) -> ImageInfo | None:
        def _do():
            try:
                client = self._get_client(context_name)
                img = client.images.get(image)
                attrs = img.attrs or {}
                size_mb = round((attrs.get("Size") or 0) / 1024 / 1024)
                created = attrs.get("Created", "")[:19].replace("T", " ")
                # reproducible builds from nix or bazel report 1970 or 1980, so fall back to LastTagTime
                if created.startswith("1970") or created.startswith("1980"):
                    last_tag = (attrs.get("Metadata") or {}).get("LastTagTime", "")
                    if last_tag:
                        created = last_tag[:19].replace("T", " ")
                short_id = img.short_id.replace("sha256:", "")
                return {"id": short_id, "size_mb": size_mb, "created": created}
            except docker.errors.ImageNotFound:
                return None
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return None

        return self._call(context_name, _do)
