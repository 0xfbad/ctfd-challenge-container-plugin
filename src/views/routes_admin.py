import queue
import json
import time
from collections import defaultdict
from statistics import median
import logging
import threading

import docker
from flask import request, render_template, current_app, jsonify, Response, stream_with_context
from CTFd.utils.decorators import admins_only
from CTFd.models import db

from . import containers_bp
from .helpers import kill_container, get_hostname_for_context
from ..utils import is_team_mode, get_setting, set_setting, DEFAULTS
from ..models import ContainerInfoModel, ContainerHistoryModel, DockerContextModel, ContainerChallengeModel
from ..container_manager import ContainerException
import os
from ..docker_host_manager import (
    LOCAL_CONTEXT_NAME,
    LOCAL_SOCKET_PATH,
    _resolve_endpoint,
    discover_contexts,
    ping_endpoint,
)
from ..event_logger import event_logger

logger = logging.getLogger(__name__)

_MAX_ANALYTICS_ROWS = 50000
_MAX_SSE_CONNECTIONS = 10
_sse_connection_count = 0
_sse_connection_lock = threading.Lock()


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
            "docker_context": container.docker_context or "local",
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


@containers_bp.route("/api/stats/summary", methods=["GET"])
@admins_only
def route_stats_summary():
    active = ContainerInfoModel.query.count()
    total = ContainerHistoryModel.query.count()

    rows = ContainerHistoryModel.query.filter(
        ContainerHistoryModel.stopped_at.isnot(None),
    ).all()

    durations = [r.stopped_at - r.created_at for r in rows if r.stopped_at and r.created_at]
    avg_duration = sum(durations) / len(durations) if durations else 0

    unique_users = db.session.query(db.func.count(db.distinct(ContainerHistoryModel.user_id))).scalar() or 0

    flag_shares = len([e for e in event_logger.get_recent_events(limit=2000) if e.get("type") == "flag_sharing"])

    # peak concurrent via sweep line on history
    events_list = []
    for r in ContainerHistoryModel.query.all():
        if r.created_at:
            events_list.append((r.created_at, 1))
        if r.stopped_at:
            events_list.append((r.stopped_at, -1))
    events_list.sort()
    peak = 0
    current = 0
    for _, delta in events_list:
        current += delta
        peak = max(peak, current)

    return jsonify(
        active=active,
        total=total + active,
        avg_duration=round(avg_duration),
        unique_users=unique_users,
        flag_shares=flag_shares,
        peak_concurrent=peak,
    )


@containers_bp.route("/api/events/recent", methods=["GET"])
@admins_only
def route_get_recent_events():
    events = event_logger.get_recent_events(limit=50)
    return jsonify(events=events)


@containers_bp.route("/api/flag_sharing", methods=["GET"])
@admins_only
def route_get_flag_sharing():
    all_events = event_logger.get_recent_events(limit=2000)
    sharing = [e for e in all_events if e.get("type") == "flag_sharing"]
    return jsonify(events=sharing)


@containers_bp.route("/api/events/stream", methods=["GET"])
@admins_only
def route_events_stream():
    global _sse_connection_count

    with _sse_connection_lock:
        if _sse_connection_count >= _MAX_SSE_CONNECTIONS:
            return jsonify(error="too many event stream connections"), 429
        _sse_connection_count += 1

    def event_stream():
        global _sse_connection_count
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
            with _sse_connection_lock:
                _sse_connection_count -= 1

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

    # get container info before killing for the event log
    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
    user_name = container.user.name if container and container.user else None
    user_id = container.user_id if container else None
    chal_name = container.challenge.name if container and container.challenge else None

    result = kill_container(container_id)

    if "success" in result:
        event_logger.log_event(
            "admin_action",
            f"admin killed container for {user_name or 'unknown'}",
            level="warning",
            metadata={
                "action": "kill",
                "target": user_name,
                "target_id": user_id,
                "challenge_name": chal_name,
                "container_id": container_id[:12],
            },
        )

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
    event_logger.log_event(
        "admin_action",
        f"purged {len(container_ids)} containers",
        level="warning",
        metadata={"action": "purge", "count": len(container_ids)},
    )
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
    contexts = container_manager.get_connected_contexts()
    return jsonify(contexts=contexts)


@containers_bp.route("/admin/contexts", methods=["GET"])
@admins_only
def route_list_contexts():
    return render_template("admin/contexts.html")


