import os
import json
import threading
import logging

import docker
import paramiko

logger = logging.getLogger(__name__)

LOCAL_CONTEXT_NAME = "local"
LOCAL_SOCKET_PATH = "/var/run/docker.sock"


def _scan_context_meta(context_name=None):
    """Read docker context metadata from ~/.docker/contexts/meta/.
    Docker stores context dirs by sha256 hash, not by name,
    so we scan all entries and match on the Name field."""
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


def _resolve_endpoint(context_name, hostname):
    meta = _scan_context_meta(context_name)
    if meta:
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host")
        if endpoint:
            return endpoint

    if hostname:
        if "@" in hostname:
            return f"ssh://{hostname}"
        return f"ssh://root@{hostname}"

    if os.path.exists(LOCAL_SOCKET_PATH):
        return f"unix://{LOCAL_SOCKET_PATH}"

    return None


def discover_contexts():
    """Scan the host for available docker contexts."""
    discovered = []
    for meta in _scan_context_meta():
        name = meta.get("Name", "")
        endpoint = meta.get("Endpoints", {}).get("docker", {}).get("Host", "")
        if name:
            discovered.append({"name": name, "endpoint": endpoint})

    if not any(d["name"] == LOCAL_CONTEXT_NAME for d in discovered):
        if os.path.exists(LOCAL_SOCKET_PATH):
            discovered.append({"name": LOCAL_CONTEXT_NAME, "endpoint": f"unix://{LOCAL_SOCKET_PATH}"})

    return discovered


def ping_endpoint(endpoint, timeout=3):
    """Quick connectivity check for a docker endpoint."""
    try:
        client = docker.DockerClient(base_url=endpoint, timeout=timeout)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


