from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import re
import socket as _socket
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import UTC, datetime, tzinfo
from statistics import median

from flask import Response, current_app, jsonify, render_template, request, stream_with_context

from CTFd.models import Teams, Users, db
from CTFd.utils.decorators import admins_only
from CTFd.utils.user import get_current_user

from ..container_manager import ContainerManager, container_name
from ..coordination import InstanceCoordinator
from ..docker_host_manager import (
    LOCAL_CONTEXT_NAME,
    LOCAL_SOCKET_PATH,
    ImageInfo,
    _new_docker_client,
    _resolve_endpoint,
    discover_contexts,
    ping_endpoint,
)
from ..event_logger import (
    MetadataValue,
    dense_user_flags,
    event_logger,
    flag_share_message,
    flag_share_metadata,
    sparse_user_flags,
    user_flag_values,
)
from ..exceptions import ContainerException
from ..freshness import generate_secret
from ..messages import INVALID_REQUEST
from ..models import (
    CONTEXT_STATES,
    ContainerChallengeModel,
    ContainerFlagShareModel,
    ContainerHistoryModel,
    ContainerInfoModel,
    ContainerInstanceModel,
    ContainerSettingsModel,
    DockerContextModel,
)
from ..utils import (
    DEFAULTS,
    SETTING_SPECS,
    ValidationError,
    get_setting,
    is_team_mode,
    parse_strict_int,
    parse_timezone,
    set_setting,
    validate_settings_patch,
)
from . import containers_bp
from .helpers import cleanup_instance, get_hostname_for_context, kill_container, request_json, resolve_expiration

logger = logging.getLogger(__name__)

_MAX_ANALYTICS_ROWS = 50000
_MAX_SSE_CONNECTIONS = 10
_CONTEXT_TEST_TIMEOUT = 5  # seconds, this ping runs synchronously inside an admin request
# the settings value column is TEXT, an oversized write fails under mysql strict mode
_MAX_IMAGE_CACHE_BYTES = 60_000
_sse_connection_count = 0
_sse_connection_lock = threading.Lock()

_CONTEXT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DNS_NAME_RE = re.compile(
    r"^(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?$"
)
_SSH_TARGET_RE = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9][A-Za-z0-9.-]*)(?::[1-9][0-9]{0,4})?$"
)


def _log_admin_action(message: str, *, level: str = "info", **metadata: MetadataValue) -> None:
    admin = get_current_user()
    event_logger.log_event(
        "admin_action",
        message,
        user_id=admin.id if admin else None,
        username=admin.name if admin else None,
        level=level,
        metadata=metadata,
    )


def _flag_share_to_event(row: ContainerFlagShareModel) -> dict:
    if row.owner_team_id is not None:
        source_type = "teams"
        source_id = row.owner_team_id
        source_entity = row.owner_team.name if row.owner_team else None
    else:
        source_type = "users"
        source_id = row.owner_user_id
        source_entity = row.owner_user.name if row.owner_user else None

    meta = flag_share_metadata(
        row.challenge_id,
        row.challenge.name if row.challenge else None,
        source_id,
        source_entity,
        source_type,
        team_id=row.submitter_team_id,
        team_name=row.submitter_team.name if row.submitter_team else None,
    )

    submitter_name = row.submitter_user.name if row.submitter_user else None
    chal_name = row.challenge.name if row.challenge else "unknown"
    msg = flag_share_message(submitter_name, source_entity or "unknown", chal_name)

    return {
        "id": row.id,
        "timestamp": row.timestamp,
        "type": "flag_sharing",
        "level": "warning",
        "user_id": row.submitter_user_id,
        "username": submitter_name,
        "message": msg,
        "metadata": meta,
    }


def _get_connection_status(container_manager: ContainerManager) -> tuple[bool, set[str]]:
    try:
        connected = container_manager.is_connected()
    except ContainerException:
        connected = False

    try:
        running_ids = container_manager.get_running_container_ids()
    except ContainerException:
        running_ids = set()

    return connected, running_ids