@containers_bp.route("/api/contexts/list", methods=["GET"])
@admins_only
def route_api_list_contexts():
    container_manager = current_app.container_manager
    connected = set(container_manager.get_connected_contexts())
    orch_status = {s["context_name"]: s for s in container_manager.orchestrator.get_status()}

    docker_socket = os.path.exists(LOCAL_SOCKET_PATH)

    contexts = DockerContextModel.query.all()
    contexts_data = []
    for ctx in contexts:
        info = orch_status.get(ctx.context_name, {})
        contexts_data.append(
            {
                "id": ctx.id,
                "context_name": ctx.context_name,
                "hostname": ctx.hostname,
                "pub_hostname": ctx.pub_hostname,
                "weight": ctx.weight,
                "enabled": ctx.enabled,
                "connected": ctx.context_name in connected,
                "healthy": info.get("healthy", False),
                "active_containers": info.get("active_containers", 0),
                "is_local": ctx.context_name == LOCAL_CONTEXT_NAME,
            }
        )

    return jsonify(contexts=contexts_data, docker_socket=docker_socket)


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

    event_logger.log_event(
        "context_changed",
        f"context {context_name} added",
        level="info",
        metadata={"action": "added", "context_name": context_name},
    )
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

    event_logger.log_event(
        "context_changed",
        f"context {context.context_name} updated",
        level="info",
        metadata={"action": "updated", "context_name": context.context_name},
    )
    return jsonify(success="context updated")


@containers_bp.route("/api/contexts/delete/<int:context_id>", methods=["DELETE"])
@admins_only
def route_api_delete_context(context_id):
    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    name = context.context_name
    db.session.delete(context)
    db.session.commit()

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    event_logger.log_event(
        "context_changed",
        f"context {name} deleted",
        level="warning",
        metadata={"action": "deleted", "context_name": name},
    )
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


@containers_bp.route("/api/contexts/discover", methods=["GET"])
@admins_only
def route_api_discover_contexts():
    import socket as _socket
    from concurrent.futures import ThreadPoolExecutor

    try:
        found = discover_contexts()
        existing = {ctx.context_name for ctx in DockerContextModel.query.all()}

        available = []
        for ctx in found:
            if ctx["name"] in existing:
                continue

            ep = ctx["endpoint"]
            if ep.startswith("unix://"):
                suggested = _socket.gethostname()
            elif "://" in ep:
                stripped = ep.split("://", 1)[-1]
                if "@" in stripped:
                    stripped = stripped.split("@", 1)[-1]
                suggested = stripped.split(":")[0].split("/")[0]
            else:
                suggested = ""

            available.append(
                {
                    "name": ctx["name"],
                    "endpoint": ctx["endpoint"],
                    "suggested_hostname": suggested,
                }
            )

        if available:

            def _ping(ctx):
                ctx["reachable"] = ping_endpoint(ctx["endpoint"])
                return ctx

            with ThreadPoolExecutor(max_workers=min(len(available), 8)) as pool:
                list(pool.map(_ping, available))

        return jsonify(contexts=available)
    except Exception as e:
        logger.error(f"error discovering contexts: {e}")
        return jsonify(error=str(e)), 500


@containers_bp.route("/api/images/matrix", methods=["GET"])
@admins_only
def route_api_images_matrix():
    from concurrent.futures import ThreadPoolExecutor, as_completed

    container_manager = current_app.container_manager

    challenges = ContainerChallengeModel.query.all()
    challenge_images = sorted({c.image for c in challenges if c.image})

    if not challenge_images:
        return jsonify(images=[], contexts=[], matrix={})

    connected = container_manager.get_connected_contexts()
    if not connected:
        return jsonify(images=challenge_images, contexts=[], matrix={})

    # list images and fetch metadata on each context in parallel
    def _list(ctx_name):
        return ctx_name, set(container_manager.host_manager.get_images(ctx_name))

    def _info(ctx_name, image):
        return ctx_name, image, container_manager.host_manager.get_image_info(ctx_name, image)

    context_images: dict[str, set] = {}
    with ThreadPoolExecutor(max_workers=min(len(connected), 8)) as pool:
        futures = {pool.submit(_list, ctx): ctx for ctx in connected}
        for future in as_completed(futures, timeout=15):
            try:
                ctx_name, tags = future.result()
                context_images[ctx_name] = tags
            except Exception:
                context_images[futures[future]] = set()

    # build matrix keyed by display name (strip :latest)
    matrix: dict[str, dict] = {}
    pending: list[tuple[str, str, object]] = []
    max_workers = min(max(len(connected), 1) * max(len(challenge_images), 1), 16)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for img in challenge_images:
            normalized = img if ":" in img else f"{img}:latest"
            display = img.removesuffix(":latest")
            matrix[display] = {}

            for ctx in connected:
                tags = context_images.get(ctx, set())
                present = normalized in tags or img in tags
                matrix[display][ctx] = {"available": present}

                if present:
                    docker_name = normalized if normalized in tags else img
                    pending.append((display, ctx, pool.submit(_info, ctx, docker_name)))

        for display, ctx, future in pending:
            try:
                _, _, info = future.result(timeout=15)
                matrix[display][ctx]["info"] = info
            except Exception:
                pass

    display_images = sorted(matrix.keys())

    set_setting("image_cache", json.dumps({"matrix": matrix, "contexts": connected, "scanned_at": time.time()}))

    return jsonify(images=display_images, contexts=connected, matrix=matrix)


