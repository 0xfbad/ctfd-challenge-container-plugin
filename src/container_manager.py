import atexit
import sys
import os
import time
import json
import logging

from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers import SchedulerNotRunningError
import docker
from docker.models.containers import Container

from CTFd.models import db
from .models import ContainerInfoModel, ContainerHistoryModel
from .docker_host_manager import DockerHostManager
from .orchestrator import Orchestrator
from .exceptions import ContainerException
from .utils import get_setting
from .event_logger import event_logger

logger = logging.getLogger(__name__)

CPU_QUOTA_BASE = 100000
_SSH_CAPS = ["SYS_CHROOT", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "AUDIT_WRITE"]

_BLOCKED_CAPS = frozenset(
    {
        "SYS_ADMIN",
        "SYS_RAWIO",
        "SYS_MODULE",
        "SYS_PTRACE",
        "NET_RAW",
        "DAC_READ_SEARCH",
        "SYS_BOOT",
        "SYS_TIME",
    }
)

_VOLUME_BLOCKED_PATHS = frozenset(
    {
        "/etc/shadow",
        "/etc/passwd",
        "/etc/sudoers",
        "/proc",
        "/sys",
        "/dev",
        "/var/run",
        "/run",
    }
)

# value types that appear in kwargs dicts forwarded to docker run
_DockerRunVal = str | int | bool | list[str] | dict[str, str] | dict[str, dict[str, str]]


def _build_caps(ctype: str | None, cap_add: str | None) -> list[str]:
    caps = []
    if ctype == "ssh":
        caps.extend(_SSH_CAPS)
    if cap_add:
        for c in cap_add.split(","):
            c = c.strip().upper()
            if c and c not in _BLOCKED_CAPS:
                caps.append(c)
    return list(set(caps)) if caps else []


