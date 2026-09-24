from __future__ import annotations

import logging
import time
from functools import wraps
from typing import Any

from flask import current_app, g, request

from CTFd.utils.user import is_admin

from ..challenge_config import normalize_services
from ..container_manager import ContainerManager
from ..coordination import (
    ContextUnavailable,
    CoordinationError,
    CreateCapacityUnavailable,
    FinalizedInstance,
    InstanceCoordinator,
    InstanceQuotaExceeded,
    InstanceReservation,
    PhysicalMember,
)
from ..docker_host_manager import LOCAL_CONTEXT_NAME
from ..event_logger import event_logger
from ..exceptions import ContainerException
from ..freshness import compute_token
from ..messages import (
    AWAITING_CLEANUP,
    CHALLENGE_LOCKED,
    CHALLENGE_MISCONFIGURED,
    CHALLENGE_NOT_FOUND,
    CHALLENGE_UNAVAILABLE,
    CLEANUP_ALREADY_RUNNING,
    CLEANUP_FINALIZING,
    CLEANUP_IN_PROGRESS,
    CLEANUP_PENDING,
    CONTAINER_NOT_FOUND,
    CONTAINER_NOT_FOUND_RESET,
    CONTEXT_UNAVAILABLE,
    EXPIRED_NO_RENEW,
    FINALIZATION_FAILED,
    HOST_UNAVAILABLE_CLEANUP,
    HOST_UNREACHABLE,
    HOSTS_BUSY,
    INSTANCE_NOT_FOUND,
    NO_RENEWALS,
    PLACEMENT_FAILED,
    PORT_UNAVAILABLE,
    QUOTA_EXCEEDED,
    REQUEST_IN_PROGRESS,
    SOLVED_NO_RENEW,
)
from ..models import ContainerChallengeModel, ContainerInfoModel, ContainerInstanceModel, DockerContextModel
from ..utils import ValidationError, error_body, get_setting, owner_filter, sanitize_container_error
from ..volume_policy import (
    MountConfigError,
    MountRequest,
    VolumeMetadata,
    VolumePolicy,
    evaluate_volume_readiness,
    load_volume_policy,
    parse_mount_config,
    resolve_mounts_for_context,
)

logger = logging.getLogger(__name__)

JsonResponse = dict[str, str | int | bool | None]


def _resolve_challenge(chal_id: int) -> ContainerChallengeModel | None:
    stashed = getattr(g, "challenge", None)
    if stashed is not None and getattr(stashed, "id", None) == chal_id:
        return stashed

    return ContainerChallengeModel.query.filter_by(id=chal_id).first()  # admin routes lack cached challenges


