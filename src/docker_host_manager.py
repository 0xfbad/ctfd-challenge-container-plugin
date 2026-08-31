from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable
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

# docker SDK HTTP read timeout for control plane ops
DEFAULT_CLIENT_TIMEOUT = 10
# image pulls run for minutes, give them their own short-lived client
PULL_CLIENT_TIMEOUT = 300
# per-context pool size, caps concurrent in-flight blocking calls per host
THREADPOOL_SIZE = 4

# value types that appear in kwargs forwarded to docker containers.run
_DockerRunVal = str | int | bool | list[str] | dict[str, str] | dict[str, dict[str, str]]

_T = TypeVar("_T")


def _confirm_removal_in_progress(container: Container, error: docker.errors.APIError) -> bool:
    """Wait briefly for Docker's auto-remove operation to become observable."""
    if getattr(error, "status_code", None) != 409 or "already in progress" not in str(
        getattr(error, "explanation", error)
    ):
        return False
    for _attempt in range(20):
        try:
            container.reload()
        except docker.errors.NotFound:
            return True
        time.sleep(0.05)
    return False


def _run_with_port_retry(attempt: Callable[[int], _T], *, exhausted_message: str) -> _T:
    # retry containers.run on a fresh random host port when docker reports the port taken,
    # the attempt callable owns the actual run (and any static-ip reconnect) for one candidate
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


# docker context metadata from ~/.docker/contexts/meta/*/meta.json
_ContextMeta = dict[str, object]


@overload
def _scan_context_meta(context_name: str) -> _ContextMeta | None: ...


@overload
def _scan_context_meta(context_name: None = None) -> list[_ContextMeta]: ...


def _scan_context_meta(context_name: str | None = None) -> _ContextMeta | list[_ContextMeta] | None:
    # docker hashes context dirs by sha256, so we scan all entries and match by Name
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
            if context_name:
                if meta.get("Name") == context_name:
                    return meta
            else:
                results.append(meta)
        except Exception:
            continue

    return None if context_name else results