def _running_container_row(
    instance: ContainerInstanceModel,
    *,
    container_id: str,
    name: str,
    image: str | None,
    challenge: str,
    challenge_id: int,
    user_obj: Users | None,
    user_id: int | None,
    port: int | None,
    created: int,
    expires: int,
    is_running: bool,
    hostname: str,
    connect_type: str | None,
    ssh_username: str | None,
    ssh_password: str | None,
    docker_context: str,
    stack_id: str | None,
    companion_count: int,
    cleanup_only: bool,
    team_mode: bool,
    team_obj: Teams | None,
    team_id: int | None,
) -> dict:
    row = {
        "container_id": container_id,
        "instance_id": instance.id,
        "container_name": name,
        "image": image,
        "challenge": challenge,
        "challenge_id": challenge_id,
        "user": user_obj.name if user_obj else "deleted user",
        "user_id": user_id,
        **(dense_user_flags(user_flag_values(user_obj)) if user_obj else {}),
        "port": port,
        "created": created,
        "expires": expires,
        "is_running": is_running,
        "hostname": hostname,
        "connect_type": connect_type,
        "ssh_username": ssh_username,
        "ssh_password": ssh_password,
        "docker_context": docker_context,
        "stack_id": stack_id,
        "companion_count": companion_count,
        "state": instance.state,
        "last_error": instance.last_error,
        "cleanup_only": cleanup_only,
    }
    if team_mode:
        row["team"] = team_obj.name if team_obj else "deleted team"
        row["team_id"] = team_id
    return row


@containers_bp.route("/dashboard", methods=["GET"])
@admins_only
def route_containers_dashboard():
    container_manager = current_app.container_manager
    running_containers = ContainerInfoModel.query.order_by(ContainerInfoModel.timestamp.desc()).all()

    connected, running_ids = _get_connection_status(container_manager)

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
    running_containers = (
        ContainerInfoModel.query.filter(ContainerInfoModel.entry_or_standalone())
        .options(
            db.joinedload(ContainerInfoModel.user),
            db.joinedload(ContainerInfoModel.team),
            db.joinedload(ContainerInfoModel.challenge),
        )
        .order_by(ContainerInfoModel.timestamp.desc())
        .all()
    )

    connected, running_ids = _get_connection_status(container_manager)

    team_mode = is_team_mode()

    running_containers_data = []
    physical_instance_ids = set()
    for container in running_containers:
        physical_instance_ids.add(container.instance_id)
        container.is_running = container.container_id in running_ids

        hostname = get_hostname_for_context(container.docker_context)

        cname = container_name(
            container.user_id or "deleted",
            container.challenge_id,
            container.timestamp,
            nonce=container.instance_id,
        )

        running_containers_data.append(
            _running_container_row(
                container.instance,
                container_id=container.container_id,
                name=cname,
                image=container.challenge.image,
                challenge=container.challenge.name,
                challenge_id=container.challenge_id,
                user_obj=container.user,
                user_id=container.user_id,
                port=container.port,
                created=container.timestamp,
                expires=container.expires,
                is_running=container.is_running,
                hostname=hostname,
                connect_type=container.challenge.ctype,
                ssh_username=container.challenge.ssh_username,
                ssh_password=container.challenge.ssh_password,
                docker_context=container.docker_context or "local",
                stack_id=container.stack_id,
                companion_count=ContainerInfoModel.query.filter_by(stack_id=container.stack_id, is_entry=False).count()
                if container.stack_id
                else 0,
                cleanup_only=False,
                team_mode=team_mode,
                team_obj=container.team,
                team_id=container.team_id,
            )
        )

    logical_only = ContainerInstanceModel.query.filter(~ContainerInstanceModel.id.in_(physical_instance_ids)).all()
    for instance in logical_only:
        challenge = instance.challenge
        context_name = instance.docker_context.context_name if instance.docker_context else None
        running_containers_data.append(
            _running_container_row(
                instance,
                container_id="",
                name=container_name(
                    instance.user_id or "deleted",
                    instance.challenge_id,
                    int(instance.created_at),
                    nonce=instance.id,
                ),
                image=challenge.image if challenge else None,
                challenge=challenge.name if challenge else "deleted challenge",
                challenge_id=instance.challenge_id,
                user_obj=instance.user,
                user_id=instance.user_id,
                port=None,
                created=int(instance.created_at),
                expires=int(instance.expires),
                is_running=False,
                hostname=get_hostname_for_context(context_name),
                connect_type=challenge.ctype if challenge else "tcp",
                ssh_username=None,
                ssh_password=None,
                docker_context=context_name or "unavailable",
                stack_id=instance.stack_id,
                companion_count=0,
                cleanup_only=True,
                team_mode=team_mode,
                team_obj=instance.team,
                team_id=instance.team_id,
            )
        )

    running_containers_data.sort(key=lambda row: row["created"], reverse=True)

    response_data = {
        "containers": running_containers_data,
        "connected": connected,
    }

    return jsonify(response_data)


@containers_bp.route("/api/user_flags", methods=["GET"])
@admins_only
def route_user_flags():
    from CTFd.models import Users

    rows = Users.query.with_entities(Users.id, Users.type, Users.hidden, Users.banned).all()
    flags = {}
    for uid, utype, hidden, banned in rows:
        f = sparse_user_flags((utype == "admin", hidden, banned))
        if f:
            flags[uid] = f
    return jsonify(flags)