def _load_image_cache():
    raw = get_setting("image_cache")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


@containers_bp.route("/api/images/cache", methods=["GET"])
@admins_only
def route_api_images_cache():
    cache = _load_image_cache()
    if not cache:
        return jsonify(cached=False)

    return jsonify(
        cached=True,
        images=sorted(cache["matrix"].keys()),
        contexts=cache["contexts"],
        matrix=cache["matrix"],
        scanned_at=cache["scanned_at"],
    )


@containers_bp.route("/api/images/status", methods=["GET"])
@admins_only
def route_api_image_status():
    image = request.args.get("image", "").removesuffix(":latest")
    cache = _load_image_cache()

    if not cache:
        return jsonify(cached=False)

    contexts = cache["matrix"].get(image, {})

    return jsonify(cached=True, image=image, contexts=contexts, scanned_at=cache["scanned_at"])


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
    rows = query.order_by(ContainerHistoryModel.created_at.desc()).limit(_MAX_ANALYTICS_ROWS).all()

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
    rows = query.order_by(ContainerHistoryModel.created_at.desc()).limit(_MAX_ANALYTICS_ROWS).all()

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
    rows = query.order_by(ContainerHistoryModel.created_at.desc()).limit(_MAX_ANALYTICS_ROWS).all()

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
    solves = solve_query.order_by(Solves.date.desc()).limit(_MAX_ANALYTICS_ROWS).all()

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


@containers_bp.route("/api/analytics/flag_sharing", methods=["GET"])
@admins_only
def route_analytics_flag_sharing():
    cutoff = _range_cutoff()

    all_events = event_logger.get_recent_events(limit=2000)
    sharing = [e for e in all_events if e.get("type") == "flag_sharing" and e.get("timestamp", 0) >= cutoff]

    if not sharing:
        return jsonify(labels=[], counts=[], by_challenge={})

    # bin by hour
    bins: dict[int, int] = {}
    by_challenge: dict[str, int] = {}
    for e in sharing:
        hour = int(e["timestamp"]) // 3600 * 3600
        bins[hour] = bins.get(hour, 0) + 1
        cname = (e.get("metadata") or {}).get("challenge_name", "unknown")
        by_challenge[cname] = by_challenge.get(cname, 0) + 1

    if bins:
        min_t = min(bins)
        max_t = max(bins)
        labels = list(range(min_t, max_t + 3600, 3600))
        counts = [bins.get(t, 0) for t in labels]
    else:
        labels, counts = [], []

    return jsonify(labels=labels, counts=counts, by_challenge=by_challenge)


@containers_bp.route("/api/analytics/heatmap", methods=["GET"])
@admins_only
def route_analytics_heatmap():
    cutoff = _range_cutoff()

    rows = ContainerHistoryModel.query.filter(ContainerHistoryModel.created_at >= cutoff).all()

    # build hour-of-day x day-of-week matrix
    from datetime import datetime

    matrix = [[0] * 7 for _ in range(24)]
    for r in rows:
        if not r.created_at:
            continue
        dt = datetime.fromtimestamp(r.created_at)
        matrix[dt.hour][dt.weekday()] += 1

    # echarts heatmap format: [[day, hour, value], ...]
    data = []
    for hour in range(24):
        for day in range(7):
            if matrix[hour][day] > 0:
                data.append([day, hour, matrix[hour][day]])

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    hours = [f"{h:02d}:00" for h in range(24)]

    return jsonify(data=data, days=days, hours=hours)
