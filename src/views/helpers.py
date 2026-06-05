from __future__ import annotations

import time
import logging
import threading

from flask import current_app, request
from CTFd.models import db

from ..models import ContainerInfoModel, ContainerChallengeModel, ContainerHistoryModel, DockerContextModel
from ..exceptions import ContainerException, ContainerUnavailableException
from ..container_manager import ContainerManager
from ..docker_host_manager import LOCAL_CONTEXT_NAME
from ..utils import get_setting, owner_filter
from ..freshness import compute_token
from ..event_logger import event_logger

logger = logging.getLogger(__name__)

# JSON response dicts returned by helper functions
JsonResponse = dict[str, str | int | bool | None]

_create_locks_guard = threading.Lock()
_create_locks: dict[tuple[int, str, int], threading.Lock] = {}
_MAX_CREATE_LOCKS = 1000


def _get_create_lock(chal_id: int, xid: int, is_team: bool) -> threading.Lock:
    key = (chal_id, "team" if is_team else "user", xid)
    with _create_locks_guard:
        if key not in _create_locks:
            if len(_create_locks) > _MAX_CREATE_LOCKS:
                stale = [k for k, v in _create_locks.items() if not v.locked()]
                for k in stale:
                    del _create_locks[k]
            _create_locks[key] = threading.Lock()
        return _create_locks[key]


_USER_SAFE_PATTERNS = (
    "no renewals remaining",
    "container not found",
    "challenge not found",
    "you can only spawn",
    "another container request is in progress",
    "docker image not found",
    "memory limit must be",
    "cpu limit must be",
)


def sanitize_container_error(err: ContainerException | Exception) -> str:
    msg = str(err)
    lower = msg.lower()
    if any(p in lower for p in _USER_SAFE_PATTERNS):
        return msg
    logger.error(f"container error (sanitized): {msg}")
    return "a server error occurred, please try again"


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


def build_connection_response(
    status: str,
    challenge: ContainerChallengeModel,
    container: ContainerInfoModel,
    context_name: str | None,
) -> JsonResponse:
    max_renewals = challenge.max_renewals
    if max_renewals is None:
        max_renewals = get_setting("default_max_renewals", 2)
    return {
        "status": status,
        "hostname": get_hostname_for_context(context_name),
        "port": container.port,
        "ssh_username": challenge.ssh_username,
        "ssh_password": challenge.ssh_password,
        "connect": challenge.ctype,
        "expires": container.expires,
        "renewals_used": container.renewals_used or 0,
        "max_renewals": max_renewals,
    }


def _request_hostname() -> str:
    return request.host.split(":")[0]


def get_hostname_for_context(context_name: str | None) -> str:
    if not context_name:
        return _request_hostname()

    # local containers are colocated so users connect via the CTFd hostname
    if context_name == LOCAL_CONTEXT_NAME:
        return _request_hostname()

    context = DockerContextModel.query.filter_by(context_name=context_name).first()
    if context:
        if context.pub_hostname:
            return context.pub_hostname
        if context.hostname:
            hostname = context.hostname
            if "@" in hostname:
                hostname = hostname.split("@")[1]
            return hostname

    return _request_hostname()


def record_history_stop(container_id: str, reason: str) -> None:
    row = ContainerHistoryModel.query.filter_by(container_id=container_id).first()
    if row:
        row.stopped_at = time.time()
        row.reason = reason


def _add_history_row(
    container_id: str,
    challenge_id: int,
    uid: int,
    team_id: int | None,
    context_name: str,
    stack_id: str | None = None,
) -> None:
    db.session.add(
        ContainerHistoryModel(
            container_id=container_id,
            challenge_id=challenge_id,
            user_id=uid,
            team_id=team_id,
            docker_context=context_name,
            stack_id=stack_id,
            created_at=time.time(),
        )
    )


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


def kill_container(container_id: str) -> JsonResponse:
    container_manager = current_app.container_manager
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()

    if not container:
        return {"error": "container not found"}

    context_name = container.docker_context
    stack_id = container.stack_id

    try:
        if stack_id:
            container_manager.host_manager.kill_stack(context_name, stack_id)
        else:
            container_manager.kill_container(container_id, context_name)
    except ContainerException:
        return {"error": "docker is not initialized, please check your settings"}
    except Exception as e:
        logger.error(f"failed to kill container {container_id}: {e}")
        return {"error": "failed to stop container, please try again"}

    container_manager.release_slot(context_name)

    challenge_name = container.challenge.name if container.challenge else None
    user_name = container.user.name if container.user else None
    team_name = container.team.name if container.team else None

    log_container_event(
        event_type="killed",
        container_id=container_id,
        challenge_id=container.challenge_id,
        challenge_name=challenge_name,
        user_id=container.user_id,
        user_name=user_name,
        team_id=container.team_id,
        team_name=team_name,
        docker_context=context_name,
        message=f"container killed for {challenge_name}",
    )

    if stack_id:
        siblings = ContainerInfoModel.query.filter_by(stack_id=stack_id).all()
        for s in siblings:
            record_history_stop(s.container_id, "stopped")
            db.session.delete(s)
    else:
        record_history_stop(container_id, "stopped")
        db.session.delete(container)

    db.session.commit()
    return {"success": "container killed"}