class DockerHostManager:
    def __init__(self):
        self._context_configs = {}
        self._pub_hostnames = {}
        self._clients = {}
        self._config_generation = 0
        self._client_generation = -1
        self._lock = threading.Lock()
        self._semaphores = {}

    def _get_client(self, context_name):
        with self._lock:
            if self._client_generation != self._config_generation:
                for old in self._clients.values():
                    try:
                        old.close()
                    except Exception:
                        pass
                self._clients = {}
                self._client_generation = self._config_generation

            if context_name in self._clients:
                return self._clients[context_name]

            url = self._context_configs.get(context_name)
            if not url:
                raise Exception(f"no client for context '{context_name}'")

            client = docker.DockerClient(base_url=url)
            self._clients[context_name] = client
            return client

    def _clear_client(self, context_name):
        with self._lock:
            old = self._clients.pop(context_name, None)
        if old:
            try:
                old.close()
            except Exception:
                pass

    def _init_semaphores(self, limit):
        new_semaphores = {}
        for ctx_name in self._context_configs:
            new_semaphores[ctx_name] = threading.BoundedSemaphore(limit)
        self._semaphores = new_semaphores

    def acquire_semaphore(self, context_name, timeout=30):
        sem = self._semaphores.get(context_name)
        if sem is None:
            return True
        acquired = sem.acquire(blocking=True, timeout=timeout)
        if not acquired:
            raise Exception("server busy, please try again shortly")
        return True

    def release_semaphore(self, context_name):
        sem = self._semaphores.get(context_name)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def load_contexts(self, contexts, max_concurrent_creates=2):
        """(Re)connect all enabled contexts. contexts is a list of
        DockerContextModel rows."""
        new_configs = {}
        new_pub_hostnames = {}

        for ctx in contexts:
            endpoint = _resolve_endpoint(ctx.context_name, ctx.hostname)
            if not endpoint:
                logger.warning(f"no endpoint for context '{ctx.context_name}', skipping")
                continue

            try:
                client = docker.DockerClient(base_url=endpoint)
                client.ping()
                client.close()
                new_configs[ctx.context_name] = endpoint
                new_pub_hostnames[ctx.context_name] = ctx.pub_hostname
                logger.info(f"connected to context '{ctx.context_name}' at {endpoint}")
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
                logger.error(f"could not connect to context '{ctx.context_name}': {e}")

        with self._lock:
            self._context_configs = new_configs
            self._pub_hostnames = new_pub_hostnames
            self._config_generation += 1

        self._init_semaphores(max_concurrent_creates)

    def get_pub_hostname(self, context_name):
        return self._pub_hostnames.get(context_name)

    def get_connected_contexts(self):
        return list(self._context_configs.keys())

    def has_contexts(self):
        return bool(self._context_configs)

    def ping(self, context_name):
        try:
            client = self._get_client(context_name)
            client.ping()
            return True
        except Exception:
            self._clear_client(context_name)
            return False

    def is_container_running(self, context_name, container_id):
        try:
            client = self._get_client(context_name)
            container = client.containers.get(container_id)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except docker.errors.DockerException:
            self._clear_client(context_name)
            raise

    def get_container_port(self, context_name, container_id):
        try:
            client = self._get_client(context_name)
            container = client.containers.get(container_id)
            ports = container.attrs["NetworkSettings"]["Ports"]
            for port_mappings in ports.values():
                if port_mappings:
                    return port_mappings[0]["HostPort"]
        except (KeyError, IndexError, docker.errors.NotFound):
            return None
        except docker.errors.DockerException:
            self._clear_client(context_name)
            raise
        return None

    def get_running_container_ids(self, context_name):
        try:
            client = self._get_client(context_name)
            containers = client.containers.list(filters={"status": "running"})
            return {c.id for c in containers}
        except docker.errors.DockerException:
            self._clear_client(context_name)
            return set()

    def run_container(self, context_name, image, port, command, environment, **kwargs):
        """Create and start a container on the specified context."""
        import random

        client = self._get_client(context_name)
        last_err = None
        for _ in range(50):
            host_port = random.randint(40000, 59999)
            try:
                container = client.containers.run(
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
                return container
            except docker.errors.APIError as e:
                # port conflict, retry with a different port
                if "port is already allocated" in str(e) or "address already in use" in str(e):
                    last_err = e
                    continue
                self._clear_client(context_name)
                raise
            except docker.errors.DockerException:
                self._clear_client(context_name)
                raise

        raise docker.errors.DockerException(f"failed to find available port after retries: {last_err}")

    def kill_container(self, context_name, container_id):
        try:
            client = self._get_client(context_name)
            container = client.containers.get(container_id)
            container.kill()
            return True
        except docker.errors.NotFound:
            return False
        except docker.errors.DockerException:
            self._clear_client(context_name)
            raise

    # -- compose stack operations --

    def create_network(self, context_name, network_name, subnet=None, labels=None):
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

    def run_container_on_network(
        self,
        context_name,
        image,
        network_name,
        container_name,
        command,
        environment,
        ip_address=None,
        publish_port=None,
        hostname=None,
        internal_port=None,
        **kwargs,
    ):
        import random

        client = self._get_client(context_name)

        # skip no-new-privileges when cap_add is set so file capabilities work
        sec_opt = [] if kwargs.get("cap_add") else ["no-new-privileges:true"]

        if publish_port and internal_port:
            last_err = None
            for _ in range(50):
                host_port = random.randint(40000, 59999)
                try:
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
                except docker.errors.APIError as e:
                    if "port is already allocated" in str(e) or "address already in use" in str(e):
                        last_err = e
                        continue
                    raise
            raise docker.errors.DockerException(f"failed to find available port: {last_err}")
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

    def kill_stack(self, context_name, stack_id):
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

    def get_container_logs(self, context_name, container_id, tail=200):
        try:
            client = self._get_client(context_name)
            container = client.containers.get(container_id)
            output = container.logs(stdout=True, stderr=True, tail=tail)
            if isinstance(output, bytes):
                return output.decode("utf-8", errors="replace")
            return output
        except docker.errors.NotFound:
            return ""
        except docker.errors.DockerException:
            self._clear_client(context_name)
            raise

    def get_images(self, context_name):
        try:
            client = self._get_client(context_name)
            images = client.images.list()
            tags = []
            for image in images:
                for tag in image.tags:
                    if tag:
                        tags.append(tag)
            return sorted(tags)
        except docker.errors.DockerException:
            self._clear_client(context_name)
            return []

    def pull_image(self, context_name, image):
        client = self._get_client(context_name)
        try:
            client.images.pull(image)
            return "ok"
        except docker.errors.DockerException:
            self._clear_client(context_name)
            raise

    def check_image(self, context_name, image):
        try:
            client = self._get_client(context_name)
            client.images.get(image)
            return True
        except Exception:
            return False

    def get_image_info(self, context_name, image):
        """Return image metadata (id, size, build time) or None."""
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
        except Exception:
            return None
