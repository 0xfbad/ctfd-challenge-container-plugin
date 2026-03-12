from __future__ import annotations

import atexit
import time
import json
import random
import socket
import os
from typing import Any

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
import docker
import paramiko
import requests

from CTFd.models import db
from .models import ContainerInfoModel, DockerContextModel

PORT_RANGE_MIN = 1024
PORT_RANGE_MAX = 65536
CPU_QUOTA_BASE = 100000
DEFAULT_CONTEXT_NAME = "default"


class ContainerException(Exception):
    def __init__(self, *args):
        super().__init__(*args)
        self.message = args[0] if args else "unknown container exception"

    def __str__(self):
        return self.message


class ContainerManager:
    def __init__(self, settings, app):
        self.settings = settings
        self.app = app
        self.clients = {}
        self.weighted_contexts = []
        self.context_index = 0

        try:
            self.initialize_connection()
        except ContainerException:
            print("docker could not initialize or connect")

    def initialize_connection(self):
        import threading

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
        new_clients = {}
        new_weighted_contexts = []

        try:
            contexts = DockerContextModel.query.filter_by(enabled=True).all()
        except Exception as e:
            print(f"could not query docker contexts (database may need migration): {e}")
            contexts = []

        try:
            client = docker.from_env()
            client.ping()
            new_clients[DEFAULT_CONTEXT_NAME] = client
            new_weighted_contexts.append("default")
        except (
            docker.errors.DockerException,
            paramiko.ssh_exception.SSHException,
            requests.exceptions.RequestException,
        ) as e:
            print(f"could not connect to default docker context: {e}")

        for context in contexts:
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

                client = docker.DockerClient(base_url=docker_endpoint)
                client.ping()
                new_clients[context.context_name] = client

                for _ in range(context.weight):
                    new_weighted_contexts.append(context.context_name)
            except (
                docker.errors.DockerException,
                paramiko.ssh_exception.SSHException,
                requests.exceptions.RequestException,
            ) as e:
                print(f"could not connect to docker context '{context.context_name}': {e}")

        if not new_clients:
            print("no docker contexts available, containers will not work until contexts are configured")

        self.clients = new_clients
        self.weighted_contexts = new_weighted_contexts

    def get_next_context(self):
        if not self.weighted_contexts:
            raise ContainerException("no docker contexts available")

        context_name = self.weighted_contexts[self.context_index]
        self.context_index = (self.context_index + 1) % len(self.weighted_contexts)
        return context_name

    def setup_expiration_scheduler(self):
        expiration_check_interval = 5

        self.expiration_scheduler = BackgroundScheduler()
        self.expiration_scheduler.add_job(
            func=self.kill_expired_containers,
            args=(self.app,),
            trigger="interval",
            seconds=expiration_check_interval,
        )
        self.expiration_scheduler.start()

        atexit.register(lambda: self.expiration_scheduler.shutdown())

    def _is_port_available(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.bind(("0.0.0.0", port))
                return True
            except OSError:
                return False

    def _allocate_random_port(self):
        selectable_port_range = list(range(PORT_RANGE_MIN, PORT_RANGE_MAX))
        random.shuffle(selectable_port_range)

        for external_port in selectable_port_range:
            if self._is_port_available(external_port):
                return external_port

        raise ContainerException("no available port found")

    def run_command(func: Any) -> Any:
        def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not self.clients:
                try:
                    self.initialize_connection()
                except ContainerException:
                    raise ContainerException("docker is not connected")

            if not self.clients:
                raise ContainerException("no docker contexts available")

            return func(self, *args, **kwargs)

        return wrapper

    @run_command
    def kill_expired_containers(self, app: Flask):
        with app.app_context():
            from .event_logger import event_logger

            containers = ContainerInfoModel.query.all()
            for container in containers:
                if container.expires < int(time.time()):
                    try:
                        self.kill_container(container.container_id, container.docker_context)
                    except ContainerException:
                        print("[container expiry job] docker is not initialized, please check your settings")

                    challenge_id = container.challenge_id
                    challenge_name = container.challenge.name if container.challenge else None
                    user_id = container.user_id
                    user_name = container.user.name if container.user else None
                    team_id = container.team_id
                    team_name = container.team.name if container.team else None

                    event_logger.log_event(
                        event_type="expired",
                        container_id=container.container_id,
                        challenge_id=challenge_id,
                        challenge_name=challenge_name,
                        user_id=user_id,
                        user_name=user_name,
                        team_id=team_id,
                        team_name=team_name,
                        message=f"container expired for {challenge_name}",
                    )

                    db.session.delete(container)
                    db.session.commit()

    @run_command
    def is_container_running(self, container_id: str, context_name: str | None = None) -> bool:
        if context_name and context_name in self.clients:
            client = self.clients[context_name]
        else:
            for client in self.clients.values():
                try:
                    container = client.containers.get(container_id)
                    return container.status == "running"
                except docker.errors.NotFound:
                    continue
            return False

        try:
            container = client.containers.get(container_id)
            return container.status == "running"
        except docker.errors.NotFound:
            return False
        except docker.errors.DockerException as e:
            raise ContainerException(f"docker error: {e}")

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

        external_port = self._allocate_random_port()

        if context_name:
            if context_name not in self.clients:
                raise ContainerException(f"docker context '{context_name}' not available")

            client = self.clients[context_name]

            try:
                container = client.containers.run(
                    image,
                    ports={str(port): str(external_port)},
                    command=command,
                    detach=True,
                    auto_remove=True,
                    environment={
                        "CHALLENGE_ID": chal_id,
                        "TEAM_ID": team_id,
                        "USER_ID": user_id,
                    },
                    **kwargs,
                )
                return container, context_name
            except docker.errors.ImageNotFound:
                raise ContainerException("docker image not found")
            except docker.errors.DockerException as e:
                raise ContainerException(f"failed to create container: {e}")
        else:
            tried_contexts: set[str] = set()
            last_error = None

            while len(tried_contexts) < len(self.clients):
                selected_context = self.get_next_context()

                if selected_context in tried_contexts:
                    continue

                tried_contexts.add(selected_context)
                client = self.clients.get(selected_context)

                if not client:
                    continue

                try:
                    container = client.containers.run(
                        image,
                        ports={str(port): str(external_port)},
                        command=command,
                        detach=True,
                        auto_remove=True,
                        environment={
                            "CHALLENGE_ID": chal_id,
                            "TEAM_ID": team_id,
                            "USER_ID": user_id,
                        },
                        **kwargs,
                    )
                    return container, selected_context
                except docker.errors.ImageNotFound:
                    raise ContainerException("docker image not found")
                except docker.errors.DockerException as e:
                    last_error = e
                    continue

            raise ContainerException(f"failed to create container on any context: {last_error}")

    @run_command
    def get_container_port(self, container_id: str, context_name: str | None = None) -> str | None:
        if context_name and context_name in self.clients:
            client = self.clients[context_name]
        else:
            for client in self.clients.values():
                try:
                    container = client.containers.get(container_id)
                    ports = container.attrs["NetworkSettings"]["Ports"]
                    for port_mappings in ports.values():
                        if port_mappings:
                            return port_mappings[0]["HostPort"]
                except docker.errors.NotFound:
                    continue
            return None

        try:
            container = client.containers.get(container_id)
            ports = container.attrs["NetworkSettings"]["Ports"]
            for port_mappings in ports.values():
                if port_mappings:
                    return port_mappings[0]["HostPort"]
        except (KeyError, IndexError, docker.errors.NotFound):
            return None
        except docker.errors.DockerException as e:
            raise ContainerException(f"docker error: {e}")
        return None

    @run_command
    def get_images(self) -> list:
        images_by_context: dict[str, list[str]] = {}
        for context_name, client in self.clients.items():
            try:
                images = client.images.list()
                for image in images:
                    for tag in image.tags:
                        if tag:
                            if tag not in images_by_context:
                                images_by_context[tag] = []
                            images_by_context[tag].append(context_name)
            except docker.errors.DockerException:
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
        if context_name not in self.clients:
            return []

        client = self.clients[context_name]
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

    @run_command
    def kill_container(self, container_id: str, context_name: str | None = None):
        if context_name and context_name in self.clients:
            client = self.clients[context_name]
        else:
            for client in self.clients.values():
                try:
                    container = client.containers.get(container_id)
                    container.kill()
                    return
                except docker.errors.NotFound:
                    continue
            return

        try:
            container = client.containers.get(container_id)
            container.kill()
        except docker.errors.NotFound:
            pass
        except docker.errors.DockerException as e:
            raise ContainerException(f"docker error: {e}")

    def is_connected(self) -> bool:
        if not self.clients:
            return False

        for client in self.clients.values():
            try:
                client.ping()
                return True
            except docker.errors.DockerException:
                continue
        return False