def _resolve_endpoint(context_name: str, hostname: str | None) -> str | None:
    meta = _scan_context_meta(context_name)
    if meta:
        endpoints = meta.get("Endpoints", {})
        if isinstance(endpoints, dict):
            docker_ep = endpoints.get("docker", {})
            if isinstance(docker_ep, dict):
                endpoint = docker_ep.get("Host")
                if endpoint:
                    return str(endpoint)

    if hostname:
        if "@" in hostname:
            return f"ssh://{hostname}"
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
        if name:
            discovered.append({"name": name, "endpoint": endpoint})

    if not any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered):
        if os.path.exists(LOCAL_SOCKET_PATH):
            discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def ping_endpoint(endpoint: str, timeout: int = 3) -> bool:
    client = None
    try:
        client = docker.DockerClient(base_url=endpoint, timeout=timeout)
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
        # Configuration and reachability are intentionally separate. A context
        # that is down during load remains configured so health checks and
        # cleanup can recover it without an admin reload.
        self._context_configs: dict[str, str] = {}
        self._connected_contexts: set[str] = set()
        self._pub_hostnames: dict[str, str | None] = {}
        # keyed by (context_name, thread_ident) because paramiko Channels bind
        # gevent.Event to the Hub of the creating thread. reuse from another
        # gevent threadpool worker raises gevent.InvalidThreadUseError
        self._clients: dict[tuple[str, int], DockerClient] = {}
        self._config_generation: int = 0
        self._client_generation: int = -1
        # reentrant so a wrapped op can re-enter lock-protected helpers without
        # tripping a deadlock if some future caller ever holds the lock across _call
        self._lock: threading.RLock = threading.RLock()
        # per-context threadpool keeps paramiko blocking off the gevent hub,
        # so one hung host can't stop the worker from serving other requests
        self._threadpools: dict[str, gevent.threadpool.ThreadPool] = {}

    def _get_threadpool(self, context_name: str) -> gevent.threadpool.ThreadPool:
        with self._lock:
            pool = self._threadpools.get(context_name)
            if pool is None:
                pool = gevent.threadpool.ThreadPool(maxsize=THREADPOOL_SIZE)
                self._threadpools[context_name] = pool
            return pool

    def _call(self, context_name: str, fn, *args, **kwargs):
        # gevent.threadpool.ThreadPool.apply needs the gevent hub, which only
        # exists when monkey-patching is active (gunicorn worker). during
        # Flask CLI paths do not initialize the gevent hub and
        # apply() hangs in futex, so run inline in those cases
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
                # prune entries for dead threads. gevent threadpool workers
                # rarely die so this is a cheap safety net, not a hot path.
                # bounded by num_contexts * THREADPOOL_SIZE
                live_idents = {t.ident for t in threading.enumerate()}
                dead_keys = [k for k in self._clients if k[1] not in live_idents]
                for k in dead_keys:
                    to_close.append(self._clients.pop(k))

            key = (context_name, tid)
            if key in self._clients:
                client = self._clients[key]
            else:
                url = self._context_configs.get(context_name)
                if not url:
                    raise Exception(f"no client for context '{context_name}'")
                client = docker.DockerClient(base_url=url, timeout=DEFAULT_CLIENT_TIMEOUT)
                self._clients[key] = client

        # close outside the lock, paramiko teardown can block on SSH for seconds
        for old in to_close:
            try:
                old.close()
            except Exception:
                pass
        return client

    def _clear_client(self, context_name: str) -> None:
        # drop EVERY cached client for this context across all threads so any
        # worker that next calls _get_client builds a fresh one. preserves the
        # original contract (next call gets a new client) but accounts for N
        # cached entries instead of 1
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
        # shared broad-catch: DockerException/SSHException drop the cached client
        # and re-raise as is. anything else (gevent.InvalidThreadUseError, paramiko
        # ChannelException, etc) surfaces when the cached client is reused from a
        # different gevent hub, drop the client and surface as a typed transient.
        # method-specific exceptions (NotFound, KeyError, etc) MUST be handled
        # inside fn before they reach this layer
        try:
            result = fn()
            with self._lock:
                self._connected_contexts.add(context_name)
            return result
        except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
            self._clear_client(context_name)
            raise
        except Exception:
            self._clear_client(context_name)
            raise ContainerUnavailableException(f"transient client failure on {context_name}")

    def _call_with_client_op(self, context_name, fn):
        return self._call(context_name, lambda: self._invoke_client_op(context_name, fn))

    def load_contexts(self, contexts: list[DockerContextModel]) -> None:
        new_configs = {}
        new_pub_hostnames = {}
        new_connected = set()

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            # Retain the resolved endpoint regardless of this load-time ping.
            new_configs[ctx.context_name] = endpoint
            new_pub_hostnames[ctx.context_name] = ctx.pub_hostname

            def _check(endpoint=endpoint):
                client = None
                try:
                    client = docker.DockerClient(base_url=endpoint, timeout=DEFAULT_CLIENT_TIMEOUT)
                    client.ping()
                    return None
                except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
                    return e
                finally:
                    if client:
                        try:
                            client.close()
                        except Exception:
                            pass

            try:
                err = self._call(ctx.context_name, _check)
            except Exception as e:
                err = e

            if err is None:
                new_connected.add(ctx.context_name)
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            else:
                logger.error(f"could not connect to context '{ctx.context_name}': {err}")

        with self._lock:
            self._context_configs = new_configs
            self._connected_contexts = new_connected
            self._pub_hostnames = new_pub_hostnames
            self._config_generation += 1

    def get_pub_hostname(self, context_name: str) -> str | None:
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self) -> list[str]:
        with self._lock:
            return sorted(self._connected_contexts)

    def get_configured_contexts(self) -> list[str]:
        with self._lock:
            return sorted(self._context_configs)

    def has_contexts(self) -> bool:
        with self._lock:
            return bool(self._connected_contexts)

    def ping(self, context_name: str) -> bool:
        # use a fresh ephemeral client. cached clients share paramiko transports
        # that wedge on dead-but-unreaped TCP sockets after idle periods, blocking
        # the 30s health_check past its interval for the full kernel retransmit cycle
        with self._lock:
            url = self._context_configs.get(context_name)
        if not url:
            return False
        if ping_endpoint(url, timeout=3):
            with self._lock:
                self._connected_contexts.add(context_name)
            return True
        self._clear_client(context_name)
        return False

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
                # sparse=True skips per-container inspect calls, which over ssh opens a
                # channel per container and exhausts the remote's MaxSessions
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
        # docker run kwargs (name, hostname, mem_limit, cpu_quota, volumes, cap_add, etc.)
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
        # forwarded to docker containers.run (labels, cap_add, mem_limit, etc.)
        **kwargs: _DockerRunVal,
    ) -> tuple[Container, int | None]:
        def _do():
            client = self._get_client(context_name)
            sec_opt = ["no-new-privileges:true"]

            if publish_port and internal_port:

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
                    if ip_address:
                        # reconnect with static IP (initial connect used DHCP)
                        network = client.networks.get(network_name)
                        network.disconnect(container)
                        network.connect(container, ipv4_address=ip_address)
                    return container, host_port

                return _run_with_port_retry(attempt, exhausted_message="failed to find available port")
            else:
                container = client.containers.run(
                    image,
                    name=container_name,
                    hostname=container_name,
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
                if ip_address:
                    network = client.networks.get(network_name)
                    network.disconnect(container)
                    network.connect(container, ipv4_address=ip_address)
                return container, None

        return self._call_with_client_op(context_name, _do)

    def _parse_container_created(self, created_raw: str) -> float:
        # docker emits 9-digit fractional seconds, fromisoformat only accepts 6
        if not created_raw:
            return 0.0
        try:
            from datetime import datetime

            iso = created_raw.replace("Z", "+00:00")
            if "." in iso:
                head, tail = iso.split(".", 1)
                tz_idx = max(tail.find("+"), tail.find("-"))
                if tz_idx == -1:
                    frac, tz_suffix = tail, ""
                else:
                    frac, tz_suffix = tail[:tz_idx], tail[tz_idx:]
                iso = f"{head}.{frac[:6]}{tz_suffix}"
            return datetime.fromisoformat(iso).timestamp()
        except (ValueError, AttributeError):
            return 0.0

    def _list_containers(self, context_name: str, filters: dict[str, str]) -> list[ReconcileEntry]:
        # Return label identity and age for every matching container (any state).
        # swallows errors and returns [] so a flapping host can't break the sweep loop
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
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException):
                self._clear_client(context_name)
                return []
            except Exception:
                self._clear_client(context_name)
                return []

        return self._call(context_name, _do)

    def list_containers_by_label(self, context_name: str, label_key: str) -> list[ReconcileEntry]:
        # used by the reconcile sweep for any container carrying the given label (any value)
        return self._list_containers(context_name, {"label": label_key})

    def kill_stack(self, context_name: str, stack_id: str) -> int:
        # Missing/unreachable configuration is not evidence of Docker absence.
        # Fail closed so callers retain DB rows/quota for later reconciliation.
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
        """Strictly remove containers and networks matching one exact label.

        Docker/SSH failures are propagated, so an empty successful result is
        usable as proof of absence by lifecycle reconciliation.
        """

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
        """Count containers and networks for an exact label filter.

        Transport failures propagate so callers never mistake an unreachable
        daemon for proof that no resources remain.
        """

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
        """Inspect an exact pre-provisioned volume without creating it."""

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
        # pulls can run for minutes, use a dedicated short-lived client with a
        # longer timeout instead of the shared 10s control-plane client
        with self._lock:
            url = self._context_configs.get(context_name)
        if not url:
            raise Exception(f"no client for context '{context_name}'")

        def _do():
            client = docker.DockerClient(base_url=url, timeout=PULL_CLIENT_TIMEOUT)
            try:
                client.images.pull(image)
                return "ok"
            except paramiko.ssh_exception.SSHException:
                # only the ssh transport-dead case implicates the cached client,
                # api-level DockerException leaves it alone to avoid churn
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
                # nix/bazel reproducible builds report 1970/1980, use LastTagTime instead
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
