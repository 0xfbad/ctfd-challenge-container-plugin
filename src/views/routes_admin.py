import queue
import json
import time
from collections import defaultdict
from statistics import median

import docker
from flask import request, render_template, current_app, jsonify, Response, stream_with_context
from CTFd.utils.decorators import admins_only
from CTFd.models import db

from . import containers_bp
from .helpers import kill_container, get_hostname_for_context
from ..utils import is_team_mode, get_setting, set_setting, DEFAULTS
from ..models import ContainerInfoModel, ContainerHistoryModel, DockerContextModel
from ..container_manager import ContainerException, LOCAL_CONTEXT_NAME, _resolve_endpoint
from ..event_logger import event_logger


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
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

        event_logger.add_listener(listener)

        try:
            recent_events = event_logger.get_recent_events(limit=200)
            for event in recent_events:
                yield f"data: {json.dumps(event)}\n\n"

            while True:
                try:
                    event_data = q.get(timeout=30)
                    yield f"data: {json.dumps(event_data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"

        finally:
            event_logger.remove_listener(listener)

    return Response(
        stream_with_context(event_stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
        contexts = DockerContextModel.query.all()
    except Exception:
        contexts = []

    connected_set = set(container_manager.get_connected_contexts())

    return render_template("admin/contexts.html", contexts=contexts, connected_set=connected_set)


@containers_bp.route("/api/contexts/list", methods=["GET"])
@admins_only
def route_api_list_contexts():
    container_manager = current_app.container_manager
    connected = set(container_manager.get_connected_contexts())

    contexts = DockerContextModel.query.all()
    contexts_data = [
        {
            "id": ctx.id,
            "context_name": ctx.context_name,
            "hostname": ctx.hostname,
            "pub_hostname": ctx.pub_hostname,
            "weight": ctx.weight,
            "enabled": ctx.enabled,
            "connected": ctx.context_name in connected,
            "is_local": ctx.context_name == LOCAL_CONTEXT_NAME,
        }
        for ctx in contexts
    ]
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

    if not pub_hostname:
        return jsonify(error="pub_hostname is required"), 400

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
        context_name=context_name, hostname=hostname or None, pub_hostname=pub_hostname, weight=weight, enabled=enabled
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
        context.hostname = request.json["hostname"] or None

    if "pub_hostname" in request.json:
        if not request.json["pub_hostname"]:
            return jsonify(error="pub_hostname cannot be empty"), 400
        context.pub_hostname = request.json["pub_hostname"]

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
    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    endpoint = _resolve_endpoint(context.context_name, context.hostname)
    if not endpoint:
        return jsonify(error="no endpoint could be resolved for this context"), 400

    try:
        client = docker.DockerClient(base_url=endpoint)
        client.ping()
        client.close()
        return jsonify(success="context is reachable")
    except Exception as e:
        return jsonify(error=f"context unreachable: {str(e)}"), 500


@containers_bp.route("/api/contexts/reload", methods=["POST"])
@admins_only
def route_api_reload_contexts():
    container_manager = current_app.container_manager
    try:
        container_manager.load_docker_contexts()
        return jsonify(success="contexts reloaded")
    except Exception as e:
        return jsonify(error=str(e)), 500


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


@containers_bp.route("/api/logs/<container_id>", methods=["GET"])
@admins_only
def route_get_container_logs(container_id):
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
    if not container:
        return jsonify(error="container not found"), 404

    tail = request.args.get("tail", 200, type=int)
    tail = max(1, min(tail, 1000))

    container_manager = current_app.container_manager
    try:
        logs = container_manager.get_container_logs(container_id, container.docker_context, tail=tail)
    except ContainerException as err:
        return jsonify(error=str(err)), 500

    return jsonify(logs=logs)


def _range_cutoff():
    range_param = request.args.get("range", "7d")
    now = time.time()
    ranges = {"24h": 86400, "7d": 604800, "30d": 2592000}
    delta = ranges.get(range_param)
    if delta:
        return now - delta
    return 0


@containers_bp.route("/admin/stats", methods=["GET"])
@admins_only
def route_analytics_page():
    return render_template("admin/analytics.html")


@containers_bp.route("/api/analytics/activity", methods=["GET"])
@admins_only
def route_analytics_activity():
    cutoff = _range_cutoff()
    range_param = request.args.get("range", "7d")

    query = ContainerHistoryModel.query
    if cutoff > 0:
        query = query.filter(ContainerHistoryModel.created_at >= cutoff)
    rows = query.all()

    # hourly buckets for 24h, daily for everything else
    if range_param == "24h":
        bucket_size = 3600
    else:
        bucket_size = 86400

    create_buckets = defaultdict(int)
    stop_buckets = defaultdict(int)

    for row in rows:
        bucket = int(row.created_at // bucket_size) * bucket_size
        create_buckets[bucket] += 1
        if row.stopped_at:
            stop_bucket = int(row.stopped_at // bucket_size) * bucket_size
            stop_buckets[stop_bucket] += 1

    all_keys = sorted(set(create_buckets.keys()) | set(stop_buckets.keys()))
    labels = [k for k in all_keys]
    creates = [create_buckets.get(k, 0) for k in all_keys]
    stops = [stop_buckets.get(k, 0) for k in all_keys]

    return jsonify(labels=labels, creates=creates, stops=stops)


@containers_bp.route("/api/analytics/top_users", methods=["GET"])
@admins_only
def route_analytics_top_users():
    from CTFd.models import Users

    cutoff = _range_cutoff()
    now = time.time()

    query = ContainerHistoryModel.query
    if cutoff > 0:
        query = query.filter(ContainerHistoryModel.created_at >= cutoff)
    rows = query.all()

    user_stats = defaultdict(lambda: {"total_seconds": 0, "container_count": 0, "challenges": set()})

    for row in rows:
        if not row.user_id:
            continue
        stats = user_stats[row.user_id]
        end = row.stopped_at if row.stopped_at else now
        stats["total_seconds"] += end - row.created_at
        stats["container_count"] += 1
        if row.challenge_id:
            stats["challenges"].add(row.challenge_id)

    result = []
    for user_id, stats in user_stats.items():
        user = Users.query.get(user_id)
        username = user.name if user else f"user#{user_id}"
        result.append(
            {
                "user_id": user_id,
                "username": username,
                "total_seconds": round(stats["total_seconds"], 1),
                "container_count": stats["container_count"],
                "unique_challenges": len(stats["challenges"]),
            }
        )

    result.sort(key=lambda x: x["total_seconds"], reverse=True)
    return jsonify(result[:20])


@containers_bp.route("/api/analytics/challenges", methods=["GET"])
@admins_only
def route_analytics_challenges():
    from CTFd.models import Challenges

    cutoff = _range_cutoff()
    now = time.time()

    query = ContainerHistoryModel.query
    if cutoff > 0:
        query = query.filter(ContainerHistoryModel.created_at >= cutoff)
    rows = query.all()

    chal_stats = defaultdict(lambda: {"count": 0, "users": set(), "lifetimes": []})

    for row in rows:
        if not row.challenge_id:
            continue
        stats = chal_stats[row.challenge_id]
        stats["count"] += 1
        if row.user_id:
            stats["users"].add(row.user_id)
        end = row.stopped_at if row.stopped_at else now
        stats["lifetimes"].append(end - row.created_at)

    result = []
    for chal_id, stats in chal_stats.items():
        challenge = Challenges.query.get(chal_id)
        name = challenge.name if challenge else f"challenge#{chal_id}"
        unique_users = len(stats["users"])
        avg_lifetime = sum(stats["lifetimes"]) / len(stats["lifetimes"]) if stats["lifetimes"] else 0
        restarts_per_user = stats["count"] / unique_users if unique_users > 0 else 0

        result.append(
            {
                "challenge_id": chal_id,
                "name": name,
                "container_count": stats["count"],
                "unique_users": unique_users,
                "avg_lifetime": round(avg_lifetime, 1),
                "restarts_per_user": round(restarts_per_user, 2),
            }
        )

    result.sort(key=lambda x: x["container_count"], reverse=True)
    return jsonify(result)


@containers_bp.route("/api/analytics/solve_times", methods=["GET"])
@admins_only
def route_analytics_solve_times():
    from CTFd.models import Challenges

    try:
        from CTFd.models import Solves
    except ImportError:
        return jsonify([])

    cutoff = _range_cutoff()

    solve_query = Solves.query
    if cutoff > 0:
        solve_query = solve_query.filter(Solves.date >= cutoff)
    solves = solve_query.all()

    chal_times = defaultdict(lambda: {"times": [], "solve_count": 0})

    for solve in solves:
        solve_ts = solve.date.timestamp() if hasattr(solve.date, "timestamp") else float(solve.date)
        user_id = solve.user_id
        team_id = getattr(solve, "team_id", None)
        challenge_id = solve.challenge_id

        # find the most recent history row for this user/team + challenge created before the solve
        history_query = ContainerHistoryModel.query.filter(
            ContainerHistoryModel.challenge_id == challenge_id,
            ContainerHistoryModel.created_at <= solve_ts,
        )
        if team_id:
            history_query = history_query.filter(ContainerHistoryModel.team_id == team_id)
        else:
            history_query = history_query.filter(ContainerHistoryModel.user_id == user_id)

        history = history_query.order_by(ContainerHistoryModel.created_at.desc()).first()
        if not history:
            continue

        solve_time = solve_ts - history.created_at
        stats = chal_times[challenge_id]
        stats["times"].append(round(solve_time, 1))
        stats["solve_count"] += 1

    result = []
    for chal_id, stats in chal_times.items():
        challenge = Challenges.query.get(chal_id)
        name = challenge.name if challenge else f"challenge#{chal_id}"
        times = stats["times"]

        result.append(
            {
                "challenge_id": chal_id,
                "name": name,
                "solve_count": stats["solve_count"],
                "times": times,
                "avg_time": round(sum(times) / len(times), 1) if times else 0,
                "median_time": round(median(times), 1) if times else 0,
                "fastest_time": round(min(times), 1) if times else 0,
            }
        )

    result.sort(key=lambda x: x["solve_count"], reverse=True)
    return jsonify(result)