def request_json() -> dict[str, Any] | None:
    try:
        body = request.get_json(silent=True)  # malformed nested bodies can raise recursion errors
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def requires_visible_challenge(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        chal_id = kwargs.get("chal_id")
        if chal_id is None:
            chal_id = kwargs.get("challenge_id")
        if chal_id is None:
            chal_id = (request_json() or {}).get("chal_id")

        try:
            chal_id = int(chal_id) if chal_id is not None else None
        except (TypeError, ValueError):
            chal_id = None

        if chal_id is None:
            return {"error": CHALLENGE_NOT_FOUND}, 404

        challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()
        if challenge is None:
            return {"error": CHALLENGE_NOT_FOUND}, 404

        admin = is_admin()
        if challenge.state == "hidden" and not admin:
            return {"error": CHALLENGE_NOT_FOUND}, 404

        if challenge.state == "locked" and not admin:
            return {"error": CHALLENGE_LOCKED}, 403

        g.challenge = challenge
        return f(*args, **kwargs)

    return wrapper


def log_container_event(
    event_type: str,
    message: str,
    user_id: int | None = None,
    user_name: str | None = None,
    container_id: str | None = None,
    challenge_id: int | None = None,
    challenge_name: str | None = None,
    team_id: int | None = None,
    team_name: str | None = None,
    docker_context: str | None = None,
) -> None:
    event_logger.log_event(
        event_type=event_type,
        message=message,
        user_id=user_id,
        username=user_name,
        metadata={
            "container_id": container_id,
            "challenge_id": challenge_id,
            "challenge_name": challenge_name,
            "team_id": team_id,
            "team_name": team_name,
            "docker_context": docker_context,
        },
    )


def resolve_expiration(challenge: ContainerChallengeModel) -> int:
    return int(challenge.expiration_seconds or get_setting("default_expiration_seconds", 1800) or 1800)


def resolve_max_renewals(challenge: ContainerChallengeModel) -> int:
    max_renewals = challenge.max_renewals
    if max_renewals is None:  # zero disables renewals
        max_renewals = get_setting("default_max_renewals", 2)

    return int(max_renewals)


def build_connection_response(
    status: str,
    challenge: ContainerChallengeModel,
    container: ContainerInfoModel | FinalizedInstance,
    context_name: str | None,
    *,
    expires: int | None = None,
    renewals_used: int | None = None,
) -> JsonResponse:
    response: JsonResponse = {
        "status": status,
        "hostname": get_hostname_for_context(context_name),
        "port": container.port,
        "connect": challenge.ctype,
        "expires": container.expires if expires is None else expires,
        "renewals_used": container.renewals_used if renewals_used is None else renewals_used,
        "max_renewals": resolve_max_renewals(challenge),
    }
    if challenge.ctype == "ssh":
        response["ssh_username"] = challenge.ssh_username
        response["ssh_password"] = challenge.ssh_password

    return response


def _request_hostname() -> str:
    return request.host.split(":")[0]


def get_hostname_for_context(context_name: str | None) -> str:
    if not context_name or context_name == LOCAL_CONTEXT_NAME:
        return _request_hostname()

    context = DockerContextModel.query.filter_by(context_name=context_name).first()
    if context is None:
        return _request_hostname()

    if context.pub_hostname:
        return context.pub_hostname

    if not context.hostname:
        return _request_hostname()

    return context.hostname.split("@")[-1]  # ssh endpoints can include a username


def _log_request_failed(challenge: ContainerChallengeModel, uid: int, err: Exception) -> None:
    event_logger.log_event(
        "request_failed",
        f"container request failed for {challenge.name}: {err}",
        level="error",
        user_id=uid,
        metadata={
            "challenge_id": challenge.id,
            "challenge_name": challenge.name,
            "reason": str(err),
        },
    )


def cleanup_instance(instance_id: str, *, reason: str = "stopped") -> JsonResponse:
    container_manager = current_app.container_manager
    instance = ContainerInstanceModel.query.filter_by(id=instance_id).first()
    if instance is None:
        return error_body(INSTANCE_NOT_FOUND, "transient")

    if instance.docker_context is None:
        return error_body(CONTEXT_UNAVAILABLE, "transient")

    context_name = instance.docker_context.context_name
    operation_token = InstanceCoordinator.claim_operation(
        instance_id, ("running", "provisioning", "cleanup_pending"), "cleanup_pending"
    )
    if operation_token is None:
        return error_body(CLEANUP_ALREADY_RUNNING, "transient")

    try:
        container_manager.host_manager.force_remove_resources_by_label(context_name, f"ctf.instance_id={instance_id}")
    except Exception as error:
        logger.warning("failed to clean instance %s; retaining cleanup state", instance_id, exc_info=True)
        InstanceCoordinator.release_operation(instance_id, operation_token, str(error))
        return error_body(HOST_UNAVAILABLE_CLEANUP, "transient")

    if not InstanceCoordinator.delete_after_confirmed_cleanup(
        instance_id,
        operation_token=operation_token,
        reason=reason,
        stopped_at=time.time(),
    ):
        return error_body(CLEANUP_FINALIZING, "transient")

    return {"success": "container cleaned"}


def kill_container(container_id: str) -> JsonResponse:
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
    if not container:
        return error_body(CONTAINER_NOT_FOUND, "user")

    context_name = container.docker_context
    challenge_name = container.challenge.name if container.challenge else None
    user_name = container.user.name if container.user else None
    team_name = container.team.name if container.team else None
    audit = {
        "challenge_id": container.challenge_id,
        "user_id": container.user_id,
        "team_id": container.team_id,
        "instance_id": container.instance_id,
    }

    instance_id = str(audit["instance_id"])
    result = cleanup_instance(instance_id)
    if "success" not in result:
        return result

    log_container_event(
        event_type="killed",
        container_id=container_id,
        challenge_id=audit["challenge_id"],
        challenge_name=challenge_name,
        user_id=audit["user_id"],
        user_name=user_name,
        team_id=audit["team_id"],
        team_name=team_name,
        docker_context=context_name,
        message=f"container killed for {challenge_name}",
    )
    return {"success": "container killed"}


def renew_container(chal_id: int, xid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    challenge = _resolve_challenge(chal_id)
    if challenge is None:
        return error_body(CHALLENGE_NOT_FOUND, "permanent"), 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, is_entry=True, **owner_filter(xid, is_team)
    ).first()

    if running_container is None:
        return error_body(CONTAINER_NOT_FOUND_RESET, "user"), 400

    container_manager = current_app.container_manager
    try:
        if not container_manager.is_container_running(running_container.container_id, running_container.docker_context):
            kill_container(running_container.container_id)
            return error_body(CONTAINER_NOT_FOUND_RESET, "user"), 400
    except ContainerException:
        return error_body(HOST_UNREACHABLE, "transient"), 503

    max_renewals = resolve_max_renewals(challenge)
    renewals_used = running_container.renewals_used

    if renewals_used >= max_renewals:
        return error_body(NO_RENEWALS, "user"), 400

    now = int(time.time())
    time_remaining = max(0, running_container.expires - now)

    expiration = resolve_expiration(challenge)
    new_expires = now + expiration
    update = InstanceCoordinator.renew(
        running_container.instance_id,
        now=now,
        new_expires=new_expires,
        max_renewals=max_renewals,
    )
    if update is None:
        lifecycle = InstanceCoordinator.get_lifecycle(running_container.instance_id)
        if lifecycle and lifecycle.solved_at is not None:
            return error_body(SOLVED_NO_RENEW, "user"), 400
        if lifecycle and lifecycle.expires <= now:
            return error_body(EXPIRED_NO_RENEW, "user"), 400

        return error_body(NO_RENEWALS, "user"), 400

    new_expires = update.expires
    renewals_used = update.renewals_used - 1

    user_id = running_container.user_id
    user_name = running_container.user.name if running_container.user else None
    team_id = running_container.team_id
    team_name = running_container.team.name if running_container.team else None

    event_logger.log_event(
        event_type="renewed",
        message=f"container renewed for {challenge.name}",
        user_id=user_id,
        username=user_name,
        metadata={
            "container_id": running_container.container_id,
            "challenge_id": challenge.id,
            "challenge_name": challenge.name,
            "team_id": team_id,
            "team_name": team_name,
            "time_remaining": time_remaining,
            "renewal": f"{renewals_used + 1}/{max_renewals}",
        },
    )

    response = build_connection_response(
        "success",
        challenge,
        running_container,
        running_container.docker_context,
        expires=new_expires,
        renewals_used=renewals_used + 1,
    )
    response["success"] = "container renewed"
    return response


def _runtime_volume_plan(
    challenge: ContainerChallengeModel, container_manager: ContainerManager
) -> tuple[VolumePolicy, dict[str, tuple[MountRequest, ...]], set[str] | None]:
    try:
        policy = load_volume_policy()
        plan: dict[str, tuple[MountRequest, ...]] = {
            "entry": parse_mount_config(challenge.volumes, expected_scope="entry")
        }
        _, services = normalize_services(challenge.services_json)
        for service_name, service in services.items():
            plan[service_name] = parse_mount_config(service.get("volumes"), expected_scope="service")
    except (ValidationError, MountConfigError, ValueError) as exc:
        raise ContainerException(str(exc)) from exc

    if not any(plan.values()):
        return policy, plan, None  # without mounts every context is eligible

    configured = set(container_manager.host_manager.get_configured_contexts())
    candidates = [
        row.context_name
        for row in DockerContextModel.query.filter(
            DockerContextModel.state == "active",
            DockerContextModel.health_state == "healthy",
        ).all()
        if row.context_name in configured
    ]
    eligible = set(candidates)
    metadata_cache: dict[tuple[str, str], VolumeMetadata | None] = {}

    def inspect_once(context_name: str, docker_name: str) -> VolumeMetadata | None:
        key = (context_name, docker_name)
        if key not in metadata_cache:
            metadata_cache[key] = container_manager.host_manager.get_volume_metadata(context_name, docker_name)
        return metadata_cache[key]

    for mounts in plan.values():
        if not mounts:
            continue
        report = evaluate_volume_readiness(
            policy,
            candidates,
            mounts,
            inspect_once,
        )
        eligible.intersection_update(report.eligible_contexts)
    return policy, plan, eligible


def _resolve_runtime_volumes(
    policy: VolumePolicy,
    plan: dict[str, tuple[MountRequest, ...]],
    context_name: str,
    container_manager: ContainerManager,
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, dict[str, str]]]]:
    resolved: dict[str, dict[str, dict[str, str]]] = {}
    for service_name, mounts in plan.items():
        if not mounts:
            resolved[service_name] = {}
            continue
        readiness = evaluate_volume_readiness(  # the selected daemon can change after placement planning
            policy,
            [context_name],
            mounts,
            container_manager.host_manager.get_volume_metadata,
        )
        if not readiness.eligible_contexts:
            codes = sorted({issue.code for context in readiness.contexts for issue in context.issues})
            raise ContainerException(f"required volume is unavailable on the selected host ({', '.join(codes)})")
        resolved[service_name] = resolve_mounts_for_context(policy, context_name, mounts)
    return resolved.pop("entry", {}), resolved


