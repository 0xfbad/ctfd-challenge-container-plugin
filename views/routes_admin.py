import queue
import json
from flask import request, render_template, current_app, jsonify, Response, stream_with_context
from CTFd.utils.decorators import admins_only
from CTFd.models import db

from . import containers_bp
from .helpers import kill_container, get_hostname_for_context
from ..utils import is_team_mode, get_setting, set_setting, DEFAULTS
from ..models import ContainerInfoModel, DockerContextModel
from ..container_manager import ContainerException
from ..event_logger import event_logger


def get_contexts_with_default(container_manager):
    contexts = DockerContextModel.query.all()
    contexts_list = list(contexts)

    if "default" in container_manager._context_configs and not any(c.context_name == "default" for c in contexts_list):

        class DefaultContext:
            id = None
            context_name = "default"
            hostname = None
            weight = 1
            enabled = True

        contexts_list.insert(0, DefaultContext())

    return contexts_list


def get_contexts_data_with_default(container_manager):
    contexts = DockerContextModel.query.all()
    contexts_data = [
        {
            "id": ctx.id,
            "context_name": ctx.context_name,
            "hostname": ctx.hostname,
            "pub_hostname": getattr(ctx, "pub_hostname", None),
            "weight": ctx.weight,
            "enabled": ctx.enabled,
        }
        for ctx in contexts
    ]

    if "default" in container_manager._context_configs and not any(
        c["context_name"] == "default" for c in contexts_data
    ):
        contexts_data.insert(
            0,
            {
                "id": None,
                "context_name": "default",
                "hostname": None,
                "weight": 1,
                "enabled": True,
            },
        )

    return contexts_data


@containers_bp.route("/dashboard", methods=["GET"])
@admins_only
def route_containers_dashboard():
    container_manager = current_app.container_manager
    running_containers = ContainerInfoModel.query.order_by(ContainerInfoModel.timestamp.desc()).all()

    try:
        connected = container_manager.is_connected()
    except ContainerException:
        connected = False

    try:
        running_ids = container_manager.get_running_container_ids()
    except ContainerException:
        running_ids = set()

    for container in running_containers:
        container.is_running = container.container_id in running_ids
        container.hostname = get_hostname_for_context(container.docker_context)

    return render_template(
        "container_dashboard.html",
        containers=running_containers,
        connected=connected,
    )


@containers_bp.route("/api/running_containers", methods=["GET"])
@admins_only
def route_get_running_containers():
    container_manager = current_app.container_manager
    running_containers = ContainerInfoModel.query.order_by(ContainerInfoModel.timestamp.desc()).all()

    try:
        connected = container_manager.is_connected()
    except ContainerException:
        connected = False

    try:
        running_ids = container_manager.get_running_container_ids()
    except ContainerException:
        running_ids = set()

    team_mode = is_team_mode()

    running_containers_data = []
    for container in running_containers:
        container.is_running = container.container_id in running_ids

        hostname = get_hostname_for_context(container.docker_context)

        container_data = {
            "container_id": container.container_id,
            "image": container.challenge.image,
            "challenge": f"{container.challenge.name}",
            "challenge_id": container.challenge_id,
            "user": f"{container.user.name}",
            "user_id": container.user_id,
            "port": container.port,
            "created": container.timestamp,
            "expires": container.expires,
            "is_running": container.is_running,
            "hostname": hostname,
            "connect_type": container.challenge.ctype,
            "ssh_username": container.challenge.ssh_username,
            "ssh_password": container.challenge.ssh_password,
        }
        if team_mode:
            container_data["team"] = f"{container.team.name}"
            container_data["team_id"] = container.team_id
        running_containers_data.append(container_data)

    response_data = {
        "containers": running_containers_data,
        "connected": connected,
    }

    return jsonify(response_data)


@containers_bp.route("/api/events/recent", methods=["GET"])
@admins_only
def route_get_recent_events():
    events = event_logger.get_recent_events(limit=50)
    return jsonify(events=events)


