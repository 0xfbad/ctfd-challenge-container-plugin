import time
import logging
import threading
from flask import current_app, request
from CTFd.models import db

from ..models import ContainerInfoModel, ContainerChallengeModel, ContainerHistoryModel, DockerContextModel
from ..container_manager import ContainerException
from ..utils import get_setting
from ..freshness import compute_token
from ..event_logger import event_logger

logger = logging.getLogger(__name__)

_create_locks_guard = threading.Lock()
_create_locks: dict[tuple, threading.Lock] = {}
_MAX_CREATE_LOCKS = 1000


def _get_create_lock(chal_id, xid, is_team):
    key = (chal_id, "team" if is_team else "user", xid)
    with _create_locks_guard:
        if key not in _create_locks:
            if len(_create_locks) > _MAX_CREATE_LOCKS:
                stale = [k for k, v in _create_locks.items() if not v.locked()]
                for k in stale:
                    del _create_locks[k]
            _create_locks[key] = threading.Lock()
        return _create_locks[key]


def sanitize_container_error(err):
    """strip internal details from container errors shown to users"""
    msg = str(err)
    if any(
        p in msg.lower()
        for p in (
            "failed to",
            "docker error",
            "not connected",
            "no docker context",
            "no healthy context",
            "not available",
        )
    ):
        logger.error(f"container error (sanitized): {msg}")
        return "a server error occurred, please try again"
    return msg


def log_container_event(
    event_type,
    message,
    user_id=None,
    user_name=None,
    container_id=None,
    challenge_id=None,
    challenge_name=None,
    team_id=None,
    team_name=None,
):
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
        },
    )


def build_connection_response(status, challenge, container, context_name):
    return {
        "status": status,
        "hostname": get_hostname_for_context(context_name),
        "port": container.port,
        "ssh_username": challenge.ssh_username,
        "ssh_password": challenge.ssh_password,
        "connect": challenge.ctype,
        "expires": container.expires,
    }


def _request_hostname():
    try:
        return request.host.split(":")[0]
    except Exception:
        return "localhost"


def get_hostname_for_context(context_name):
    if not context_name:
        return _request_hostname()

    from ..docker_host_manager import LOCAL_CONTEXT_NAME

    # local context runs on the same machine, use the address users reach CTFd through
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


def record_history_stop(container_id, reason):
    row = ContainerHistoryModel.query.filter_by(container_id=container_id).first()
    if row:
        row.stopped_at = time.time()
        row.reason = reason


def kill_container(container_id):
    container_manager = current_app.container_manager
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()

    try:
        context_name = container.docker_context if container else None
        container_manager.kill_container(container_id, context_name)
    except ContainerException:
        return {"error": "docker is not initialized, please check your settings"}
    except Exception as e:
        logger.error(f"failed to kill container {container_id}: {e}")
        return {"error": "failed to stop container, please try again"}

    if container:
        try:
            container_manager.release_slot(container.docker_context)

            challenge_id = container.challenge_id
            challenge_name = container.challenge.name if container.challenge else None
            user_id = container.user_id
            user_name = container.user.name if container.user else None
            team_id = container.team_id
            team_name = container.team.name if container.team else None

            log_container_event(
                event_type="killed",
                container_id=container_id,
                challenge_id=challenge_id,
                challenge_name=challenge_name,
                user_id=user_id,
                user_name=user_name,
                team_id=team_id,
                team_name=team_name,
                message=f"container killed for {challenge_name}",
            )

            record_history_stop(container_id, "stopped")
            db.session.delete(container)
            db.session.commit()
            return {"success": "container killed"}
        except Exception as e:
            logger.error(f"failed to clean up container {container_id}: {e}")
            return {"error": "failed to stop container, please try again"}
    else:
        return {"error": "container not found"}


