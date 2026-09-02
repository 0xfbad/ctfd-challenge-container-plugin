import atexit
import fcntl
import hashlib
import json
import logging
import math
import os
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import docker
import paramiko
from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from docker.models.containers import Container
from flask import Flask
from sqlalchemy import text

from CTFd.models import db

from .coordination import InstanceCoordinator
from .docker_host_manager import DockerHostManager, ReconcileEntry, _DockerRunVal
from .event_logger import event_logger
from .exceptions import ContainerException, ContainerUnavailableException
from .models import ContainerInfoModel, ContainerInstanceModel, ContainerMaintenanceModel
from .orchestrator import Orchestrator
from .utils import get_setting

logger = logging.getLogger(__name__)

CPU_QUOTA_BASE = 100000

NAME_PREFIX = "chal-"

_SSH_CAPS = ["SYS_CHROOT", "SETUID", "SETGID", "CHOWN", "DAC_OVERRIDE", "AUDIT_WRITE"]
_RESERVATION_ID = re.compile(r"^[0-9a-f]{32}$")
_MAINTENANCE_LOCK_PREFIX = "ctfd-challenge-containers"


@contextmanager
def _maintenance_lock(app: Flask, job_name: str) -> Iterator[bool]:
    connection = None
    lock_file = None
    dialect = ""
    lock_name = f"{_MAINTENANCE_LOCK_PREFIX}:{job_name}"
    lock_key = int.from_bytes(hashlib.sha256(lock_name.encode()).digest()[:8], "big", signed=True)
    acquired = False
    try:
        with app.app_context():
            engine = db.engine
            dialect = engine.dialect.name
            if dialect in {"mysql", "mariadb", "postgresql"}:
                connection = engine.connect()

        if dialect in {"mysql", "mariadb"}:
            assert connection is not None
            acquired = bool(connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": lock_name}).scalar())
        elif dialect == "postgresql":
            assert connection is not None
            acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar())
        elif dialect == "sqlite":
            lock_path = os.path.join(tempfile.gettempdir(), f"{lock_name}.lock")
            lock_file = open(lock_path, "a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                acquired = False
        else:
            logger.error("maintenance disabled for unsupported database dialect %s", dialect)

        yield acquired
    finally:
        if acquired and connection is not None:
            try:
                if dialect in {"mysql", "mariadb"}:
                    connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})
                elif dialect == "postgresql":
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
            except Exception:
                logger.warning("failed to release maintenance lock %s", job_name, exc_info=True)
        if connection is not None:
            connection.close()
        if lock_file is not None:
            try:
                if acquired:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _valid_reservation_identity(value: str | None) -> bool:
    return bool(value and _RESERVATION_ID.fullmatch(value))


def container_name(user_id: int | str, chal_id: int | str, ts: int, nonce: str | None = None) -> str:
    suffix = nonce[:12] if nonce else str(ts)
    return f"{NAME_PREFIX}u{user_id}-c{chal_id}-{suffix}"


def _resource_kwargs(max_memory_mb: int | None, max_cpu: float | None) -> dict[str, _DockerRunVal]:
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
        except ValueError:
            raise ContainerException("cpu limit must be a positive number")

        if not math.isfinite(cpu_quota) or cpu_quota <= 0:
            raise ContainerException("cpu limit must be a positive number")

        kwargs["cpu_quota"] = int(cpu_quota * CPU_QUOTA_BASE)
        kwargs["cpu_period"] = CPU_QUOTA_BASE

    return kwargs


# no-new-privileges does not drop granted caps so keep this allowlist minimal
_ALLOWED_CAPS = frozenset({"NET_ADMIN", "NET_RAW", "SYS_PTRACE", "SYS_NICE"})


def _filter_admin_caps(cap_add: str | None, chal_id: int | str | None = None) -> list[str]:
    if not cap_add:
        return []
    safe: list[str] = []
    for c in cap_add.split(","):
        c = c.strip().upper()
        if not c:
            continue
        if c in _ALLOWED_CAPS:
            safe.append(c)
        else:
            logger.warning("dropping disallowed cap %r for challenge %s", c, chal_id)
    return safe