def renew_container(chal_id: int, xid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, is_entry=True, **owner_filter(xid, is_team)
    ).first()

    if running_container is None:
        return {"error": "container not found, try resetting the container"}

    container_manager = current_app.container_manager
    try:
        if not container_manager.is_container_running(running_container.container_id, running_container.docker_context):
            if running_container.stack_id:
                for s in ContainerInfoModel.query.filter_by(stack_id=running_container.stack_id).all():
                    db.session.delete(s)
            else:
                db.session.delete(running_container)
            db.session.commit()
            return {"error": "container not found, try resetting the container"}
    except ContainerException:
        return {"error": "the container host is temporarily unreachable, please wait"}

    max_renewals = challenge.max_renewals
    if max_renewals is None:
        max_renewals = get_setting("default_max_renewals", 2)
    renewals_used = running_container.renewals_used or 0

    if renewals_used >= max_renewals:
        return {"error": "no renewals remaining"}

    now = int(time.time())
    time_remaining = max(0, (running_container.expires or 0) - now)

    expiration = int(challenge.expiration_seconds or get_setting("default_expiration_seconds", 1800) or 1800)
    new_expires = now + expiration
    running_container.expires = new_expires
    running_container.renewals_used = renewals_used + 1
    if running_container.stack_id:
        ContainerInfoModel.query.filter_by(stack_id=running_container.stack_id).update(
            {"expires": new_expires, "renewals_used": renewals_used + 1}
        )
    db.session.commit()

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

    response = build_connection_response("success", challenge, running_container, running_container.docker_context)
    response["success"] = "container renewed"
    return response