@containers_bp.route("/api/stats/summary", methods=["GET"])
@admins_only
def route_stats_summary():
    active = ContainerInfoModel.query.filter(ContainerInfoModel.entry_or_standalone()).count()

    excluded = _excluded_user_ids()

    total_history = ContainerHistoryModel.query.filter(
        ContainerHistoryModel.stopped_at.isnot(None),
        ContainerHistoryModel.is_entry.is_(True),
    ).all()
    entry_rows = [row for row in total_history if row.user_id not in excluded]

    total = len(entry_rows) + active
    durations = [r.stopped_at - r.created_at for r in entry_rows if r.stopped_at and r.created_at]
    avg_duration = sum(durations) / len(durations) if durations else 0

    unique_users = (
        db.session.query(db.func.count(db.distinct(ContainerHistoryModel.user_id)))
        .filter(~ContainerHistoryModel.user_id.in_(excluded))
        .scalar()
        or 0
    )

    flag_shares = ContainerFlagShareModel.query.count()

    events_list = []
    for r in entry_rows:
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
        total=total,
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
    rows = (
        ContainerFlagShareModel.query.options(
            db.joinedload(ContainerFlagShareModel.submitter_user),
            db.joinedload(ContainerFlagShareModel.submitter_team),
            db.joinedload(ContainerFlagShareModel.owner_user),
            db.joinedload(ContainerFlagShareModel.owner_team),
            db.joinedload(ContainerFlagShareModel.challenge),
        )
        .order_by(ContainerFlagShareModel.timestamp.desc())
        .limit(500)
        .all()
    )
    events = [_flag_share_to_event(r) for r in rows]
    return jsonify(events=events)


@contextmanager
def _sse_connection_slot() -> Iterator[bool]:
    """claimed from inside the generator, a client that aborts before the first chunk never starts it"""
    global _sse_connection_count

    with _sse_connection_lock:
        claimed = _sse_connection_count < _MAX_SSE_CONNECTIONS
        if claimed:
            _sse_connection_count += 1
    try:
        yield claimed
    finally:
        if claimed:
            with _sse_connection_lock:
                _sse_connection_count -= 1


@containers_bp.route("/api/events/stream", methods=["GET"])
@admins_only
def route_events_stream():
    with _sse_connection_lock:
        if _sse_connection_count >= _MAX_SSE_CONNECTIONS:
            return jsonify(error="too many event stream connections"), 429

    def event_stream():
        with _sse_connection_slot() as claimed:
            if not claimed:
                return

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
        return jsonify(error=INVALID_REQUEST), 400

    container_id = (request_json() or {}).get("container_id")
    if not container_id:
        return jsonify(error="no container_id specified"), 400

    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
    user_name = container.user.name if container and container.user else None
    user_id = container.user_id if container else None
    chal_name = container.challenge.name if container and container.challenge else None

    result = kill_container(container_id)

    if "success" in result:
        _log_admin_action(
            f"admin killed container for {user_name or 'unknown'}",
            level="warning",
            action="kill",
            target=user_name,
            target_id=user_id,
            challenge_name=chal_name,
            container_id=container_id[:12],
        )

    status_code = 200 if "success" in result else 400
    return jsonify(result), status_code


@containers_bp.route("/api/admin_extend", methods=["POST"])
@admins_only
def route_admin_extend():
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400

    container_id = (request_json() or {}).get("container_id")
    if not container_id:
        return jsonify(error="no container_id specified"), 400

    container = ContainerInfoModel.query.filter_by(container_id=container_id).first()
    if not container:
        return jsonify(error="container not found"), 404

    challenge = container.challenge
    if not challenge:
        return jsonify(error="challenge not found"), 404

    expiration = resolve_expiration(challenge)
    new_expires = int(time.time() + expiration)

    update = InstanceCoordinator.extend_by_admin(container.instance_id, new_expires=new_expires)
    if update is None:
        return jsonify(error="container is not running or already expires later"), 409

    _log_admin_action(
        f"admin extended container for {container.user.name if container.user else 'unknown'}",
        level="info",
        action="extend",
        target=container.user.name if container.user else None,
        target_id=container.user_id,
        challenge_name=challenge.name,
    )

    return jsonify(success="extended")


@containers_bp.route("/api/purge", methods=["POST"])
@admins_only
def route_purge_containers():
    instances = ContainerInstanceModel.query.all()
    failures = []
    purged = 0
    for instance in instances:
        result = cleanup_instance(instance.id, reason="purged")
        if "success" in result:
            purged += 1
        else:
            failures.append(
                {
                    "instance_id": instance.id,
                    "error": result.get("error", "cleanup failed"),
                }
            )
    _log_admin_action(
        f"purged {purged} instances; {len(failures)} remain",
        level="warning",
        action="purge",
        purged=purged,
        failed=len(failures),
    )
    if failures:
        return jsonify(error="some instances remain pending cleanup", purged=purged, failures=failures), 503
    return jsonify(success="purged all instances", purged=purged), 200


