from __future__ import annotations

from flask import request

from CTFd.models import Users
from CTFd.utils.decorators import (
    authed_only,
    during_ctf_time_only,
    require_verified_emails,
)
from CTFd.utils.user import get_current_user

from ..models import ContainerInfoModel
from ..utils import DEFAULTS, error_body, handle_container_errors, is_team_mode, owner_filter, ratelimit_per_user
from . import containers_bp
from .helpers import (
    connect_type,
    create_container,
    kill_container,
    renew_container,
    requires_visible_challenge,
    view_container_info,
)

# keys and literal defaults resolve to settings per request, decorators bind before an app context exists
_RL_VIEW = DEFAULTS["rate_limit_requests"]
_RL_VIEW_INTERVAL = DEFAULTS["rate_limit_interval"]
_RL_MUTATE = "mutation_rate_limit_requests"
_RL_MUTATE_INTERVAL = "mutation_rate_limit_interval"


def validate_request(
    required_fields: list[str],
) -> tuple[dict[str, str] | None, int | None, Users | None]:
    user = get_current_user()

    # a list or scalar json body would make the get calls below raise AttributeError
    if not isinstance(request.json, dict):
        return error_body("invalid request", "user"), 400, None

    for field in required_fields:
        if not request.json.get(field):
            return error_body(f"no {field} specified", "transient"), 400, None

    if "chal_id" in required_fields:
        try:
            int(request.json["chal_id"])
        except (TypeError, ValueError):
            return error_body("invalid challenge id", "user"), 400, None

    if not user:
        return error_body("user not found", "transient"), 400, None

    if is_team_mode() and not user.team:
        return error_body("user not a member of a team", "user"), 400, None

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

    chal_id = int(request.json["chal_id"])
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

    chal_id = int(request.json["chal_id"])
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

    chal_id = int(request.json["chal_id"])
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

    chal_id = int(request.json["chal_id"])
    xid, is_team = _resolve_identity(user)

    running_container = ContainerInfoModel.query.filter_by(challenge_id=chal_id, **owner_filter(xid, is_team)).first()

    if running_container is None:
        return error_body("no container found", "user"), 400

    return kill_container(running_container.container_id)
