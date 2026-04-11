from __future__ import annotations

import atexit
import sys
import os
import time
import json
import logging
from typing import Any

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
import docker

from CTFd.models import db
from .models import ContainerInfoModel, ContainerHistoryModel
from .docker_host_manager import DockerHostManager
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

CPU_QUOTA_BASE = 100000


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
        self.host_manager = DockerHostManager()
        self.orchestrator = Orchestrator(self.host_manager)

        try:
            self.initialize_connection()
        except ContainerException:
            logger.error("docker could not initialize or connect")

    def _ensure_connected(self):
        if not self.host_manager.has_contexts():
            try:
                self.initialize_connection()
            except ContainerException:
                raise ContainerException("docker is not connected")

            if not self.host_manager.has_contexts():
                raise ContainerException("no docker contexts available")

    def initialize_connection(self):
        try:
            self.expiration_scheduler.shutdown()
        except (SchedulerNotRunningError, AttributeError):
            pass

        self.load_docker_contexts()
        self.setup_expiration_scheduler()

    def load_docker_contexts(self):
        self.orchestrator.load_from_db()

    def setup_expiration_scheduler(self):
        _serving = (
            "gunicorn" in sys.modules
            or os.environ.get("WERKZEUG_RUN_MAIN")
            or (len(sys.argv) > 1 and sys.argv[1] == "run")
        )
        if not _serving:
            logger.info("scheduler skipped (CLI mode)")
            return

        from .utils import get_setting

        expiration_check_interval = get_setting("expiration_check_interval", 5)

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
            func=self.orchestrator.health_check,
            trigger="interval",
            seconds=30,
            misfire_grace_time=30,
            coalesce=True,
        )
        self.expiration_scheduler.start()

        def _shutdown_scheduler():
            if self.expiration_scheduler.running:
                self.expiration_scheduler.shutdown(wait=False)

        atexit.register(_shutdown_scheduler)

    def select_and_reserve(self):
        return self.orchestrator.select_and_reserve()

    def reserve_slot(self, context_name):
        self.orchestrator.reserve_slot(context_name)

    def release_slot(self, context_name):
        self.orchestrator.release_slot(context_name)

    def is_container_running(self, container_id: str, context_name: str | None = None) -> bool:
        self._ensure_connected()

        if context_name and context_name in self.host_manager._context_configs:
            try:
                return self.host_manager.is_container_running(context_name, container_id)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                if self.host_manager.is_container_running(ctx, container_id):
                    return True
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return False

    def get_container_port(self, container_id: str, context_name: str | None = None) -> str | None:
        self._ensure_connected()

        if context_name and context_name in self.host_manager._context_configs:
            try:
                return self.host_manager.get_container_port(context_name, container_id)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                result = self.host_manager.get_container_port(ctx, container_id)
                if result is not None:
                    return result
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return None

    def get_running_container_ids(self) -> set[str]:
        self._ensure_connected()

        result = set()
        for ctx in self.host_manager.get_connected_contexts():
            result.update(self.host_manager.get_running_container_ids(ctx))
        return result

    def kill_container(self, container_id: str, context_name: str | None = None):
        self._ensure_connected()

        if context_name and context_name in self.host_manager._context_configs:
            try:
                self.host_manager.kill_container(context_name, container_id)
                return
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                if self.host_manager.kill_container(ctx, container_id):
                    return
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue

    def get_container_logs(self, container_id: str, context_name: str | None = None, tail: int = 200) -> str:
        self._ensure_connected()

        if context_name and context_name in self.host_manager._context_configs:
            try:
                return self.host_manager.get_container_logs(context_name, container_id, tail=tail)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                result = self.host_manager.get_container_logs(ctx, container_id, tail=tail)
                if result is not None:
                    return result
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return ""

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
        ctype: str | None = None,
        cap_add: str | None = None,
    ):
        self._ensure_connected()

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
                for host_path in volumes_dict:
                    if "docker.sock" in os.path.normpath(host_path):
                        raise ContainerException("mounting the docker socket is not allowed")
                kwargs["volumes"] = volumes_dict
            except json.decoder.JSONDecodeError:
                raise ContainerException("volumes json string is invalid")

        environment = {
            "CHALLENGE_ID": chal_id,
            "TEAM_ID": team_id,
            "USER_ID": user_id,
            **(extra_env or {}),
        }

        ts = int(time.time())
        container_name = f"chal-u{user_id}-c{chal_id}-{ts}"
        # sets shell prompt to image name instead of container hash
        container_hostname = image.split(":")[0] if image else container_name
        kwargs["name"] = container_name
        kwargs["hostname"] = container_hostname

        # ssh containers need extra capabilities for sshd privilege separation
        caps = []
        if ctype == "ssh":
            caps.extend(["SYS_CHROOT", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "AUDIT_WRITE"])
        if cap_add:
            caps.extend([c.strip() for c in cap_add.split(",") if c.strip()])
        if caps:
            kwargs["cap_add"] = list(set(caps))

        if context_name:
            return self._create_on_context(context_name, image, port, command, environment, kwargs)

        return self._create_load_balanced(image, port, command, environment, kwargs)

    def _create_on_context(self, context_name, image, port, command, environment, kwargs):
        from .event_logger import event_logger

        if context_name not in self.host_manager._context_configs:
            raise ContainerException(f"docker context '{context_name}' not available")

        self.orchestrator.reserve_slot(context_name)
        self.host_manager.acquire_semaphore(context_name)
        try:
            container = self.host_manager.run_container(context_name, image, port, command, environment, **kwargs)
            return container, context_name
        except docker.errors.ImageNotFound:
            self.orchestrator.release_slot(context_name)
            event_logger.log_event(
                "container_error",
                f"image {image} not found on {context_name}",
                level="error",
                metadata={"context_name": context_name, "image": image, "reason": "image not found"},
            )
            raise ContainerException("docker image not found")
        except docker.errors.DockerException as e:
            self.orchestrator.release_slot(context_name)
            event_logger.log_event(
                "container_error",
                f"failed to create container on {context_name}: {e}",
                level="error",
                metadata={"context_name": context_name, "image": image, "reason": str(e)},
            )
            raise ContainerException(f"failed to create container: {e}")
        finally:
            self.host_manager.release_semaphore(context_name)

    def _create_load_balanced(self, image, port, command, environment, kwargs):
        from .event_logger import event_logger

        tried: set[str] = set()
        last_error = None

        while len(tried) < len(self.host_manager._context_configs):
            selected = self.orchestrator.select_and_reserve()
            if selected is None:
                break

            if selected in tried:
                self.orchestrator.release_slot(selected)
                continue

            tried.add(selected)

            if selected not in self.host_manager._context_configs:
                self.orchestrator.release_slot(selected)
                continue

            self.host_manager.acquire_semaphore(selected)
            try:
                container = self.host_manager.run_container(selected, image, port, command, environment, **kwargs)
                return container, selected
            except docker.errors.ImageNotFound:
                self.orchestrator.release_slot(selected)
                event_logger.log_event(
                    "container_error",
                    f"image {image} not found on {selected}",
                    level="error",
                    metadata={"context_name": selected, "image": image, "reason": "image not found"},
                )
                raise ContainerException("docker image not found")
            except docker.errors.DockerException as e:
                self.orchestrator.release_slot(selected)
                last_error = e
                continue
            finally:
                self.host_manager.release_semaphore(selected)

        raise ContainerException(f"failed to create container on any context: {last_error}")

    def create_stack(
        self,
        chal_id,
        team_id,
        user_id,
        image,
        port,
        command,
        volumes,
        services_json,
        network_json,
        max_memory_mb=None,
        max_cpu=None,
        context_name=None,
        extra_env=None,
        ctype=None,
        cap_add=None,
    ):
        import uuid

        self._ensure_connected()

        services = json.loads(services_json) if services_json else {}
        network_cfg = json.loads(network_json) if network_json else {}

        stack_id = uuid.uuid4().hex
        ts = int(time.time())
        base_name = f"chal-u{user_id}-c{chal_id}-{ts}"
        net_name = f"{base_name}-net"

        if context_name:
            if context_name not in self.host_manager._context_configs:
                raise ContainerException(f"docker context '{context_name}' not available")
        else:
            context_name = self.orchestrator.select_and_reserve()
            if context_name is None:
                raise ContainerException("no healthy context available")

        self.orchestrator.reserve_slot(context_name)
        stack_labels = {"ctf.stack_id": stack_id}

        base_env = {
            "CHALLENGE_ID": str(chal_id),
            "TEAM_ID": str(team_id),
            "USER_ID": str(user_id),
            **(extra_env or {}),
        }

        try:
            subnet = network_cfg.get("subnet")
            ips = network_cfg.get("ips", {})

            self.host_manager.create_network(context_name, net_name, subnet=subnet, labels=stack_labels)

            entry_caps = []
            if ctype == "ssh":
                entry_caps.extend(["SYS_CHROOT", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "AUDIT_WRITE"])
            if cap_add:
                entry_caps.extend([c.strip() for c in cap_add.split(",") if c.strip()])

            entry_kwargs: dict[str, Any] = {"labels": stack_labels}
            if entry_caps:
                entry_kwargs["cap_add"] = list(set(entry_caps))
            if max_memory_mb:
                entry_kwargs["mem_limit"] = f"{int(max_memory_mb)}m"
            if max_cpu:
                entry_kwargs["cpu_quota"] = int(float(max_cpu) * 100000)
                entry_kwargs["cpu_period"] = 100000

            entry_hostname = image.split(":")[0] if image else base_name
            entry_container, host_port = self.host_manager.run_container_on_network(
                context_name,
                image,
                net_name,
                base_name,
                command,
                base_env,
                ip_address=ips.get("entry"),
                publish_port=True,
                hostname=entry_hostname,
                internal_port=port,
                **entry_kwargs,
            )

            companions = []
            for svc_name, svc_cfg in services.items():
                svc_env = dict(base_env)
                svc_env.update(svc_cfg.get("environment", {}))

                svc_caps = []
                if svc_cfg.get("cap_add"):
                    svc_caps = [c.strip() for c in svc_cfg["cap_add"].split(",") if c.strip()]

                svc_kwargs: dict[str, Any] = {"labels": stack_labels}
                if svc_caps:
                    svc_kwargs["cap_add"] = svc_caps

                svc_container, _ = self.host_manager.run_container_on_network(
                    context_name,
                    svc_cfg["image"],
                    net_name,
                    f"{base_name}-{svc_name}",
                    svc_cfg.get("command"),
                    svc_env,
                    ip_address=ips.get(svc_name),
                    hostname=svc_name,
                    **svc_kwargs,
                )
                companions.append((svc_name, svc_container))

            return entry_container, host_port, companions, stack_id, context_name

        except Exception:
            try:
                self.host_manager.kill_stack(context_name, stack_id)
            except Exception:
                logger.debug("failed to clean up partial stack %s", stack_id, exc_info=True)
            self.orchestrator.release_slot(context_name)
            raise

    def get_images(self) -> list:
        self._ensure_connected()

        images_by_context: dict[str, list[str]] = {}
        for ctx in self.host_manager.get_connected_contexts():
            for tag in self.host_manager.get_images(ctx):
                if tag not in images_by_context:
                    images_by_context[tag] = []
                images_by_context[tag].append(ctx)

        result = []
        for image, contexts in sorted(images_by_context.items()):
            if len(contexts) == 1:
                result.append(image)
            else:
                for context in contexts:
                    result.append(f"{image} ({context})")

        return result

    def get_images_for_context(self, context_name: str) -> list:
        self._ensure_connected()

        if context_name not in self.host_manager._context_configs:
            return []

        return self.host_manager.get_images(context_name)

    def pull_image(self, image: str, context_name: str | None = None) -> dict:
        self._ensure_connected()

        results = {}
        targets = [context_name] if context_name else self.host_manager.get_connected_contexts()
        for ctx in targets:
            if ctx not in self.host_manager._context_configs:
                results[ctx] = "failed: context not available"
                continue
            try:
                results[ctx] = self.host_manager.pull_image(ctx, image)
            except Exception as e:
                results[ctx] = f"failed: {e}"
        return results

    def is_connected(self) -> bool:
        if not self.host_manager.has_contexts():
            return False

        for ctx in self.host_manager.get_connected_contexts():
            if self.host_manager.ping(ctx):
                return True
        return False

    def get_connected_contexts(self):
        return self.host_manager.get_connected_contexts()

    def reload_settings(self):
        from .utils import get_setting

        max_concurrent = get_setting("max_concurrent_creates", 2)
        self.host_manager._init_semaphores(max_concurrent)

    def kill_expired_containers(self, app: Flask):
        if not self.host_manager.has_contexts():
            try:
                self.initialize_connection()
            except ContainerException:
                return

            if not self.host_manager.has_contexts():
                return

        with app.app_context():
            from .event_logger import event_logger

            # only check entry containers (or standalone containers without stack_id)
            entries = ContainerInfoModel.query.filter(
                db.or_(ContainerInfoModel.is_entry == True, ContainerInfoModel.stack_id.is_(None))  # noqa: E712
            ).all()
            killed_rows = []
            released_stacks = set()

            for container in entries:
                if container.expires >= int(time.time()):
                    continue

                try:
                    if container.stack_id:
                        self.host_manager.kill_stack(container.docker_context, container.stack_id)
                    else:
                        self.kill_container(container.container_id, container.docker_context)
                except ContainerException:
                    logger.warning("expiry job: docker is not initialized")
                    continue

                self.release_slot(container.docker_context)

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

                if container.stack_id and container.stack_id not in released_stacks:
                    siblings = ContainerInfoModel.query.filter_by(stack_id=container.stack_id).all()
                    killed_rows.extend(siblings)
                    released_stacks.add(container.stack_id)
                else:
                    killed_rows.append(container)

            for row in killed_rows:
                history = ContainerHistoryModel.query.filter_by(container_id=row.container_id).first()
                if history:
                    history.stopped_at = time.time()
                    if not history.reason:
                        history.reason = "expired"
                db.session.delete(row)
            if killed_rows:
                db.session.commit()
