from __future__ import annotations

from flask import request
from CTFd.models import Users
from CTFd.utils.decorators import (
    authed_only,
    during_ctf_time_only,
    require_verified_emails,
)
from CTFd.utils.user import get_current_user

from . import containers_bp
from .helpers import (
    connect_type,
    view_container_info,
    create_container,
    renew_container,
    kill_container,
    requires_visible_challenge,
)
from ..utils import is_team_mode, DEFAULTS, ratelimit_per_user, handle_container_errors, owner_filter
from ..models import ContainerInfoModel

# rate limit values are evaluated at import time (before app context),
# so we use hardcoded defaults here, changes require a restart
_RL_VIEW = DEFAULTS["rate_limit_requests"]
_RL_VIEW_INTERVAL = DEFAULTS["rate_limit_interval"]
_RL_MUTATE = 10
_RL_MUTATE_INTERVAL = 60


def validate_request(
    required_fields: list[str],
) -> tuple[dict[str, str] | None, int | None, Users | None]:
    user = get_current_user()

    # also guard non-dict json bodies (lists, strings, numbers). without this
    # check, request.json.get below raises AttributeError on a list payload
    if not isinstance(request.json, dict):
        return {"error": "invalid request"}, 400, None

    for field in required_fields:
        if not request.json.get(field):
            return {"error": f"no {field} specified"}, 400, None

    if "chal_id" in required_fields:
        try:
            int(request.json["chal_id"])
        except (TypeError, ValueError):
            return {"error": "invalid challenge id"}, 400, None

    if not user:
        return {"error": "user not found"}, 400, None

    if is_team_mode() and not user.team:
        return {"error": "user not a member of a team"}, 400, None

    return None, None, user


def _resolve_identity(user):
    """Return (xid, is_team) for the current user based on user/team mode"""
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
def route_stop_container():
    error_response, status_code, user = validate_request(["chal_id"])
    if error_response:
        return error_response, status_code

    chal_id = int(request.json["chal_id"])
    xid, is_team = _resolve_identity(user)

    running_container = ContainerInfoModel.query.filter_by(challenge_id=chal_id, **owner_filter(xid, is_team)).first()

    if running_container:
        return kill_container(running_container.container_id)
    else:
        return {"error": "no container found"}, 400
