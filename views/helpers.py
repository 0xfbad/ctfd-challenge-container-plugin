import time
import datetime
from flask import current_app, request
from CTFd.models import db

from . import containers_bp
from ..models import ContainerInfoModel, ContainerChallengeModel, DockerContextModel
from ..container_manager import ContainerException
from ..utils import settings
from ..event_logger import event_logger


def log_container_event(
    event_type,
    container_id=None,
    challenge_id=None,
    challenge_name=None,
    user_id=None,
    user_name=None,
    team_id=None,
    team_name=None,
    message=None,
):
    event_logger.log_event(
        event_type=event_type,
        container_id=container_id,
        challenge_id=challenge_id,
        challenge_name=challenge_name,
        user_id=user_id,
        user_name=user_name,
        team_id=team_id,
        team_name=team_name,
        message=message,
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


def get_hostname_for_context(context_name):
    if not context_name or context_name == "default":
        try:
            return request.host.split(":")[0]
        except Exception:
            return "localhost"

    context = DockerContextModel.query.filter_by(context_name=context_name).first()
    if context and context.hostname:
        hostname = context.hostname
        if "@" in hostname:
            hostname = hostname.split("@")[1]
        return hostname

    try:
        return request.host.split(":")[0]
    except Exception:
        return "localhost"


def kill_container(container_id):
    container_manager = current_app.container_manager
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()

    try:
        context_name = container.docker_context if container else None
        container_manager.kill_container(container_id, context_name)
    except ContainerException:
        return {"error": "docker is not initialized, please check your settings"}
    except Exception as e:
        return {"error": f"failed to kill container: {str(e)}"}

    if container:
        try:
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

            db.session.delete(container)
            db.session.commit()
            return {"success": "container killed"}
        except Exception as e:
            return {"error": f"failed to log event or delete container: {str(e)}"}
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
    container_manager = current_app.container_manager
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    max_containers_allowed = int(settings["vars"]["MAX_CONTAINERS_ALLOWED"])
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
            return {"error": str(err)}, 500

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
        )
    except ContainerException as err:
        return {"error": str(err)}

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
    db.session.commit()

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
        except ContainerException as err:
            return {"error": str(err)}, 500
    else:
        return {"status": "instance not started"}


def connect_type(chal_id):
    challenge = ContainerChallengeModel.query.filter_by(id=chal_id).first()

    if challenge is None:
        return {"error": "challenge not found"}, 400

    return {"status": "ok", "connect": challenge.ctype}


@containers_bp.app_template_filter("format_time")
def format_time_filter(unix_seconds):
    dt = datetime.datetime.fromtimestamp(
        unix_seconds,
        tz=datetime.datetime.now(datetime.timezone.utc).astimezone().tzinfo,
    )

    return dt.strftime("%H:%M:%S %d/%m/%Y")
