from __future__ import annotations

from CTFd.models import Users
from CTFd.utils.decorators import (
    authed_only,
    during_ctf_time_only,
    require_verified_emails,
)
from CTFd.utils.user import get_current_user

from ..messages import (
    INVALID_CHALLENGE_ID,
    INVALID_REQUEST,
    MISSING_FIELD,
    NO_CONTAINER,
    TEAM_REQUIRED,
    USER_NOT_FOUND,
)
from ..models import ContainerInfoModel
from ..utils import error_body, handle_container_errors, is_team_mode, owner_filter, ratelimit_per_user
from . import containers_bp
from .helpers import (
    connect_type,
    create_container,
    kill_container,
    renew_container,
    request_json,
    requires_visible_challenge,
    view_container_info,
)

# setting keys resolve per request, decorators bind before an app context exists
_RL_VIEW = "rate_limit_requests"
_RL_VIEW_INTERVAL = "rate_limit_interval"
_RL_MUTATE = "mutation_rate_limit_requests"
_RL_MUTATE_INTERVAL = "mutation_rate_limit_interval"


def validate_request(
    required_fields: list[str],
) -> tuple[dict[str, str] | None, int | None, Users | None]:
    user = get_current_user()

    # a list or scalar json body would make the get calls below raise AttributeError
    body = request_json()
    if body is None:
        return error_body(INVALID_REQUEST, "user"), 400, None

    for field in required_fields:
        if not body.get(field):
            return error_body(MISSING_FIELD.format(field=field), "transient"), 400, None

    if "chal_id" in required_fields:
        try:
            int(body["chal_id"])
        except (TypeError, ValueError):
            return error_body(INVALID_CHALLENGE_ID, "user"), 400, None

    if not user:
        return error_body(USER_NOT_FOUND, "transient"), 400, None

    if is_team_mode() and not user.team:
        return error_body(TEAM_REQUIRED, "user"), 400, None

    return None, None, user


def _resolve_identity(user: Users) -> tuple[int, bool]:
    if is_team_mode():
        return user.team.id, True

    return user.id, False


@containers_bp.route("/api/get_connect_type/<int:challenge_id>", methods=["GET"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit_per_user(
    method="GET",
    limit=_RL_VIEW,
    interval=_RL_VIEW_INTERVAL,
)
@handle_container_errors
@requires_visible_challenge
def get_connect_type_route(challenge_id):
    return connect_type(challenge_id)


@containers_bp.route("/api/view_info", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit_per_user(
    method="POST",
    limit=_RL_VIEW,
    interval=_RL_VIEW_INTERVAL,
)
@handle_container_errors
@requires_visible_challenge
def route_view_info():
    error_response, status_code, user = validate_request(["chal_id"])
    if error_response:
        return error_response, status_code

    chal_id = int((request_json() or {})["chal_id"])
    xid, is_team = _resolve_identity(user)
    return view_container_info(chal_id, xid, is_team)


@containers_bp.route("/api/request", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit_per_user(
    method="POST",
    limit=_RL_MUTATE,
    interval=_RL_MUTATE_INTERVAL,
)
@handle_container_errors
@requires_visible_challenge
def route_request_container():
    error_response, status_code, user = validate_request(["chal_id"])
    if error_response:
        return error_response, status_code

    chal_id = int((request_json() or {})["chal_id"])
    xid, is_team = _resolve_identity(user)
    return create_container(chal_id, xid, user.id, is_team)


@containers_bp.route("/api/renew", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit_per_user(
    method="POST",
    limit=_RL_MUTATE,
    interval=_RL_MUTATE_INTERVAL,
)
@handle_container_errors
@requires_visible_challenge
def route_renew_container_route():
    error_response, status_code, user = validate_request(["chal_id"])
    if error_response:
        return error_response, status_code

    chal_id = int((request_json() or {})["chal_id"])
    xid, is_team = _resolve_identity(user)
    return renew_container(chal_id, xid, is_team)


@containers_bp.route("/api/stop", methods=["POST"])
@authed_only
@during_ctf_time_only
@require_verified_emails
@ratelimit_per_user(
    method="POST",
    limit=_RL_MUTATE,
    interval=_RL_MUTATE_INTERVAL,
)
@handle_container_errors
# no visibility guard here, a hidden challenge discloses nothing on stop and blocking it would strand the owner quota slot
def route_stop_container():
    error_response, status_code, user = validate_request(["chal_id"])
    if error_response:
        return error_response, status_code

    chal_id = int((request_json() or {})["chal_id"])
    xid, is_team = _resolve_identity(user)

    running_container = ContainerInfoModel.query.filter_by(challenge_id=chal_id, **owner_filter(xid, is_team)).first()

    if running_container is None:
        return error_body(NO_CONTAINER, "user"), 400

    result = kill_container(running_container.container_id)
    if "success" in result:
        return result

    kind = result.get("error_kind")
    return result, 400 if kind == "user" else 503