@containers_bp.route("/api/cleanup", methods=["POST"])
@admins_only
def route_cleanup_instance():
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400
    instance_id = (request_json() or {}).get("instance_id")
    if not isinstance(instance_id, str) or not re.fullmatch(r"[0-9a-f]{32}", instance_id):
        return jsonify(error="invalid instance_id"), 400
    result = cleanup_instance(instance_id, reason="admin_cleanup")
    if "success" in result:
        _log_admin_action(
            f"admin cleaned instance {instance_id}",
            level="warning",
            action="cleanup",
            instance_id=instance_id,
        )
        return jsonify(result), 200
    return jsonify(result), 409


@containers_bp.route("/api/clear_history", methods=["POST"])
@admins_only
def route_clear_history():
    count = ContainerHistoryModel.query.count()
    ContainerHistoryModel.query.delete()
    db.session.commit()

    _log_admin_action(
        f"cleared {count} history records",
        level="warning",
        action="clear_history",
        count=count,
    )
    return jsonify(success=f"cleared {count} history records")


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


def _context_payload(*, creating: bool) -> dict[str, object]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValidationError("context payload must be a JSON object")

    allowed = {"hostname", "pub_hostname", "weight", "state"}
    if creating:
        allowed.add("context_name")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValidationError(f"unknown context field: {unknown[0]}")
    return payload


def _required_context_name(value: object) -> str:
    if not isinstance(value, str) or not _CONTEXT_NAME_RE.fullmatch(value):
        raise ValidationError("context_name must contain only letters, numbers, dot, underscore, or hyphen")
    return value


