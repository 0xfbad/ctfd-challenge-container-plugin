from __future__ import annotations

import atexit
import socket
import time
import json
import os
import threading
from typing import Any

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
import docker
import paramiko
import requests

from CTFd.models import db
from .models import ContainerInfoModel, DockerContextModel

try:
    from gevent.threadpool import ThreadPool as _GeventPool
    from gevent.lock import BoundedSemaphore as _GeventSemaphore

    _HAS_GEVENT = True
except ImportError:
    _HAS_GEVENT = False

CPU_QUOTA_BASE = 100000
LOCAL_CONTEXT_NAME = "local"


class ContainerException(Exception):
    def __init__(self, *args):
        super().__init__(*args)
        self.message = args[0] if args else "unknown container exception"

    def __str__(self):
        return self.message


class _ThreadLocalClients(threading.local):
    def __init__(self):
        super().__init__()
        self.clients = {}
        self.generation = -1


class ContainerManager:
    def __init__(self, settings, app):
        self.settings = settings
        self.app = app
        self._context_configs = {}
        self._context_weights = {}
        self._thread_local = _ThreadLocalClients()
        self._context_lock = threading.Lock()
        self._config_generation = 0
        self._pool = None
        self._semaphores = {}

        self._init_pool()

        try:
            self.initialize_connection()
        except ContainerException:
            print("docker could not initialize or connect")

    def _init_pool(self):
        from .utils import get_setting

        size = get_setting("thread_pool_size", 4)

        if _HAS_GEVENT:
            self._pool = _GeventPool(maxsize=size)
        else:
            from concurrent.futures import ThreadPoolExecutor

            self._pool = ThreadPoolExecutor(max_workers=size)

    def _init_semaphores(self):
        from .utils import get_setting

        limit = get_setting("max_concurrent_creates", 2)

        new_semaphores = {}
        for ctx_name in self._context_configs:
            if _HAS_GEVENT:
                new_semaphores[ctx_name] = _GeventSemaphore(limit)
            else:
                new_semaphores[ctx_name] = threading.BoundedSemaphore(limit)

        self._semaphores = new_semaphores

    def _submit(self, fn, *args, **kwargs):
        if _HAS_GEVENT:
            result = self._pool.spawn(fn, *args, **kwargs)
            return result.get()
        else:
            future = self._pool.submit(fn, *args, **kwargs)
            return future.result()

    def _get_client(self, context_name):
        tl = self._thread_local

        if tl.generation != self._config_generation:
            tl.clients = {}
            tl.generation = self._config_generation

        clients = tl.clients
        if context_name in clients:
            return clients[context_name]

        url = self._context_configs.get(context_name)
        if not url:
            raise ContainerException(f"docker context '{context_name}' not available")

        if url == "__from_env__":
            client = docker.from_env()
        else:
            client = docker.DockerClient(base_url=url)

        clients[context_name] = client
        return client

    def _clear_thread_local_client(self, context_name):
        self._thread_local.clients.pop(context_name, None)

    def _acquire_semaphore(self, context_name, timeout=30):
        sem = self._semaphores.get(context_name)
        if sem is None:
            return True

        if _HAS_GEVENT:
            acquired = sem.acquire(blocking=True, timeout=timeout)
        else:
            acquired = sem.acquire(blocking=True, timeout=timeout)

        if not acquired:
            raise ContainerException("server busy, please try again shortly")

        return True

    def _release_semaphore(self, context_name):
        sem = self._semaphores.get(context_name)
        if sem is not None:
            try:
                sem.release()
            except ValueError:
                pass

    def initialize_connection(self):
        current_thread = threading.current_thread()
        is_scheduler_thread = hasattr(current_thread, "_target") and "apscheduler" in str(current_thread._target)

        if not is_scheduler_thread:
            try:
                self.expiration_scheduler.shutdown()
            except (SchedulerNotRunningError, AttributeError):
                pass

        self.load_docker_contexts()

        if not is_scheduler_thread:
            self.setup_expiration_scheduler()

    def load_docker_contexts(self):
        new_configs = {}
        new_weights = {}

        try:
            contexts = DockerContextModel.query.filter_by(enabled=True).all()
        except Exception as e:
            print(f"could not query docker contexts (database may need migration): {e}")
            contexts = []

        # handle local docker context
        local_ctx = DockerContextModel.query.filter_by(context_name=LOCAL_CONTEXT_NAME).first()

        if local_ctx is None:
            # first boot: auto-detect and create a DB row if the socket responds
            try:
                client = docker.from_env()
                client.ping()
                client.close()
                local_ctx = DockerContextModel(
                    context_name=LOCAL_CONTEXT_NAME,
                    hostname=socket.gethostname(),
                    enabled=True,
                    weight=1,
                )
                db.session.add(local_ctx)
                db.session.commit()
                new_configs[LOCAL_CONTEXT_NAME] = "__from_env__"
                new_weights[LOCAL_CONTEXT_NAME] = local_ctx.weight
            except (
                docker.errors.DockerException,
                paramiko.ssh_exception.SSHException,
                requests.exceptions.RequestException,
            ) as e:
                print(f"could not connect to local docker context: {e}")
        elif local_ctx.enabled:
            try:
                client = docker.from_env()
                client.ping()
                client.close()
                new_configs[LOCAL_CONTEXT_NAME] = "__from_env__"
                new_weights[LOCAL_CONTEXT_NAME] = local_ctx.weight
            except (
                docker.errors.DockerException,
                paramiko.ssh_exception.SSHException,
                requests.exceptions.RequestException,
            ) as e:
                print(f"could not connect to local docker context: {e}")

        for context in contexts:
            if context.context_name == LOCAL_CONTEXT_NAME:
                continue

            try:
                docker_endpoint = None

                context_file = os.path.expanduser(f"~/.docker/contexts/meta/{context.context_name}/meta.json")

                if os.path.exists(context_file):
                    try:
                        with open(context_file, "r") as f:
                            context_meta = json.load(f)
                            docker_endpoint = context_meta.get("Endpoints", {}).get("docker", {}).get("Host")
                    except Exception as e:
                        print(f"could not read context meta file: {e}")

                if not docker_endpoint:
                    if context.hostname:
                        if "@" in context.hostname:
                            docker_endpoint = f"ssh://{context.hostname}"
                        else:
                            docker_endpoint = f"ssh://root@{context.hostname}"
                    else:
                        print(f"no hostname configured for context '{context.context_name}', skipping")
                        continue

                # validate with a throwaway client, then store only the URL
                client = docker.DockerClient(base_url=docker_endpoint)
                client.ping()
                client.close()
                new_configs[context.context_name] = docker_endpoint
                new_weights[context.context_name] = context.weight
            except (
                docker.errors.DockerException,
                paramiko.ssh_exception.SSHException,
                requests.exceptions.RequestException,
            ) as e:
                print(f"could not connect to docker context '{context.context_name}': {e}")

        if not new_configs:
            print("no docker contexts available, containers will not work until contexts are configured")

        with self._context_lock:
            self._context_configs = new_configs
            self._context_weights = new_weights
            self._config_generation += 1

        self._init_semaphores()

    def get_next_context(self):
        with self._context_lock:
            if not self._context_configs:
                raise ContainerException("no docker contexts available")

            # least-connections scoring: weight / (active_count + 1)
            counts = {}
            try:
                with self.app.app_context():
                    for row in ContainerInfoModel.query.all():
                        ctx = row.docker_context or LOCAL_CONTEXT_NAME
                        counts[ctx] = counts.get(ctx, 0) + 1
            except Exception:
                pass

            candidates = []
            for ctx_name in self._context_configs:
                weight = self._context_weights.get(ctx_name, 1)
                count = counts.get(ctx_name, 0)
                score = weight / (count + 1)
                candidates.append((score, ctx_name))

            candidates.sort(key=lambda x: (-x[0], x[1]))
            return candidates[0][1]

    def setup_expiration_scheduler(self):
        from .utils import get_setting

        expiration_check_interval = get_setting("expiration_check_interval", 5)

        if _HAS_GEVENT:
            from apscheduler.schedulers.gevent import GeventScheduler

            self.expiration_scheduler = GeventScheduler()
        else:
            self.expiration_scheduler = BackgroundScheduler()

        self.expiration_scheduler.add_job(
            func=self.kill_expired_containers,
            args=(self.app,),
            trigger="interval",
            seconds=expiration_check_interval,
            misfire_grace_time=30,
            coalesce=True,
        )
        self.expiration_scheduler.add_job(
            func=self._health_check,
            trigger="interval",
            seconds=30,
            misfire_grace_time=30,
            coalesce=True,
        )
        self.expiration_scheduler.start()

        atexit.register(lambda: self.expiration_scheduler.shutdown())

    def _health_check(self):
        removed = []

        for ctx_name, url in list(self._context_configs.items()):
            try:
                if url == "__from_env__":
                    client = docker.from_env()
                else:
                    client = docker.DockerClient(base_url=url)
                client.ping()
                client.close()
            except Exception:
                removed.append(ctx_name)

        if removed:
            with self._context_lock:
                for ctx_name in removed:
                    print(f"health check: removing unhealthy context '{ctx_name}'")
                    self._context_configs.pop(ctx_name, None)
                    self._context_weights.pop(ctx_name, None)
                    self._semaphores.pop(ctx_name, None)

                self._config_generation += 1

        # recovery pass: try to reconnect contexts that were previously removed
        self._recover_contexts()

    def _recover_contexts(self):
        try:
            with self.app.app_context():
                known_contexts = DockerContextModel.query.filter_by(enabled=True).all()
        except Exception:
            return

        recovered = []
        for ctx in known_contexts:
            if ctx.context_name in self._context_configs:
                continue

            if ctx.context_name == LOCAL_CONTEXT_NAME:
                try:
                    client = docker.from_env()
                    client.ping()
                    client.close()
                except Exception:
                    continue

                recovered.append((ctx.context_name, ctx.weight, "__from_env__"))
                print(f"health check: recovered context '{ctx.context_name}'")
                continue

            endpoint = None
            context_file = os.path.expanduser(f"~/.docker/contexts/meta/{ctx.context_name}/meta.json")
            if os.path.exists(context_file):
                try:
                    with open(context_file, "r") as f:
                        context_meta = json.load(f)
                        endpoint = context_meta.get("Endpoints", {}).get("docker", {}).get("Host")
                except Exception:
                    pass

            if not endpoint:
                if ctx.hostname:
                    endpoint = f"ssh://{ctx.hostname}" if "@" in ctx.hostname else f"ssh://root@{ctx.hostname}"
                else:
                    continue

            try:
                client = docker.DockerClient(base_url=endpoint)
                client.ping()
                client.close()
            except Exception:
                continue

            recovered.append((ctx.context_name, ctx.weight, endpoint))
            print(f"health check: recovered context '{ctx.context_name}'")

        if not recovered:
            return

        from .utils import get_setting

        limit = get_setting("max_concurrent_creates", 2)

        with self._context_lock:
            for ctx_name, weight, endpoint in recovered:
                self._context_configs[ctx_name] = endpoint
                self._context_weights[ctx_name] = weight
                if _HAS_GEVENT:
                    self._semaphores[ctx_name] = _GeventSemaphore(limit)
                else:
                    self._semaphores[ctx_name] = threading.BoundedSemaphore(limit)

            self._config_generation += 1

    def run_command(func: Any) -> Any:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not self._context_configs:
                try:
                    self.initialize_connection()
                except ContainerException:
                    raise ContainerException("docker is not connected")

            if not self._context_configs:
                raise ContainerException("no docker contexts available")

            return func(self, *args, **kwargs)

        return wrapper

    @run_command
    def kill_expired_containers(self, app: Flask):
        with app.app_context():
            from .event_logger import event_logger

            containers = ContainerInfoModel.query.all()
            killed = []

            for container in containers:
                if container.expires == 0:
                    continue

                if container.expires < int(time.time()):
                    try:
                        self.kill_container(container.container_id, container.docker_context)
                    except ContainerException:
                        print("[container expiry job] docker is not initialized, please check your settings")
                        continue

                    event_logger.log_event(
                        "expired",
                        f"container expired for {container.challenge.name if container.challenge else 'unknown'}",
                        user_id=container.user_id,
                        username=container.user.name if container.user else None,
                        metadata={
                            "container_id": container.container_id,
                            "challenge_id": container.challenge_id,
                            "challenge_name": container.challenge.name if container.challenge else None,
                            "team_id": container.team_id,
                            "team_name": container.team.name if container.team else None,
                        },
                    )

                    killed.append(container)

            for container in killed:
                db.session.delete(container)
            if killed:
                db.session.commit()

    @run_command
    def is_container_running(self, container_id: str, context_name: str | None = None) -> bool:
        def _check(cid, ctx):
            client = self._get_client(ctx)
            try:
                container = client.containers.get(cid)
                return container.status == "running"
            except docker.errors.NotFound:
                return False
            except docker.errors.DockerException:
                self._clear_thread_local_client(ctx)
                raise

        if context_name and context_name in self._context_configs:
            try:
                return self._submit(_check, container_id, context_name)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self._context_configs:
            try:
                if self._submit(_check, container_id, ctx):
                    return True
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return False

    @run_command
    def get_running_container_ids(self) -> set[str]:
        def _list_running(ctx):
            client = self._get_client(ctx)
            try:
                containers = client.containers.list(filters={"status": "running"})
                return {c.id for c in containers}
            except docker.errors.DockerException:
                self._clear_thread_local_client(ctx)
                return set()

        result = set()
        for ctx in list(self._context_configs):
            try:
                ids = self._submit(_list_running, ctx)
                result.update(ids)
            except Exception:
                continue
        return result

    @run_command
    def create_container(
        self,
        chal_id: int | str,
        team_id: int | str,
        user_id: int | str,
        image: str,
        port: int,
        command: str,
        volumes: str,
        max_memory_mb: int | None = None,
        max_cpu: float | None = None,
        context_name: str | None = None,
        extra_env: dict | None = None,
    ):
        kwargs: dict[str, Any] = {}

        if max_memory_mb:
            try:
                mem_limit = int(max_memory_mb)
                if mem_limit > 0:
                    kwargs["mem_limit"] = f"{mem_limit}m"
            except ValueError:
                raise ContainerException("memory limit must be an integer")

        if max_cpu:
            try:
                cpu_quota = float(max_cpu)
                if cpu_quota > 0:
                    kwargs["cpu_quota"] = int(cpu_quota * CPU_QUOTA_BASE)
                    kwargs["cpu_period"] = CPU_QUOTA_BASE
                else:
                    raise ValueError
            except ValueError:
                raise ContainerException("cpu limit must be a positive number")

        if volumes:
            try:
                volumes_dict = json.loads(volumes)
                kwargs["volumes"] = volumes_dict
            except json.decoder.JSONDecodeError:
                raise ContainerException("volumes json string is invalid")

        def _do_create(ctx):
            client = self._get_client(ctx)
            try:
                container = client.containers.run(
                    image,
                    ports={str(port): None},
                    command=command,
                    detach=True,
                    auto_remove=True,
                    cap_drop=["ALL"],
                    security_opt=["no-new-privileges:true"],
                    pids_limit=256,
                    environment={
                        "CHALLENGE_ID": chal_id,
                        "TEAM_ID": team_id,
                        "USER_ID": user_id,
                        **(extra_env or {}),
                    },
                    **kwargs,
                )
                return container, ctx
            except docker.errors.ImageNotFound:
                raise ContainerException("docker image not found")
            except docker.errors.DockerException:
                self._clear_thread_local_client(ctx)
                raise

        if context_name:
            if context_name not in self._context_configs:
                raise ContainerException(f"docker context '{context_name}' not available")

            self._acquire_semaphore(context_name)
            try:
                return self._submit(_do_create, context_name)
            except docker.errors.DockerException as e:
                raise ContainerException(f"failed to create container: {e}")
            finally:
                self._release_semaphore(context_name)
        else:
            tried_contexts: set[str] = set()
            last_error = None

            while len(tried_contexts) < len(self._context_configs):
                selected_context = self.get_next_context()

                if selected_context in tried_contexts:
                    continue

                tried_contexts.add(selected_context)

                if selected_context not in self._context_configs:
                    continue

                self._acquire_semaphore(selected_context)
                try:
                    return self._submit(_do_create, selected_context)
                except docker.errors.ImageNotFound:
                    raise ContainerException("docker image not found")
                except docker.errors.DockerException as e:
                    last_error = e
                    continue
                finally:
                    self._release_semaphore(selected_context)

            raise ContainerException(f"failed to create container on any context: {last_error}")

    @run_command
    def get_container_port(self, container_id: str, context_name: str | None = None) -> str | None:
        def _get_port(cid, ctx):
            client = self._get_client(ctx)
            try:
                container = client.containers.get(cid)
                ports = container.attrs["NetworkSettings"]["Ports"]
                for port_mappings in ports.values():
                    if port_mappings:
                        return port_mappings[0]["HostPort"]
            except (KeyError, IndexError, docker.errors.NotFound):
                return None
            except docker.errors.DockerException:
                self._clear_thread_local_client(ctx)
                raise
            return None

        if context_name and context_name in self._context_configs:
            try:
                return self._submit(_get_port, container_id, context_name)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self._context_configs:
            try:
                result = self._submit(_get_port, container_id, ctx)
                if result is not None:
                    return result
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return None

    @run_command
    def get_images(self) -> list:
        def _list_images(ctx):
            client = self._get_client(ctx)
            tags = []
            try:
                images = client.images.list()
                for image in images:
                    for tag in image.tags:
                        if tag:
                            tags.append(tag)
            except docker.errors.DockerException:
                pass
            return tags

        images_by_context: dict[str, list[str]] = {}
        for context_name in self._context_configs:
            try:
                tags = self._submit(_list_images, context_name)
                for tag in tags:
                    if tag not in images_by_context:
                        images_by_context[tag] = []
                    images_by_context[tag].append(context_name)
            except Exception:
                continue

        result = []
        for image, contexts in sorted(images_by_context.items()):
            if len(contexts) == 1:
                result.append(image)
            else:
                for context in contexts:
                    result.append(f"{image} ({context})")

        return result

    @run_command
    def get_images_for_context(self, context_name: str) -> list:
        if context_name not in self._context_configs:
            return []

        def _list(ctx):
            client = self._get_client(ctx)
            result = []
            try:
                images = client.images.list()
                for image in images:
                    for tag in image.tags:
                        if tag:
                            result.append(tag)
            except docker.errors.DockerException:
                pass
            return sorted(result)

        try:
            return self._submit(_list, context_name)
        except Exception:
            return []

    @run_command
    def kill_container(self, container_id: str, context_name: str | None = None):
        def _kill(cid, ctx):
            client = self._get_client(ctx)
            try:
                container = client.containers.get(cid)
                container.kill()
                return True
            except docker.errors.NotFound:
                return False
            except docker.errors.DockerException:
                self._clear_thread_local_client(ctx)
                raise

        if context_name and context_name in self._context_configs:
            try:
                self._submit(_kill, container_id, context_name)
                return
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self._context_configs:
            try:
                if self._submit(_kill, container_id, ctx):
                    return
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue

    @run_command
    def pull_image(self, image: str, context_name: str | None = None) -> dict:
        def _pull(ctx):
            client = self._get_client(ctx)
            try:
                client.images.pull(image)
                return "ok"
            except docker.errors.DockerException as e:
                self._clear_thread_local_client(ctx)
                return f"failed: {e}"
            except Exception as e:
                return f"failed: {e}"

        results = {}
        targets = [context_name] if context_name else list(self._context_configs)
        for ctx in targets:
            if ctx not in self._context_configs:
                results[ctx] = "failed: context not available"
                continue
            try:
                results[ctx] = self._submit(_pull, ctx)
            except Exception as e:
                results[ctx] = f"failed: {e}"
        return results

    def is_connected(self) -> bool:
        if not self._context_configs:
            return False

        def _ping(ctx):
            client = self._get_client(ctx)
            client.ping()
            return True

        for ctx in self._context_configs:
            try:
                return self._submit(_ping, ctx)
            except Exception:
                continue
        return False

    def reload_settings(self):
        from .utils import get_setting

        new_size = get_setting("thread_pool_size", 4)

        if _HAS_GEVENT:
            if self._pool is None or self._pool.size != new_size:
                self._pool = _GeventPool(maxsize=new_size)
        else:
            from concurrent.futures import ThreadPoolExecutor

            if self._pool is not None:
                self._pool.shutdown(wait=False)
            self._pool = ThreadPoolExecutor(max_workers=new_size)

        self._init_semaphores()