def _build_caps(ctype: str | None, cap_add: str | None, chal_id: int | str | None = None) -> list[str]:
    caps: list[str] = []
    if ctype == "ssh":
        caps.extend(_SSH_CAPS)
    caps.extend(_filter_admin_caps(cap_add, chal_id))
    return list(set(caps)) if caps else []


class ContainerManager:
    def __init__(self, settings: dict[str, str], app: Flask) -> None:
        self.settings = settings
        self.app = app
        self.host_manager = DockerHostManager()
        self.orchestrator = Orchestrator(self.host_manager)

        self.initialize_connection()

    def _ensure_connected(self) -> None:
        if self.host_manager.has_contexts():
            return

        # reload contexts only, initialize_connection would tear down the expiration scheduler from a request greenlet and apscheduler cannot join it
        try:
            self.load_docker_contexts()
        except ContainerException:
            raise ContainerUnavailableException("docker is not connected")

        if not self.host_manager.has_contexts():
            raise ContainerUnavailableException("no docker contexts available")

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

        self.expiration_scheduler = BackgroundScheduler()
        self.expiration_scheduler.add_job(
            func=self._maintenance_tick,
            trigger="interval",
            seconds=1,
            misfire_grace_time=30,
            coalesce=True,
            max_instances=1,
        )
        self.expiration_scheduler.start()

        def _shutdown_scheduler():
            if self.expiration_scheduler.running:
                self.expiration_scheduler.shutdown(wait=False)

        atexit.register(_shutdown_scheduler)

    def _run_maintenance_job(self, name: str, interval: int, operation: Callable[[], None]) -> bool:
        try:
            with _maintenance_lock(self.app, name) as acquired:
                if not acquired:
                    return False
                with self.app.app_context():
                    now = time.time()
                    state = ContainerMaintenanceModel.query.filter_by(name=name).first()
                    if state is not None and state.last_started > now - interval:
                        return False
                    if state is None:
                        db.session.add(ContainerMaintenanceModel(name=name, last_started=now))
                    else:
                        state.last_started = now
                    db.session.commit()
                operation()
                return True
        except Exception:
            with self.app.app_context():
                db.session.rollback()
            logger.exception("maintenance job %s failed", name)
            return False

    def _maintenance_tick(self) -> None:
        with self.app.app_context():
            expiry_interval = int(get_setting("expiration_check_interval", 5) or 5)
        self._run_maintenance_job("expiry", expiry_interval, lambda: self.kill_expired_containers(self.app))

        def health_check() -> None:
            with self.app.app_context():
                try:
                    self.orchestrator.health_check()
                finally:
                    db.session.remove()

        self._run_maintenance_job("health", 30, health_check)

    def _dispatch_to_context(
        self,
        method_name: str,
        context_name: str | None,
        args: tuple[object, ...],
        kwargs: dict[str, object],
        default: object = None,
    ):
        self._ensure_connected()

        if context_name is not None:
            if context_name not in self.host_manager._context_configs:
                raise ContainerUnavailableException(f"docker context '{context_name}' is not configured")
            try:
                return getattr(self.host_manager, method_name)(context_name, *args, **kwargs)
            except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
                raise ContainerException(f"docker error: {e}")

        for ctx in self.host_manager.get_connected_contexts():
            try:
                result = getattr(self.host_manager, method_name)(ctx, *args, **kwargs)
                if result is not None and result != default:
                    return result
            except (docker.errors.NotFound, docker.errors.DockerException, paramiko.ssh_exception.SSHException):
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
        max_memory_mb: int,
        max_cpu: float,
        context_name: str,
        instance_id: str,
        provision_token: str,
        *,
        extra_env: dict[str, str] | None = None,
        ctype: str | None = None,
        cap_add: str | None = None,
        resolved_volumes: dict[str, dict[str, str]] | None = None,
    ) -> tuple[Container, str]:
        self._ensure_connected()

        if not _valid_reservation_identity(instance_id) or not _valid_reservation_identity(provision_token):
            raise ContainerException("managed reservations require valid instance and provision identities")
        if context_name not in self.host_manager.get_connected_contexts():
            raise ContainerException("reserved docker context is not reachable")

        kwargs: dict[str, _DockerRunVal] = _resource_kwargs(max_memory_mb, max_cpu)

        if resolved_volumes:
            kwargs["volumes"] = resolved_volumes

        kwargs["labels"] = {
            "ctf.instance_id": instance_id,
            "ctf.provision_token": provision_token,
        }

        environment = {
            "CHALLENGE_ID": chal_id,
            "TEAM_ID": team_id,
            "USER_ID": user_id,
            **(extra_env or {}),
        }

        ts = int(time.time())
        name = container_name(user_id, chal_id, ts, nonce=instance_id)
        kwargs["name"] = name
        kwargs["hostname"] = name

        caps = _build_caps(ctype, cap_add, chal_id)
        if caps:
            kwargs["cap_add"] = caps

        return self._try_run_on_context(context_name, image, port, command, environment, kwargs)

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
        try:
            container = self.host_manager.run_container(ctx, image, port, command, environment, **kwargs)
            return container, ctx
        except docker.errors.ImageNotFound:
            self._log_create_error(ctx, image, f"image {image} not found")
            raise ContainerException("docker image not found")
        except (docker.errors.DockerException, paramiko.ssh_exception.SSHException) as e:
            self._log_create_error(ctx, image, str(e))
            raise

    def create_stack(
        self,
        chal_id: int | str,
        team_id: int | str,
        user_id: int | str,
        image: str,
        port: int,
        command: str,
        services_json: str | None,
        network_json: str | None,
        max_memory_mb: int,
        max_cpu: float,
        context_name: str,
        instance_id: str,
        provision_token: str,
        *,
        extra_env: dict[str, str] | None = None,
        ctype: str | None = None,
        cap_add: str | None = None,
        entry_volumes: dict[str, dict[str, str]] | None = None,
        service_volumes: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> tuple[Container, int, list[tuple[str, Container]], str, str]:
        self._ensure_connected()

        if not _valid_reservation_identity(instance_id) or not _valid_reservation_identity(provision_token):
            raise ContainerException("managed reservations require valid instance and provision identities")
        if context_name not in self.host_manager.get_connected_contexts():
            raise ContainerException("reserved docker context is not reachable")

        services: dict[str, dict[str, str | dict[str, str]]] = json.loads(services_json) if services_json else {}
        network_cfg: dict[str, str | dict[str, str]] = json.loads(network_json) if network_json else {}

        stack_id = uuid.uuid4().hex
        ts = int(time.time())
        base_name = container_name(user_id, chal_id, ts, nonce=instance_id)
        net_name = f"{base_name}-net"

        stack_labels = {
            "ctf.stack_id": stack_id,
            "ctf.instance_id": instance_id,
            "ctf.provision_token": provision_token,
        }

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
            if entry_volumes:
                entry_kwargs["volumes"] = entry_volumes
            entry_caps = _build_caps(ctype, cap_add, chal_id)
            if entry_caps:
                entry_kwargs["cap_add"] = entry_caps
            entry_kwargs.update(_resource_kwargs(max_memory_mb, max_cpu))

            entry_container, host_port = self.host_manager.run_container_on_network(
                context_name,
                image,
                net_name,
                base_name,
                command,
                base_env,
                ip_address=ips.get("entry"),
                publish_port=True,
                hostname=base_name,
                internal_port=port,
                **entry_kwargs,
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
                    svc_caps = _filter_admin_caps(svc_cap_add, chal_id)

                svc_kwargs: dict[str, _DockerRunVal] = {"labels": stack_labels}
                if svc_caps:
                    svc_kwargs["cap_add"] = svc_caps
                resolved_service_volumes = (service_volumes or {}).get(svc_name)
                if svc_cfg.get("volumes") and resolved_service_volumes is None:
                    raise ContainerException(f"service {svc_name} volumes must pass named-volume policy validation")
                if resolved_service_volumes:
                    svc_kwargs["volumes"] = resolved_service_volumes
                service_memory = svc_cfg.get("max_memory_mb", max_memory_mb)
                service_cpu = svc_cfg.get("max_cpu", max_cpu)
                if isinstance(service_memory, bool) or (
                    service_memory is not None and not isinstance(service_memory, int)
                ):
                    raise ContainerException(f"service {svc_name} has an invalid memory limit")
                if isinstance(service_cpu, bool) or (
                    service_cpu is not None and not isinstance(service_cpu, (int, float))
                ):
                    raise ContainerException(f"service {svc_name} has an invalid CPU limit")
                svc_kwargs.update(
                    _resource_kwargs(
                        service_memory,
                        float(service_cpu) if service_cpu is not None else None,
                    )
                )

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
                    **svc_kwargs,  # type: ignore[arg-type]  # mypy cannot narrow dict unpacking
                )
                companions.append((svc_name, svc_container))

            assert host_port is not None
            return entry_container, host_port, companions, stack_id, context_name

        except Exception:
            try:
                self.host_manager.kill_stack(context_name, stack_id)
            except Exception:
                logger.warning(
                    "failed to clean up partial stack %s (may leak until reconcile)", stack_id, exc_info=True
                )
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

    def kill_expired_containers(self, app: Flask) -> None:
        with app.app_context():
            try:
                self._kill_expired_containers_inner()
            finally:
                # flask-sqlalchemy teardown only fires for request contexts so a manual app context leaks the scoped session connection
                db.session.remove()

    def _kill_expired_containers_inner(self) -> None:
        if not self.host_manager.has_contexts():
            # reload from db only, initialize_connection tears down the scheduler running this job and apscheduler cannot join the current thread
            try:
                self.load_docker_contexts()
            except ContainerException:
                return

        if not self.host_manager.has_contexts():
            return

        post_solve_expiry = int(get_setting("post_solve_expiry_seconds", 0) or 0)
        if post_solve_expiry > 0:
            try:
                InstanceCoordinator.reconcile_solved_instances(post_solve_expiry)
            except Exception:
                logger.exception("maintenance could not reconcile durable solves")

        now = int(time.time())
        logical_instances = ContainerInstanceModel.query.filter(
            db.or_(
                db.and_(ContainerInstanceModel.state == "running", ContainerInstanceModel.expires < now),
                db.and_(
                    ContainerInstanceModel.state == "cleanup_pending",
                    ContainerInstanceModel.updated_at < now - self.RECONCILE_SAFETY_AGE_SECONDS,
                ),
                db.and_(
                    ContainerInstanceModel.state == "provisioning",
                    ContainerInstanceModel.provision_deadline.isnot(None),
                    ContainerInstanceModel.provision_deadline < now - self.RECONCILE_SAFETY_AGE_SECONDS,
                ),
            )
        ).all()

        for instance in logical_instances:
            operation_token = InstanceCoordinator.claim_operation(
                instance.id,
                ("running", "cleanup_pending", "provisioning"),
                "cleanup_pending",
            )
            if operation_token is None:
                continue
            if instance.docker_context is None:
                logger.error(
                    "instance %s has no Docker context; retaining it for automatic reconciliation",
                    instance.id,
                )
                InstanceCoordinator.release_operation(
                    instance.id, operation_token, "configured Docker context is missing"
                )
                continue
            context_name = instance.docker_context.context_name
            entry = ContainerInfoModel.query.filter_by(instance_id=instance.id, is_entry=True).first()
            entry_metadata = None
            if entry is not None:
                entry_metadata = {
                    "container_id": entry.container_id,
                    "challenge_id": entry.challenge_id,
                    "challenge_name": entry.challenge.name if entry.challenge else None,
                    "user_id": entry.user_id,
                    "username": entry.user.name if entry.user else None,
                    "team_id": entry.team_id,
                    "team_name": entry.team.name if entry.team else None,
                }
            reason = "expired" if instance.expires < now else "reconciled"
            try:
                self.host_manager.force_remove_resources_by_label(context_name, f"ctf.instance_id={instance.id}")
            except Exception as error:
                logger.warning("maintenance could not prove cleanup for instance %s", instance.id, exc_info=True)
                InstanceCoordinator.release_operation(instance.id, operation_token, str(error))
                continue

            if not InstanceCoordinator.delete_after_confirmed_cleanup(
                instance.id,
                operation_token=operation_token,
                reason=reason,
                stopped_at=time.time(),
            ):
                continue
            if entry_metadata is not None:
                event_logger.log_event(
                    reason,
                    f"container {reason} for {entry_metadata['challenge_name'] or 'unknown'}",
                    user_id=entry_metadata["user_id"],
                    username=entry_metadata["username"],
                    metadata={
                        "container_id": entry_metadata["container_id"],
                        "challenge_id": entry_metadata["challenge_id"],
                        "challenge_name": entry_metadata["challenge_name"],
                        "team_id": entry_metadata["team_id"],
                        "team_name": entry_metadata["team_name"],
                    },
                )

        self._reconcile_orphans()

    # a docker or ssh outage between container creation and db commit leaves labeled resources with no logical instance
    RECONCILE_INSTANCE_LABEL = "ctf.instance_id"
    RECONCILE_SAFETY_AGE_SECONDS = 300

    def _reconcile_orphans(self) -> None:
        active_instance_ids = {
            row.id for row in ContainerInstanceModel.query.with_entities(ContainerInstanceModel.id).all()
        }
        now = time.time()

        for ctx_name in self.host_manager.get_connected_contexts():
            try:
                entries: list[ReconcileEntry] = self.host_manager.list_containers_by_label(
                    ctx_name, self.RECONCILE_INSTANCE_LABEL
                )
            except Exception as e:
                logger.warning(f"reconcile: list by label failed on {ctx_name}: {e}")
                continue

            oldest_by_instance: dict[str, tuple[float, str]] = {}
            for entry in entries:
                instance_id = str(entry.get("instance_id", ""))
                name = str(entry.get("name", ""))
                if not _valid_reservation_identity(instance_id) or instance_id in active_instance_ids:
                    continue
                created_ts = float(entry.get("created_ts", 0) or 0)
                current = oldest_by_instance.get(instance_id)
                if current is None or (created_ts > 0 and created_ts < current[0]):
                    oldest_by_instance[instance_id] = (created_ts, name)

            for instance_id, (created_ts, name) in oldest_by_instance.items():
                # unknown timestamp reads as age 0 so the orphan is retained
                age = now - created_ts if created_ts > 0 else 0
                if age < self.RECONCILE_SAFETY_AGE_SECONDS:
                    continue

                logger.warning(
                    "reconcile: removing orphan instance %s (%s) on %s (age %ss)",
                    instance_id,
                    name,
                    ctx_name,
                    int(age),
                )
                try:
                    self.host_manager.force_remove_resources_by_label(
                        ctx_name, f"{self.RECONCILE_INSTANCE_LABEL}={instance_id}"
                    )
                    event_logger.log_event(
                        "orphan_reaped",
                        f"reaped orphan instance {instance_id} on {ctx_name}",
                        level="warning",
                        metadata={
                            "context": ctx_name,
                            "container_name": name,
                            "instance_id": instance_id,
                            "age_seconds": int(age),
                        },
                    )
                except Exception as e:
                    logger.error(f"reconcile: failed to remove instance {instance_id} on {ctx_name}: {e}")