def _optional_ssh_target(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 512 or not _SSH_TARGET_RE.fullmatch(value):
        raise ValidationError("hostname must be a valid SSH host, user@host, or user@host:port")
    if value.rsplit(":", 1)[-1].isdigit() and ":" in value and not value.endswith("]"):
        port = int(value.rsplit(":", 1)[-1])
        if port > 65_535:
            raise ValidationError("hostname port must be at most 65535")
    return value


def _required_public_hostname(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise ValidationError("pub_hostname must be a valid hostname or IP address")
    candidate = value.strip()
    if candidate != value or any(char in candidate for char in ("/", "@", " ", "\t", "\n")):
        raise ValidationError("pub_hostname must be a valid hostname or IP address")
    try:
        address = ipaddress.ip_address(candidate.strip("[]"))
        return f"[{address.compressed}]" if address.version == 6 else address.compressed
    except ValueError:
        if not _DNS_NAME_RE.fullmatch(candidate):
            raise ValidationError("pub_hostname must be a valid hostname or IP address") from None
    return candidate


def _context_state(value: object, *, allow_retired: bool = False) -> str:
    allowed = set(CONTEXT_STATES if allow_retired else ("active", "draining", "disabled"))
    if not isinstance(value, str) or value not in allowed:
        raise ValidationError(f"state must be one of: {', '.join(sorted(allowed))}")
    return value


def _context_reference_counts(context: DockerContextModel) -> tuple[int, int]:
    physical = ContainerInfoModel.query.filter_by(docker_context=context.context_name).count()
    logical = ContainerInstanceModel.query.filter_by(docker_context_id=context.id).count()
    return physical, logical


def _context_is_reachable(context: DockerContextModel) -> bool:
    endpoint = _resolve_endpoint(context.context_name, context.hostname)
    return bool(endpoint and ping_endpoint(endpoint))


def _context_docker_resource_counts(context: DockerContextModel) -> tuple[int, int]:
    return current_app.container_manager.host_manager.count_resources_by_label(
        context.context_name,
        "ctf.instance_id",
    )


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
                "state": ctx.state,
                "health_state": ctx.health_state,
                "health_checked_at": ctx.health_checked_at,
                "health_error": ctx.health_error,
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
        return jsonify(error=INVALID_REQUEST), 400

    try:
        payload = _context_payload(creating=True)
        context_name = _required_context_name(payload.get("context_name"))
        hostname = _optional_ssh_target(payload.get("hostname"))
        pub_hostname = _required_public_hostname(payload.get("pub_hostname"))
        weight = parse_strict_int(payload.get("weight", 1), "weight", minimum=1, maximum=1_000)
        requested_state = _context_state(payload.get("state", "active"))
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400

    existing = DockerContextModel.query.filter_by(context_name=context_name).first()
    if existing:
        return jsonify(error="context already exists"), 400

    try:
        new_context = DockerContextModel(
            context_name=context_name,
            hostname=hostname,
            pub_hostname=pub_hostname,
            weight=weight,
            state=requested_state,
        )
        db.session.add(new_context)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to add docker context")
        return jsonify(error="failed to add context"), 500

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    event_logger.log_event(
        "context_changed",
        f"context {context_name} added",
        level="info",
        metadata={"action": "added", "context_name": context_name},
    )
    return jsonify(success="context added", id=new_context.id)


def _endpoint_change_conflict(context: DockerContextModel) -> tuple[Response, int] | None:
    physical_refs, logical_refs = _context_reference_counts(context)
    if physical_refs or logical_refs:
        return jsonify(error="stop and clean all instances before changing a context endpoint"), 409

    try:
        docker_containers, docker_networks = _context_docker_resource_counts(context)
    except Exception:
        logger.warning("could not verify Docker resource absence before endpoint update", exc_info=True)
        return jsonify(error="could not verify that the context has no Docker resources"), 503

    if not docker_containers and not docker_networks:
        return None

    return (
        jsonify(
            error="Docker resources remain on this context; automatic cleanup must finish first",
            docker_containers=docker_containers,
            docker_networks=docker_networks,
        ),
        409,
    )


@containers_bp.route("/api/contexts/update/<int:context_id>", methods=["PUT"])
@admins_only
def route_api_update_context(context_id):
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400

    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    try:
        payload = _context_payload(creating=False)
        updates: dict[str, object] = {}
        if "hostname" in payload:
            requested_hostname = _optional_ssh_target(payload["hostname"])
            if requested_hostname != context.hostname:
                conflict = _endpoint_change_conflict(context)
                if conflict is not None:
                    return conflict

            updates["hostname"] = requested_hostname

        if "pub_hostname" in payload:
            updates["pub_hostname"] = _required_public_hostname(payload["pub_hostname"])

        if "weight" in payload:
            updates["weight"] = parse_strict_int(payload["weight"], "weight", minimum=1, maximum=1_000)

        requested_state = context.state
        if "state" in payload:
            requested_state = _context_state(payload["state"])
        if context.state == "retired_orphaned" and requested_state != "retired_orphaned":
            raise ValidationError("retired contexts cannot be reactivated")

        updates["state"] = requested_state
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400

    try:
        for field, value in updates.items():
            setattr(context, field, value)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to update docker context")
        return jsonify(error="failed to update context"), 500

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

    force_raw = request.args.get("force", "false")
    if force_raw not in ("true", "false"):
        return jsonify(error="force must be true or false"), 400
    force = force_raw == "true"

    physical_refs, logical_refs = _context_reference_counts(context)
    pinned_challenges = ContainerChallengeModel.query.filter_by(docker_context=context.context_name).count()
    reachable = _context_is_reachable(context)
    name = context.context_name
    docker_containers = 0
    docker_networks = 0
    if reachable:
        try:
            docker_containers, docker_networks = _context_docker_resource_counts(context)
        except Exception:
            logger.warning("could not verify Docker resource absence before context deletion", exc_info=True)
            return jsonify(error="could not verify that the context has no Docker resources"), 503

    referenced = bool(physical_refs or logical_refs or pinned_challenges or docker_containers or docker_networks)
    retire_only = referenced or not reachable

    if retire_only and not force:
        reason = "context is referenced by resources or challenges" if referenced else "context is unreachable"
        return (
            jsonify(
                error=f"{reason}; retry with force=true to retire it without deleting its record",
                physical_references=physical_refs,
                logical_references=logical_refs,
                pinned_challenges=pinned_challenges,
                docker_containers=docker_containers,
                docker_networks=docker_networks,
                retirement_available=True,
            ),
            409,
        )

    try:
        if retire_only:
            context.state = "retired_orphaned"
            action = "retired"
            success = "context retired; record retained for cleanup"
        else:
            db.session.delete(context)
            action = "deleted"
            success = "context deleted"

        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to delete or retire docker context")
        return jsonify(error="failed to delete or retire context"), 500

    container_manager = current_app.container_manager
    container_manager.load_docker_contexts()

    event_logger.log_event(
        "context_changed",
        f"context {name} {action}",
        level="warning",
        metadata={"action": action, "context_name": name},
    )
    return jsonify(success=success, state="retired_orphaned" if action == "retired" else "deleted")


@containers_bp.route("/api/contexts/test/<int:context_id>", methods=["GET"])
@admins_only
def route_api_test_context(context_id):
    context = DockerContextModel.query.get(context_id)
    if not context:
        return jsonify(error="context not found"), 404

    endpoint = _resolve_endpoint(context.context_name, context.hostname)
    if not endpoint:
        return jsonify(error="no endpoint could be resolved for this context"), 400

    client = None
    try:
        client = _new_docker_client(endpoint, timeout=_CONTEXT_TEST_TIMEOUT)
        client.ping()
        return jsonify(success="context is reachable")
    except Exception as exc:  # noqa: BLE001
        logger.warning("context test failed: %s", exc)
        return jsonify(error="context unreachable"), 500
    finally:
        if client:
            try:
                client.close()
            except Exception:
                logger.debug("failed to close context test client", exc_info=True)


def _suggested_hostname(endpoint: str) -> str:
    if endpoint.startswith("unix://"):
        return _socket.gethostname()

    if "://" not in endpoint:
        return ""

    stripped = endpoint.split("://", 1)[-1]
    if "@" in stripped:
        stripped = stripped.split("@", 1)[-1]

    return stripped.split(":")[0].split("/")[0]


@containers_bp.route("/api/contexts/discover", methods=["GET"])
@admins_only
def route_api_discover_contexts():
    try:
        found = discover_contexts()
        existing = {ctx.context_name for ctx in DockerContextModel.query.all()}

        available = [
            {
                "name": ctx["name"],
                "endpoint": ctx["endpoint"],
                "suggested_hostname": _suggested_hostname(ctx["endpoint"]),
            }
            for ctx in found
            if ctx["name"] not in existing
        ]

        if not available:
            return jsonify(contexts=[])

        def _ping(ctx: dict[str, str | bool]) -> dict[str, str | bool]:
            ctx["reachable"] = ping_endpoint(str(ctx["endpoint"]))
            return ctx

        with ThreadPoolExecutor(max_workers=min(len(available), 8)) as pool:
            list(pool.map(_ping, available))

        return jsonify(contexts=available)
    except Exception:
        logger.exception("error discovering contexts")
        return jsonify(error="failed to discover contexts"), 500


@containers_bp.route("/api/images/matrix", methods=["GET"])
@admins_only
def route_api_images_matrix():
    container_manager = current_app.container_manager

    challenges = ContainerChallengeModel.query.all()
    challenge_images = sorted({c.image for c in challenges if c.image})

    if not challenge_images:
        return jsonify(images=[], contexts=[], matrix={})

    connected = container_manager.get_connected_contexts()
    if not connected:
        return jsonify(images=challenge_images, contexts=[], matrix={})

    def _list(ctx_name: str) -> tuple[str, set[str]]:
        return ctx_name, set(container_manager.host_manager.get_images(ctx_name))

    def _info(ctx_name: str, image: str) -> tuple[str, str, ImageInfo | None]:
        return ctx_name, image, container_manager.host_manager.get_image_info(ctx_name, image)

    context_images: dict[str, set[str]] = {}
    with ThreadPoolExecutor(max_workers=min(len(connected), 8)) as pool:
        futures = {pool.submit(_list, ctx): ctx for ctx in connected}
        for future in as_completed(futures, timeout=15):
            try:
                ctx_name, tags = future.result()
                context_images[ctx_name] = tags
            except Exception:
                logger.warning("failed to list images for context %s", futures[future], exc_info=True)
                context_images[futures[future]] = set()

    matrix: dict[str, dict[str, dict[str, bool | ImageInfo | None]]] = {}
    pending: list[tuple[str, str, Future[tuple[str, str, ImageInfo | None]]]] = []
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

        for display, ctx, info_future in pending:
            try:
                _, _, info = info_future.result(timeout=15)
                matrix[display][ctx]["info"] = info
            except Exception:
                logger.warning("failed to inspect image %s on context %s", display, ctx, exc_info=True)

    display_images = sorted(matrix.keys())

    payload = json.dumps({"matrix": matrix, "contexts": connected, "scanned_at": time.time()})
    if len(payload.encode()) > _MAX_IMAGE_CACHE_BYTES:
        logger.warning("image cache of %s bytes exceeds the settings column, skipping the write", len(payload))
    else:
        set_setting("image_cache", payload)

    return jsonify(images=display_images, contexts=connected, matrix=matrix)


def _load_image_cache() -> dict[str, object] | None:
    raw = get_setting("image_cache")
    if not raw or not isinstance(raw, str):
        return None
    try:
        cache = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(cache, dict) or not isinstance(cache.get("matrix"), dict):
        return None
    if "contexts" not in cache or "scanned_at" not in cache:
        return None

    return cache


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
    except Exception:
        logger.exception("error reloading contexts")
        return jsonify(error="failed to reload contexts"), 500


@containers_bp.route("/api/pull", methods=["POST"])
@admins_only
def route_pull_image():
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400

    image = (request_json() or {}).get("image")
    if not image:
        return jsonify(error="image is required"), 400

    context_name = (request_json() or {}).get("context_name")

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
        spec = SETTING_SPECS[key]
        current_settings[key] = {
            "value": "" if spec.sensitive else get_setting(key),
            "default": default,
            "type": spec.kind,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "apply_mode": spec.apply_mode,
            "sensitive": spec.sensitive,
            "configured": bool(get_setting(key)) if spec.sensitive else None,
        }
    return jsonify(settings=current_settings)


@containers_bp.route("/api/settings", methods=["PUT"])
@admins_only
def route_update_settings():
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400

    try:
        changed = validate_settings_patch(request.get_json(silent=True))
    except ValidationError as exc:
        return jsonify(error=str(exc)), 400
    if "freshness_secret" in changed:
        return jsonify(error="use the freshness secret controls to change this setting"), 400

    try:
        for key, value in changed.items():
            row = ContainerSettingsModel.query.filter_by(key=key).first()
            if row is None:
                db.session.add(ContainerSettingsModel(key=key, value=str(value)))
            else:
                row.value = str(value)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to update container settings")
        return jsonify(error="failed to update settings"), 500

    disruptive = sorted(key for key in changed if SETTING_SPECS[key].apply_mode == "live_disruptive")
    return jsonify(
        success="settings updated",
        disruptive=disruptive,
    )


@containers_bp.route("/api/settings/freshness-secret", methods=["POST"])
@admins_only
def route_update_freshness_secret():
    if not request.is_json:
        return jsonify(error=INVALID_REQUEST), 400
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or payload.get("action") not in {"regenerate", "disable"}:
        return jsonify(error="action must be regenerate or disable"), 400

    action = payload["action"]
    value = generate_secret() if action == "regenerate" else ""
    try:
        row = ContainerSettingsModel.query.filter_by(key="freshness_secret").first()
        if row is None:
            db.session.add(ContainerSettingsModel(key="freshness_secret", value=value))
        else:
            row.value = value
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("failed to update freshness secret")
        return jsonify(error="failed to update freshness secret"), 500

    _log_admin_action(
        f"freshness tokens {action}d" if action == "regenerate" else "freshness tokens disabled",
        action=f"freshness_{action}",
    )
    return jsonify(success="freshness secret updated", configured=bool(value))


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


def _range_param() -> str:
    return request.args.get("range", "7d")


def _range_cutoff() -> float:
    now = time.time()
    ranges = {"24h": 86400, "7d": 604800, "30d": 2592000}
    delta = ranges.get(_range_param())
    if delta:
        return now - delta
    return 0


def _request_tz() -> tzinfo:
    name = request.args.get("tz", "")
    if name:
        try:
            return parse_timezone(name, "tz")
        except ValidationError:
            pass
    return UTC


def _history_rows_since(cutoff: float) -> list[ContainerHistoryModel]:
    # entry filter runs before the row limit so companion rows cannot crowd out real launches
    query = ContainerHistoryModel.query.filter(ContainerHistoryModel.is_entry.is_(True))
    if cutoff > 0:
        query = query.filter(ContainerHistoryModel.created_at >= cutoff)
    return query.order_by(ContainerHistoryModel.created_at.desc()).limit(_MAX_ANALYTICS_ROWS).all()


def _excluded_user_ids() -> set[int]:
    from CTFd.models import Users

    rows = Users.query.filter(db.or_(Users.type == "admin", Users.hidden.is_(True))).all()
    return {u.id for u in rows}


@containers_bp.route("/api/analytics/activity", methods=["GET"])
@admins_only
def route_analytics_activity():
    rows = _history_rows_since(_range_cutoff())

    if _range_param() == "24h":
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

    labels = sorted(set(create_buckets) | set(stop_buckets))
    creates = [create_buckets.get(k, 0) for k in labels]
    stops = [stop_buckets.get(k, 0) for k in labels]

    return jsonify(labels=labels, creates=creates, stops=stops)


@containers_bp.route("/api/analytics/top_users", methods=["GET"])
@admins_only
def route_analytics_top_users():
    from CTFd.models import Users

    now = time.time()
    rows = _history_rows_since(_range_cutoff())

    excluded = _excluded_user_ids()
    user_stats = defaultdict(lambda: {"total_seconds": 0, "container_count": 0, "challenges": set()})

    for row in rows:
        if not row.user_id or row.user_id in excluded:
            continue
        stats = user_stats[row.user_id]
        end = row.stopped_at if row.stopped_at else now
        stats["total_seconds"] += end - row.created_at
        stats["container_count"] += 1
        if row.challenge_id:
            stats["challenges"].add(row.challenge_id)

    users_by_id = {u.id: u for u in Users.query.filter(Users.id.in_(user_stats.keys())).all()}

    result = []
    for user_id, stats in user_stats.items():
        user_obj = users_by_id.get(user_id)
        result.append(
            {
                "user_id": user_id,
                "username": user_obj.name if user_obj else f"user#{user_id}",
                **dense_user_flags(user_flag_values(user_obj)),
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

    now = time.time()
    rows = _history_rows_since(_range_cutoff())

    excluded = _excluded_user_ids()
    chal_stats = defaultdict(lambda: {"count": 0, "users": set(), "lifetimes": []})

    for row in rows:
        if not row.challenge_id or row.user_id in excluded:
            continue
        stats = chal_stats[row.challenge_id]
        stats["count"] += 1
        if row.user_id:
            stats["users"].add(row.user_id)
        end = row.stopped_at if row.stopped_at else now
        stats["lifetimes"].append(end - row.created_at)

    chal_names = {c.id: c.name for c in Challenges.query.filter(Challenges.id.in_(chal_stats.keys())).all()}

    result = []
    for chal_id, stats in chal_stats.items():
        unique_users = len(stats["users"])
        avg_lifetime = sum(stats["lifetimes"]) / len(stats["lifetimes"]) if stats["lifetimes"] else 0
        restarts_per_user = stats["count"] / unique_users if unique_users > 0 else 0

        result.append(
            {
                "challenge_id": chal_id,
                "name": chal_names.get(chal_id, f"challenge#{chal_id}"),
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
    from CTFd.models import Challenges, Solves

    cutoff = _range_cutoff()
    excluded = _excluded_user_ids()

    solve_query = Solves.query
    if cutoff > 0:
        solve_query = solve_query.filter(Solves.date >= cutoff)
    solves = solve_query.order_by(Solves.date.desc()).limit(_MAX_ANALYTICS_ROWS).all()

    from CTFd.models import Users

    chal_times = defaultdict(lambda: {"solves": [], "solve_count": 0})

    for solve in solves:
        solve_ts = solve.date.timestamp() if hasattr(solve.date, "timestamp") else float(solve.date)
        user_id = solve.user_id
        if user_id in excluded:
            continue
        team_id = getattr(solve, "team_id", None)
        challenge_id = solve.challenge_id

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
        if solve_time <= 0:
            continue
        stats = chal_times[challenge_id]
        stats["solves"].append({"time": round(solve_time, 1), "user_id": user_id})
        stats["solve_count"] += 1

    chal_names = {c.id: c.name for c in Challenges.query.filter(Challenges.id.in_(chal_times.keys())).all()}

    all_user_ids = set()
    for stats in chal_times.values():
        for s in stats["solves"]:
            all_user_ids.add(s["user_id"])
    user_names = {u.id: u.name for u in Users.query.filter(Users.id.in_(all_user_ids)).all()} if all_user_ids else {}

    result = []
    for chal_id, stats in chal_times.items():
        times = [s["time"] for s in stats["solves"]]
        for s in stats["solves"]:
            s["username"] = user_names.get(s["user_id"], f"user#{s['user_id']}")
        result.append(
            {
                "challenge_id": chal_id,
                "name": chal_names.get(chal_id, f"challenge#{chal_id}"),
                "solve_count": stats["solve_count"],
                "times": times,
                "solves": stats["solves"],
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

    rows = (
        ContainerFlagShareModel.query.options(db.joinedload(ContainerFlagShareModel.challenge))
        .filter(ContainerFlagShareModel.timestamp >= cutoff)
        .all()
    )

    if not rows:
        return jsonify(labels=[], counts=[], by_challenge={})

    bins: dict[int, int] = {}
    by_challenge: dict[str, int] = {}
    for r in rows:
        if not r.timestamp:
            continue
        hour = int(r.timestamp) // 3600 * 3600
        bins[hour] = bins.get(hour, 0) + 1
        cname = r.challenge.name if r.challenge else "unknown"
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
    tz = _request_tz()
    excluded = _excluded_user_ids()
    rows = ContainerHistoryModel.query.filter(ContainerHistoryModel.created_at >= cutoff).all()

    # weekday returns 0 for monday so columns stay mon first
    matrix = [[0] * 7 for _ in range(24)]
    for r in rows:
        if not r.created_at or r.user_id in excluded:
            continue
        dt = datetime.fromtimestamp(r.created_at, tz=tz)
        matrix[dt.hour][dt.weekday()] += 1

    # echarts heatmap series takes day, hour, value triples
    data = []
    for hour in range(24):
        for day in range(7):
            if matrix[hour][day] > 0:
                data.append([day, hour, matrix[hour][day]])

    return jsonify({"data": data, "days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]})