@containers_bp.route("/api/events/stream", methods=["GET"])
@admins_only
def route_events_stream():
    def event_stream():
        q = queue.Queue(maxsize=100)

        def listener(event):
            try:
                q.put_nowait(event)
            except queue.Full:
                # drop oldest event to make room
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

        event_logger.add_listener(listener)

        yield f"data: {json.dumps({'type': 'connected'})}\n\n"

        try:
            while True:
                try:
                    event_data = q.get(timeout=30)
                    yield f"data: {json.dumps(event_data)}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        except GeneratorExit:
            event_logger.remove_listener(listener)

    response = Response(stream_with_context(event_stream()), content_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@containers_bp.route("/api/kill", methods=["POST"])
@admins_only
def route_kill_container():
    if not request.is_json:
        return jsonify(error="invalid request"), 400

    container_id = request.json.get("container_id")
    if not container_id:
        return jsonify(error="no container_id specified"), 400

    result = kill_container(container_id)
    status_code = 200 if "success" in result else 400
    return jsonify(result), status_code


@containers_bp.route("/api/purge", methods=["POST"])
@admins_only
def route_purge_containers():
    container_ids = [c.container_id for c in ContainerInfoModel.query.all()]
    for cid in container_ids:
        try:
            kill_container(cid)
        except ContainerException:
            pass
    return jsonify(success="purged all containers"), 200


@containers_bp.route("/api/images", methods=["GET"])
@admins_only
def route_get_images():
    container_manager = current_app.container_manager
    try:
        images = container_manager.get_images()
    except ContainerException as err:
        return jsonify(error=str(err)), 500

    return jsonify(images=images)


@containers_bp.route("/api/images/<context_name>", methods=["GET"])
@admins_only
def route_get_images_for_context(context_name):
    container_manager = current_app.container_manager
    try:
        images = container_manager.get_images_for_context(context_name)
    except ContainerException as err:
        return jsonify(error=str(err)), 500

    return jsonify(images=images)


@containers_bp.route("/api/contexts", methods=["GET"])
@admins_only
def route_get_contexts():
    container_manager = current_app.container_manager
    contexts = list(container_manager._context_configs.keys())
    return jsonify(contexts=contexts)


@containers_bp.route("/admin/contexts", methods=["GET"])
@admins_only
def route_list_contexts():
    container_manager = current_app.container_manager
    try:
        contexts = get_contexts_with_default(container_manager)
    except Exception:
        contexts = []

    return render_template("admin/contexts.html", contexts=contexts)


@containers_bp.route("/api/contexts/list", methods=["GET"])
@admins_only
def route_api_list_contexts():
    container_manager = current_app.container_manager
    contexts_data = get_contexts_data_with_default(container_manager)
    return jsonify(contexts=contexts_data)


@containers_bp.route("/api/contexts/add", methods=["POST"])
@admins_only
def route_api_add_context():
    if not request.is_json:
        return jsonify(error="invalid request"), 400

    context_name = request.json.get("context_name")
    hostname = request.json.get("hostname")
    pub_hostname = request.json.get("pub_hostname")
    weight = request.json.get("weight", 1)
    enabled = request.json.get("enabled", True)

    if not context_name:
        return jsonify(error="context_name is required"), 400

    if not hostname:
        return jsonify(error="hostname is required"), 400

    existing = DockerContextModel.query.filter_by(context_name=context_name).first()
    if existing:
        return jsonify(error="context already exists"), 400

    try:
        weight = int(weight)
        if weight < 1:
            return jsonify(error="weight must be at least 1"), 400
    except ValueError:
        return jsonify(error="weight must be an integer"), 400

    new_context = DockerContextModel(
        context_name=context_name, hostname=hostname, pub_hostname=pub_hostname, weight=weight, enabled=enabled
    )
    db.session.add(new_context)
    db.session.commit()

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    return jsonify(success="context added", id=new_context.id)


@containers_bp.route("/api/contexts/update/<int:context_id>", methods=["PUT"])
@admins_only
def route_api_update_context(context_id):
    if not request.is_json:
        return jsonify(error="invalid request"), 400

    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    if "hostname" in request.json:
        if not request.json["hostname"]:
            return jsonify(error="hostname cannot be empty"), 400
        context.hostname = request.json["hostname"]

    if "pub_hostname" in request.json:
        context.pub_hostname = request.json["pub_hostname"] or None

    if "weight" in request.json:
        try:
            weight = int(request.json["weight"])
            if weight < 1:
                return jsonify(error="weight must be at least 1"), 400
            context.weight = weight
        except ValueError:
            return jsonify(error="weight must be an integer"), 400

    if "enabled" in request.json:
        context.enabled = bool(request.json["enabled"])

    db.session.commit()

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    return jsonify(success="context updated")


@containers_bp.route("/api/contexts/delete/<int:context_id>", methods=["DELETE"])
@admins_only
def route_api_delete_context(context_id):
    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    db.session.delete(context)
    db.session.commit()

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    return jsonify(success="context deleted")


@containers_bp.route("/api/contexts/test/<int:context_id>", methods=["GET"])
@admins_only
def route_api_test_context(context_id):
    import docker as _docker

    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    if not context.hostname:
        return jsonify(error="no hostname configured for this context"), 400

    try:
        if "@" in context.hostname:
            url = f"ssh://{context.hostname}"
        else:
            url = f"ssh://root@{context.hostname}"

        client = _docker.DockerClient(base_url=url)
        client.ping()
        client.close()
        return jsonify(success="context is reachable")
    except Exception as e:
        return jsonify(error=f"context unreachable: {str(e)}"), 500


@containers_bp.route("/api/pull", methods=["POST"])
@admins_only
def route_pull_image():
    if not request.is_json:
        return jsonify(error="invalid request"), 400

    image = request.json.get("image")
    if not image:
        return jsonify(error="image is required"), 400

    context_name = request.json.get("context_name")

    container_manager = current_app.container_manager
    try:
        results = container_manager.pull_image(image, context_name)
    except ContainerException as err:
        return jsonify(error=str(err)), 500

    return jsonify(results=results)


@containers_bp.route("/api/settings", methods=["GET"])
@admins_only
def route_get_settings():
    current_settings = {}
    for key, default in DEFAULTS.items():
        current_settings[key] = {
            "value": get_setting(key),
            "default": default,
        }
    return jsonify(settings=current_settings)


@containers_bp.route("/api/settings", methods=["PUT"])
@admins_only
def route_update_settings():
    if not request.is_json:
        return jsonify(error="invalid request"), 400

    changed = request.json
    for key, value in changed.items():
        if key not in DEFAULTS:
            continue
        set_setting(key, value)

    container_manager = current_app.container_manager
    container_manager.reload_settings()

    return jsonify(success="settings updated")