def _cleanup_failed_reservation(
    container_manager: ContainerManager,
    reservation: InstanceReservation,
    error: Exception | str,
    *,
    ambiguous_external_io: bool = False,
) -> bool:
    if ambiguous_external_io or not reservation.context_name:  # timed out creates can finish later
        InstanceCoordinator.mark_cleanup_pending(reservation.instance_id, reservation.provision_token, str(error))
        return False

    try:
        container_manager.host_manager.force_remove_resources_by_label(
            reservation.context_name, f"ctf.instance_id={reservation.instance_id}"
        )
        return InstanceCoordinator.delete_after_confirmed_cleanup(
            reservation.instance_id, provision_token=reservation.provision_token
        )
    except Exception:
        logger.warning("could not prove cleanup for instance %s", reservation.instance_id, exc_info=True)
        InstanceCoordinator.mark_cleanup_pending(reservation.instance_id, reservation.provision_token, str(error))
        return False


def create_container(
    chal_id: int, xid: int, uid: int, is_team: bool
) -> JsonResponse | tuple[JsonResponse, int] | tuple[JsonResponse, int, dict[str, str]]:
    container_manager = current_app.container_manager
    challenge = _resolve_challenge(chal_id)
    if challenge is None:
        return error_body(CHALLENGE_NOT_FOUND, "permanent"), 400

    extra_env: dict[str, str] = {}
    freshness_secret_raw = get_setting("freshness_secret")
    if freshness_secret_raw:
        token_length = int(get_setting("freshness_token_length", 6) or 6)
        token = compute_token(str(freshness_secret_raw), chal_id, xid, length=token_length)
        extra_env["FRESHNESS_TOKEN"] = token

    if challenge.ssh_username:
        extra_env["SSH_USERNAME"] = challenge.ssh_username
    if challenge.ssh_password:
        extra_env["SSH_PASSWORD"] = challenge.ssh_password

    extra_env_or_none: dict[str, str] | None = extra_env or None

    try:
        _, services = normalize_services(challenge.services_json)
        policy, volume_plan, eligible_contexts = _runtime_volume_plan(challenge, container_manager)
    except ContainerException as err:
        _log_request_failed(challenge, uid, err)
        return error_body(sanitize_container_error(err)), 400

    expiration = resolve_expiration(challenge)
    expires = int(time.time() + expiration)  # host inspection must not reduce container lifetime
    effective_memory_mb = (
        int(challenge.max_memory_mb)
        if challenge.max_memory_mb is not None
        else int(get_setting("default_max_memory_mb", 512) or 512)
    )
    effective_cpu = (
        float(challenge.max_cpu)
        if challenge.max_cpu is not None
        else int(get_setting("default_max_cpu_millicores", 1_000) or 1_000) / 1_000
    )

    coordinator = InstanceCoordinator()
    try:
        reservation = coordinator.reserve_instance(
            challenge_id=challenge.id,
            xid=xid,
            is_team=is_team,
            submitter_user_id=uid,
            max_instances=int(get_setting("max_containers_per_user", 4) or 4),
            max_concurrent_creates=int(get_setting("max_concurrent_creates", 2) or 2),
            placement_units=1 + len(services),
            expires=expires,
            preferred_context_name=challenge.docker_context,
            eligible_context_names=eligible_contexts,
        )
    except InstanceQuotaExceeded:
        maximum = int(get_setting("max_containers_per_user", 4) or 4)
        return error_body(QUOTA_EXCEEDED.format(maximum=maximum), "user"), 409
    except CreateCapacityUnavailable as err:
        logger.warning("no create capacity for challenge %s: %s", challenge.id, err)
        return error_body(HOSTS_BUSY, "transient"), 429, {"Retry-After": "5"}
    except ContextUnavailable as err:
        logger.warning("no docker context for challenge %s: %s", challenge.id, err)
        return error_body(HOSTS_BUSY, "transient"), 503
    except CoordinationError as err:
        return error_body(sanitize_container_error(ContainerException(str(err)))), 503

    if not reservation.created:
        existing = ContainerInfoModel.query.filter_by(instance_id=reservation.instance_id, is_entry=True).first()
        if reservation.state == "running" and existing is not None:
            return build_connection_response("already_running", challenge, existing, reservation.context_name)
        if reservation.state == "provisioning":
            return error_body(REQUEST_IN_PROGRESS, "transient"), 429, {"Retry-After": "5"}
        return error_body(CLEANUP_IN_PROGRESS, "transient"), 503, {"Retry-After": "300"}

    if not reservation.context_name:
        _cleanup_failed_reservation(container_manager, reservation, "reservation has no docker context")
        return error_body(PLACEMENT_FAILED, "transient"), 503

    try:
        entry_volumes, service_volumes = _resolve_runtime_volumes(
            policy, volume_plan, reservation.context_name, container_manager
        )
    except ContainerException as err:
        _cleanup_failed_reservation(container_manager, reservation, err)
        return error_body(sanitize_container_error(err)), 503

    host_status = container_manager.orchestrator.get_status()
    event_logger.log_event(
        "container_requested",
        f"container requested for {challenge.name}",
        user_id=uid,
        metadata={
            "challenge_id": challenge.id,
            "challenge_name": challenge.name,
            "is_stack": bool(challenge.services_json),
            "hosts": {
                h["context_name"]: {
                    "containers": h["active_containers"],
                    "weight": h["weight"],
                    "healthy": h["healthy"],
                    "score": round(h["score"], 2) if h["healthy"] else 0,
                }
                for h in host_status
            },
        },
    )

    if challenge.services_json:
        try:
            entry_container, host_port, companions, stack_id, context_name = container_manager.create_stack(
                chal_id,
                xid,
                uid,
                challenge.image,
                challenge.port,
                challenge.command,
                challenge.services_json,
                challenge.network_json,
                effective_memory_mb,
                effective_cpu,
                reservation.context_name,
                reservation.instance_id,
                reservation.provision_token,
                extra_env=extra_env_or_none,
                ctype=challenge.ctype,
                cap_add=challenge.cap_add,
                entry_volumes=entry_volumes,
                service_volumes=service_volumes,
            )
        except Exception as err:
            _log_request_failed(challenge, uid, err)
            _cleanup_failed_reservation(container_manager, reservation, err, ambiguous_external_io=True)
            return error_body(sanitize_container_error(err)), 503, {"Retry-After": "300"}

        if host_port is None:
            error = ContainerException("could not determine the entry container port")
            _cleanup_failed_reservation(container_manager, reservation, error)
            return error_body(PORT_UNAVAILABLE, "transient"), 500

        members = (PhysicalMember(entry_container.id, int(host_port), True, "entry"),) + tuple(
            PhysicalMember(svc_container.id, 0, False, svc_name) for svc_name, svc_container in companions
        )
        created_container = entry_container

    else:
        try:
            created_container, context_name = container_manager.create_container(
                chal_id,
                xid,
                uid,
                challenge.image,
                challenge.port,
                challenge.command,
                effective_memory_mb,
                effective_cpu,
                reservation.context_name,
                reservation.instance_id,
                reservation.provision_token,
                extra_env=extra_env_or_none,
                ctype=challenge.ctype,
                cap_add=challenge.cap_add,
                resolved_volumes=entry_volumes,
            )
        except Exception as err:
            _log_request_failed(challenge, uid, err)
            _cleanup_failed_reservation(container_manager, reservation, err, ambiguous_external_io=True)
            return error_body(sanitize_container_error(err)), 503, {"Retry-After": "300"}

        port = container_manager.get_container_port(created_container.id, context_name)
        if port is None:
            error = ContainerException("could not determine the container port")
            _cleanup_failed_reservation(container_manager, reservation, error)
            return error_body(PORT_UNAVAILABLE, "transient"), 500
        stack_id = None
        members = (PhysicalMember(created_container.id, int(port), True, "entry"),)

    try:
        finalized = coordinator.mark_running(
            reservation.instance_id,
            reservation.provision_token,
            created_container.id,
            stack_id=stack_id,
            physical_members=members,
        )
        if not finalized:
            raise ContainerException("the provisioning reservation changed before finalization")
    except Exception as err:
        _cleanup_failed_reservation(container_manager, reservation, err)
        return error_body(FINALIZATION_FAILED, "transient"), 500

    log_container_event(
        event_type="created",
        container_id=created_container.id,
        challenge_id=challenge.id,
        challenge_name=challenge.name,
        user_id=finalized.user_id,
        user_name=None,
        team_id=finalized.team_id,
        team_name=None,
        docker_context=context_name,
        message=f"container created for {challenge.name}",
    )

    return build_connection_response("created", challenge, finalized, context_name)