def renew_container(chal_id, xid, is_team):
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    filter_args = {"challenge_id": challenge.id}
    filter_args["team_id" if is_team else "user_id"] = xid
    running_container = ContainerInfoModel.query.filter_by(**filter_args).first()

    if running_container is None:
        return {"error": "container not found, try resetting the container"}

    try:
        expiration_seconds = (challenge.expiration_minutes or 0) * 60
        running_container.expires = int(time.time() + expiration_seconds) if expiration_seconds > 0 else 0
        db.session.commit()
    except Exception:
        return {"error": "database error occurred, please try again"}

    user_id = running_container.user_id
    user_name = running_container.user.name if running_container.user else None
    team_id = running_container.team_id
    team_name = running_container.team.name if running_container.team else None

    log_container_event(
        event_type="renewed",
        container_id=running_container.container_id,
        challenge_id=challenge.id,
        challenge_name=challenge.name,
        user_id=user_id,
        user_name=user_name,
        team_id=team_id,
        team_name=team_name,
        message=f"container renewed for {challenge.name}",
    )

    response = build_connection_response("success", challenge, running_container, running_container.docker_context)
    response["success"] = "container renewed"
    return response


def create_container(chal_id, xid, uid, is_team):
    lock = _get_create_lock(chal_id, xid, is_team)
    acquired = lock.acquire(timeout=30)
    if not acquired:
        return {"error": "another container request is in progress, please wait"}, 429

    try:
        return _create_container_inner(chal_id, xid, uid, is_team)
    finally:
        lock.release()


def _create_container_inner(chal_id, xid, uid, is_team):
    container_manager = current_app.container_manager
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    max_containers_allowed = get_setting("max_containers_per_user")
    if not is_team:
        uid = xid
    user_containers = ContainerInfoModel.query.filter_by(user_id=uid)

    if user_containers.count() >= max_containers_allowed:
        return {
            "error": f"you can only spawn {max_containers_allowed} containers at a time, please stop other containers to continue"
        }, 500

    filter_args = {"challenge_id": challenge.id}
    filter_args["team_id" if is_team else "user_id"] = xid
    running_container = ContainerInfoModel.query.filter_by(**filter_args).first()

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
        except ContainerException as err:
            logger.error(f"container status check failed: {err}")
            return {"error": "a server error occurred, please try again"}, 500

    extra_env = {}
    freshness_secret = get_setting("freshness_secret")
    if freshness_secret:
        token = compute_token(freshness_secret, chal_id, xid)
        extra_env["FRESHNESS_TOKEN"] = token

    if challenge.ssh_username:
        extra_env["SSH_USERNAME"] = challenge.ssh_username
    if challenge.ssh_password:
        extra_env["SSH_PASSWORD"] = challenge.ssh_password

    extra_env = extra_env or None

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
            extra_env=extra_env,
            ctype=challenge.ctype,
            cap_add=challenge.cap_add,
        )
    except ContainerException as err:
        return {"error": sanitize_container_error(err)}

    port = container_manager.get_container_port(created_container.id, context_name)

    if port is None:
        return {"status": "error", "error": "could not get port"}

    expiration_seconds = (challenge.expiration_minutes or 0) * 60
    expires = int(time.time() + expiration_seconds) if expiration_seconds > 0 else 0

    new_container = ContainerInfoModel(
        container_id=created_container.id,
        challenge_id=challenge.id,
        team_id=xid if is_team else None,
        user_id=uid,
        port=port,
        timestamp=int(time.time()),
        expires=expires,
        docker_context=context_name,
    )
    db.session.add(new_container)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # kill the orphaned docker container best-effort
        try:
            container_manager.kill_container(created_container.id, context_name)
        except Exception:
            pass
        return {"error": "database error, container has been cleaned up"}, 500

    history = ContainerHistoryModel(
        container_id=created_container.id,
        challenge_id=challenge.id,
        user_id=uid,
        team_id=xid if is_team else None,
        docker_context=context_name,
        created_at=time.time(),
    )
    db.session.add(history)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

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
        message=f"container created for {challenge.name}",
    )

    response = build_connection_response("created", challenge, new_container, context_name)
    return response


def view_container_info(chal_id, xid, is_team):
    container_manager = current_app.container_manager
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    filter_args = {"challenge_id": challenge.id}
    filter_args["team_id" if is_team else "user_id"] = xid
    running_container = ContainerInfoModel.query.filter_by(**filter_args).first()

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


def _check_misconfigured(challenge, container_manager):
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


def connect_type(chal_id):
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    return {"status": "ok", "connect": challenge.ctype}