def create_container(chal_id: int, xid: int, uid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    lock = _get_create_lock(chal_id, xid, is_team)
    acquired = lock.acquire(timeout=30)
    if not acquired:
        return {"error": "another container request is in progress, please wait"}, 429

    try:
        return _create_container_inner(chal_id, xid, uid, is_team)
    finally:
        lock.release()


def _create_container_inner(chal_id: int, xid: int, uid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    container_manager = current_app.container_manager
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    max_containers_allowed = get_setting("max_containers_per_user")
    user_containers = ContainerInfoModel.query.filter_by(**owner_filter(xid, is_team))

    if user_containers.count() >= max_containers_allowed:
        return {
            "error": f"you can only spawn {max_containers_allowed} containers at a time, please stop other containers to continue"
        }, 409

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, **owner_filter(xid, is_team)
    ).first()

    if running_container:
        try:
            if container_manager.is_container_running(running_container.container_id, running_container.docker_context):
                response = build_connection_response(
                    "already_running", challenge, running_container, running_container.docker_context
                )
                return response
            else:
                db.session.delete(running_container)
                db.session.commit()
        except ContainerUnavailableException:
            return {"error": "container service temporarily unavailable"}, 503
        except ContainerException as err:
            logger.error(f"container status check failed: {err}")
            return {"error": "a server error occurred, please try again"}, 500

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

    expiration = int(challenge.expiration_seconds or get_setting("default_expiration_seconds", 1800) or 1800)
    expires = int(time.time() + expiration)
    now = int(time.time())

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
                    "score": round(h["weight"] / (h["active_containers"] + 1), 2) if h["healthy"] else 0,
                }
                for h in host_status
            },
        },
    )

    team_id = xid if is_team else None

    if challenge.services_json:
        try:
            entry_container, host_port, companions, stack_id, context_name = container_manager.create_stack(
                chal_id,
                xid,
                uid,
                challenge.image,
                challenge.port,
                challenge.command,
                challenge.volumes,
                challenge.services_json,
                challenge.network_json,
                challenge.max_memory_mb,
                challenge.max_cpu,
                challenge.docker_context,
                extra_env=extra_env_or_none,
                ctype=challenge.ctype,
                cap_add=challenge.cap_add,
            )
        except ContainerException as err:
            _log_request_failed(challenge, uid, err)
            return {"error": sanitize_container_error(err)}

        if host_port is None:
            return {"status": "error", "error": "could not get port"}

        entry_row = ContainerInfoModel(
            container_id=entry_container.id,
            challenge_id=challenge.id,
            team_id=team_id,
            user_id=uid,
            port=host_port,
            timestamp=now,
            expires=expires,
            docker_context=context_name,
            stack_id=stack_id,
            is_entry=True,
        )
        db.session.add(entry_row)
        _add_history_row(entry_container.id, challenge.id, uid, team_id, context_name, stack_id)

        for svc_name, svc_container in companions:
            db.session.add(
                ContainerInfoModel(
                    container_id=svc_container.id,
                    challenge_id=challenge.id,
                    team_id=team_id,
                    user_id=uid,
                    port=0,
                    timestamp=now,
                    expires=expires,
                    docker_context=context_name,
                    stack_id=stack_id,
                    is_entry=False,
                )
            )
            _add_history_row(svc_container.id, challenge.id, uid, team_id, context_name, stack_id)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                container_manager.host_manager.kill_stack(context_name, stack_id)
            except Exception:
                logger.debug("failed to clean up stack %s after db error", stack_id, exc_info=True)
            return {"error": "database error, stack has been cleaned up"}, 500

        new_container = entry_row
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
                challenge.volumes,
                challenge.max_memory_mb,
                challenge.max_cpu,
                challenge.docker_context,
                extra_env=extra_env_or_none,
                ctype=challenge.ctype,
                cap_add=challenge.cap_add,
            )
        except ContainerException as err:
            _log_request_failed(challenge, uid, err)
            return {"error": sanitize_container_error(err)}

        port = container_manager.get_container_port(created_container.id, context_name)
        if port is None:
            return {"status": "error", "error": "could not get port"}

        new_container = ContainerInfoModel(
            container_id=created_container.id,
            challenge_id=challenge.id,
            team_id=team_id,
            user_id=uid,
            port=port,
            timestamp=now,
            expires=expires,
            docker_context=context_name,
        )
        db.session.add(new_container)
        _add_history_row(created_container.id, challenge.id, uid, team_id, context_name)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            try:
                container_manager.kill_container(created_container.id, context_name)
            except Exception:
                logger.debug("failed to clean up container after db error", exc_info=True)
            return {"error": "database error, container has been cleaned up"}, 500

    user_id = new_container.user_id
    user_name = new_container.user.name if new_container.user else None
    team_id = new_container.team_id
    team_name = new_container.team.name if new_container.team else None

    log_container_event(
        event_type="created",
        container_id=created_container.id,
        challenge_id=challenge.id,
        challenge_name=challenge.name,
        user_id=user_id,
        user_name=user_name,
        team_id=team_id,
        team_name=team_name,
        docker_context=context_name,
        message=f"container created for {challenge.name}",
    )

    response = build_connection_response("created", challenge, new_container, context_name)
    return response


def view_container_info(chal_id: int, xid: int, is_team: bool) -> JsonResponse | tuple[JsonResponse, int]:
    container_manager = current_app.container_manager
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    running_container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge.id, is_entry=True, **owner_filter(xid, is_team)
    ).first()

    if running_container:
        try:
            if container_manager.is_container_running(running_container.container_id, running_container.docker_context):
                response = build_connection_response(
                    "already_running", challenge, running_container, running_container.docker_context
                )
                return response
            else:
                if running_container.stack_id:
                    stale = ContainerInfoModel.query.filter_by(stack_id=running_container.stack_id).all()
                    for s in stale:
                        db.session.delete(s)
                else:
                    db.session.delete(running_container)
                db.session.commit()
                return {"status": "instance not started"}
        except ContainerException:
            # host is down but the container record is still valid
            response = build_connection_response(
                "host_unavailable", challenge, running_container, running_container.docker_context
            )
            response["message"] = "the container host is temporarily unreachable, please wait"
            return response

    misconfigured = _check_misconfigured(challenge, container_manager)
    if misconfigured:
        return misconfigured

    return {"status": "instance not started"}


def _check_misconfigured(
    challenge: ContainerChallengeModel, container_manager: ContainerManager
) -> JsonResponse | None:
    if not challenge.image or not challenge.port:
        logger.warning(f"challenge {challenge.id} ({challenge.name}) missing image or port")
        return {
            "status": "misconfigured",
            "message": "This challenge has a broken configuration. This is on our end, not yours.",
        }

    if not container_manager.host_manager.has_contexts():
        logger.warning(f"no docker contexts available for challenge {challenge.id} ({challenge.name})")
        return {
            "status": "misconfigured",
            "message": "This challenge is temporarily unavailable due to a server configuration issue. This is on our end, not yours.",
        }

    return None


def connect_type(chal_id: int) -> JsonResponse | tuple[JsonResponse, int]:
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    return {"status": "ok", "connect": challenge.ctype}