def view_container_info(chal_id: int, xid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    container_manager = current_app.container_manager
    challenge = _resolve_challenge(chal_id)
    if challenge is None:
        return error_body(CHALLENGE_NOT_FOUND, "permanent"), 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, is_entry=True, **owner_filter(xid, is_team)
    ).first()

    if running_container is None:
        misconfigured = _check_misconfigured(challenge, container_manager)
        if misconfigured:
            return misconfigured

        return {"status": "instance not started"}

    if running_container.instance.state == "cleanup_pending":
        return {
            "status": "cleanup_pending",
            "message": AWAITING_CLEANUP,
            "error_kind": "transient",
        }, 503

    try:
        is_running = container_manager.is_container_running(
            running_container.container_id, running_container.docker_context
        )
    except ContainerException:
        response = build_connection_response(
            "host_unavailable", challenge, running_container, running_container.docker_context
        )
        response["message"] = HOST_UNREACHABLE
        return response

    if is_running:
        return build_connection_response(
            "already_running", challenge, running_container, running_container.docker_context
        )

    cleanup = kill_container(running_container.container_id)
    if "success" not in cleanup:
        msg = cleanup.get("error")
        return error_body(msg if isinstance(msg, str) else CLEANUP_PENDING, "transient"), 503

    return {"status": "instance not started"}


def _check_misconfigured(
    challenge: ContainerChallengeModel, container_manager: ContainerManager
) -> JsonResponse | None:
    if not challenge.image or not challenge.port:
        logger.warning(f"challenge {challenge.id} ({challenge.name}) missing image or port")
        return {
            "status": "misconfigured",
            "message": CHALLENGE_MISCONFIGURED,
        }

    if not container_manager.host_manager.has_contexts():
        logger.warning(f"no docker contexts available for challenge {challenge.id} ({challenge.name})")
        return {
            "status": "misconfigured",
            "message": CHALLENGE_UNAVAILABLE,
        }

    return None


def connect_type(chal_id: int) -> JsonResponse | tuple[JsonResponse, int]:
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": CHALLENGE_NOT_FOUND}, 400

    return {"status": "ok", "connect": challenge.ctype}