class ContainerManager:
    def __init__(self, settings: dict[str, str], app: Flask) -> None:
        self.settings = settings
        self.app = app
        self.host_manager = DockerHostManager()
        self.orchestrator = Orchestrator(self.host_manager)

        self.initialize_connection()

    def _ensure_connected(self) -> None:
        if not self.host_manager.has_contexts():
            try:
                self.initialize_connection()
            except ContainerException:
                raise ContainerException("docker is not connected")

            if not self.host_manager.has_contexts():
                raise ContainerException("no docker contexts available")

    def initialize_connection(self) -> None:
        try:
            self.expiration_scheduler.shutdown()
        except (SchedulerNotRunningError, AttributeError):
            pass

        self.load_docker_contexts()
        self.setup_expiration_scheduler()

    def load_docker_contexts(self) -> None:
        self.orchestrator.load_from_db()

    def setup_expiration_scheduler(self) -> None:
        _serving = (
            "gunicorn" in sys.modules
            or os.environ.get("WERKZEUG_RUN_MAIN")
            or (len(sys.argv) > 1 and sys.argv[1] == "run")
        )
        if not _serving:
            logger.info("scheduler skipped (CLI mode)")
            return

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

    def reserve_slot(self, context_name: str) -> None:
        self.orchestrator.reserve_slot(context_name)

    def release_slot(self, context_name: str) -> None:
        self.orchestrator.release_slot(context_name)

    def _dispatch_to_context(self, method_name, context_name, args, kwargs, default=None):
        """Route a host_manager call to a specific context or fan out across all contexts"""
        self._ensure_connected()

        if context_name and context_name in self.host_manager._context_configs:
            try:
                return getattr(self.host_manager, method_name)(context_name, *args, **kwargs)
            except docker.errors.DockerException as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                result = getattr(self.host_manager, method_name)(ctx, *args, **kwargs)
                if result is not None and result != default:
                    return result
            except (docker.errors.NotFound, docker.errors.DockerException):
                continue
        return default

    def is_container_running(self, container_id: str, context_name: str | None = None) -> bool:
        return self._dispatch_to_context("is_container_running", context_name, (container_id,), {}, default=False)

    def get_container_port(self, container_id: str, context_name: str | None = None) -> str | None:
        return self._dispatch_to_context("get_container_port", context_name, (container_id,), {}, default=None)

    def get_running_container_ids(self) -> set[str]:
        self._ensure_connected()

        result = set()
        for ctx in self.host_manager.get_connected_contexts():
            result.update(self.host_manager.get_running_container_ids(ctx))
        return result

    def kill_container(self, container_id: str, context_name: str | None = None) -> None:
        self._dispatch_to_context("kill_container", context_name, (container_id,), {}, default=None)

    def get_container_logs(self, container_id: str, context_name: str | None = None, tail: int = 200) -> str:
        result = self._dispatch_to_context(
            "get_container_logs", context_name, (container_id,), {"tail": tail}, default=""
        )
        return result if result is not None else ""

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
        extra_env: dict[str, str] | None = None,
        ctype: str | None = None,
        cap_add: str | None = None,
    ) -> tuple[Container, str]:
        self._ensure_connected()

        kwargs: dict[str, _DockerRunVal] = {}

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
                    normalized = os.path.normpath(host_path)
                    if "docker.sock" in normalized:
                        raise ContainerException("mounting the docker socket is not allowed")
                    for blocked in _VOLUME_BLOCKED_PATHS:
                        if normalized == blocked or normalized.startswith(blocked + "/"):
                            raise ContainerException(f"mounting {blocked} is not allowed")
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

        caps = _build_caps(ctype, cap_add)
        if caps:
            kwargs["cap_add"] = caps

        if context_name:
            return self._create_on_context(context_name, image, port, command, environment, kwargs)

        return self._create_load_balanced(image, port, command, environment, kwargs)

    def _log_create_error(self, context_name: str, image: str, reason: str) -> None:
        event_logger.log_event(
            "container_error",
            f"{reason} on {context_name}"
            if "not found" in reason
            else f"failed to create container on {context_name}: {reason}",
            level="error",
            metadata={"context_name": context_name, "image": image, "reason": reason},
        )

    def _try_run_on_context(
        self,
        ctx: str,
        image: str,
        port: int,
        command: str,
        environment: dict[str, str | int],
        kwargs: dict[str, _DockerRunVal],
    ) -> tuple[Container, str]:
        self.host_manager.acquire_semaphore(ctx)
        try:
            container = self.host_manager.run_container(ctx, image, port, command, environment, **kwargs)
            return container, ctx
        except docker.errors.ImageNotFound:
            self.orchestrator.release_slot(ctx)
            self._log_create_error(ctx, image, f"image {image} not found")
            raise ContainerException("docker image not found")
        except docker.errors.DockerException as e:
            self.orchestrator.release_slot(ctx)
            self._log_create_error(ctx, image, str(e))
            raise
        finally:
            self.host_manager.release_semaphore(ctx)

    def _create_on_context(
        self,
        context_name: str,
        image: str,
        port: int,
        command: str,
        environment: dict[str, str | int],
        kwargs: dict[str, _DockerRunVal],
    ) -> tuple[Container, str]:
        if context_name not in self.host_manager._context_configs:
            raise ContainerException(f"docker context '{context_name}' not available")

        self.orchestrator.reserve_slot(context_name)
        try:
            return self._try_run_on_context(context_name, image, port, command, environment, kwargs)
        except docker.errors.DockerException as e:
            raise ContainerException(f"failed to create container: {e}")

    def _create_load_balanced(
        self,
        image: str,
        port: int,
        command: str,
        environment: dict[str, str | int],
        kwargs: dict[str, _DockerRunVal],
    ) -> tuple[Container, str]:
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

            try:
                return self._try_run_on_context(selected, image, port, command, environment, kwargs)
            except docker.errors.DockerException as e:
                last_error = e
                continue

        raise ContainerException(f"failed to create container on any context: {last_error}")

    def create_stack(
        self,
        chal_id: int | str,
        team_id: int | str,
        user_id: int | str,
        image: str,
        port: int,
        command: str,
        volumes: str,
        services_json: str | None,
        network_json: str | None,
        max_memory_mb: int | None = None,
        max_cpu: float | None = None,
        context_name: str | None = None,
        extra_env: dict[str, str] | None = None,
        ctype: str | None = None,
        cap_add: str | None = None,
    ) -> tuple[Container, int, list[tuple[str, Container]], str, str]:
        import uuid

        self._ensure_connected()

        services: dict[str, dict[str, str | dict[str, str]]] = json.loads(services_json) if services_json else {}
        network_cfg: dict[str, str | dict[str, str]] = json.loads(network_json) if network_json else {}

        stack_id = uuid.uuid4().hex
        ts = int(time.time())
        base_name = f"chal-u{user_id}-c{chal_id}-{ts}"
        net_name = f"{base_name}-net"

        if context_name:
            if context_name not in self.host_manager._context_configs:
                raise ContainerException(f"docker context '{context_name}' not available")
            self.orchestrator.reserve_slot(context_name)
        else:
            context_name = self.orchestrator.select_and_reserve()
            if context_name is None:
                raise ContainerException("no healthy context available")
        stack_labels = {"ctf.stack_id": stack_id}

        base_env = {
            "CHALLENGE_ID": str(chal_id),
            "TEAM_ID": str(team_id),
            "USER_ID": str(user_id),
            **(extra_env or {}),
        }

        try:
            subnet_raw = network_cfg.get("subnet")
            subnet = str(subnet_raw) if subnet_raw else None
            ips_raw = network_cfg.get("ips", {})
            ips: dict[str, str] = ips_raw if isinstance(ips_raw, dict) else {}

            self.host_manager.create_network(context_name, net_name, subnet=subnet, labels=stack_labels)

            entry_kwargs: dict[str, _DockerRunVal] = {"labels": stack_labels}
            entry_caps = _build_caps(ctype, cap_add)
            if entry_caps:
                entry_kwargs["cap_add"] = entry_caps
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
                **entry_kwargs,  # type: ignore[arg-type]  # mypy can't narrow **dict unpacking
            )

            companions: list[tuple[str, Container]] = []
            for svc_name, svc_cfg in services.items():
                svc_env = dict(base_env)
                svc_env_extra = svc_cfg.get("environment", {})
                if isinstance(svc_env_extra, dict):
                    svc_env.update(svc_env_extra)

                svc_caps: list[str] = []
                svc_cap_add = svc_cfg.get("cap_add")
                if isinstance(svc_cap_add, str) and svc_cap_add:
                    svc_caps = [c.strip() for c in svc_cap_add.split(",") if c.strip()]

                svc_kwargs: dict[str, _DockerRunVal] = {"labels": stack_labels}
                if svc_caps:
                    svc_kwargs["cap_add"] = svc_caps

                svc_image = svc_cfg["image"]
                svc_command_raw = svc_cfg.get("command")
                svc_command = str(svc_command_raw) if isinstance(svc_command_raw, str) else None

                svc_container, _ = self.host_manager.run_container_on_network(
                    context_name,
                    str(svc_image),
                    net_name,
                    f"{base_name}-{svc_name}",
                    svc_command,
                    svc_env,
                    ip_address=ips.get(svc_name),
                    hostname=svc_name,
                    **svc_kwargs,  # type: ignore[arg-type]  # mypy can't narrow **dict unpacking
                )
                companions.append((svc_name, svc_container))

            assert host_port is not None
            return entry_container, host_port, companions, stack_id, context_name

        except Exception:
            try:
                self.host_manager.kill_stack(context_name, stack_id)
            except Exception:
                logger.debug("failed to clean up partial stack %s", stack_id, exc_info=True)
            self.orchestrator.release_slot(context_name)
            raise

    def get_images(self) -> list[str]:
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

    def get_images_for_context(self, context_name: str) -> list[str]:
        self._ensure_connected()

        if context_name not in self.host_manager._context_configs:
            return []

        return self.host_manager.get_images(context_name)

    def pull_image(self, image: str, context_name: str | None = None) -> dict[str, str]:
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

    def get_connected_contexts(self) -> list[str]:
        return self.host_manager.get_connected_contexts()

    def reload_settings(self) -> None:
        max_concurrent = int(get_setting("max_concurrent_creates", 2) or 2)
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
