from __future__ import annotations

import functools
import logging

from flask import jsonify, request
from CTFd.utils import get_config

from .exceptions import ContainerException, ContainerUnavailableException
from .models import ContainerSettingsModel

logger = logging.getLogger(__name__)


DEFAULTS: dict[str, int | str] = {
    "max_containers_per_user": 4,
    "rate_limit_requests": 45,
    "rate_limit_interval": 60,
    "expiration_check_interval": 5,
    "max_concurrent_creates": 2,
    "freshness_secret": "",
    "freshness_token_length": 6,
    "post_solve_expiry_seconds": 90,
    "default_expiration_seconds": 1800,
    "default_max_renewals": 2,
}

_TOKEN_LENGTH_KEY = "freshness_token_length"

USERS_MODE = "users"
TEAMS_MODE = "teams"


def get_setting(key: str, default: int | float | str | bool | None = None) -> int | float | str | bool | None:
    if default is None:
        default = DEFAULTS.get(key)

    # flask raises RuntimeError when current_app is accessed outside an app context
    try:
        from flask import current_app

        if not current_app:
            return default
    except RuntimeError:
        return default

    row = ContainerSettingsModel.query.filter_by(key=key).first()
    if row is None:
        return default

    return _coerce(row.value, default)


def set_setting(key: str, value: int | float | str | bool) -> None:
    from CTFd.models import db

    row = ContainerSettingsModel.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = ContainerSettingsModel(key=key, value=str(value))
        db.session.add(row)
    db.session.commit()


def _coerce(raw: str, default: int | float | str | bool | None) -> int | float | str | bool:
    if default is None:
        return raw

    target = type(default)
    if target is bool:
        return raw.lower() in ("true", "1", "yes") if isinstance(raw, str) else bool(raw)
    if target is int:
        return int(float(raw))
    if target is float:
        return float(raw)
    return raw


def settings_to_dict(settings_query: list[ContainerSettingsModel]) -> dict[str, str]:
    return {setting.key: setting.value for setting in settings_query}


def is_team_mode() -> bool | None:
    mode = get_config("user_mode")
    return mode == TEAMS_MODE if mode in (TEAMS_MODE, USERS_MODE) else None


def resolve_xid(user) -> int | None:
    # team id in team mode (None when team mode but the user has no team), user id otherwise
    if is_team_mode():
        if not user.team:
            return None
        return user.team.id
    return user.id


def owner_filter(xid: int, is_team: bool) -> dict[str, int]:
    return {"team_id" if is_team else "user_id": xid}


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


def handle_container_errors(f):
    # centralize ContainerException dispatch so routes stay focused on happy paths
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ContainerUnavailableException as err:
            return {"error": sanitize_container_error(err)}, 503
        except ContainerException as err:
            return {"error": sanitize_container_error(err)}, 500

    return wrapper


def ratelimit_per_user(method="POST", limit=50, interval=300, key_prefix="rl_user"):
    # ctfd's @ratelimit keys on ip, which falsely throttles students sharing
    # an egress ip (campus wifi, nat, vpn)
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from CTFd.cache import cache
            from CTFd.utils.user import get_current_user, get_ip

            if request.method != method:
                return f(*args, **kwargs)

            user = get_current_user()
            if user is not None:
                bucket = f"u{user.id}"
            else:
                bucket = f"ip{get_ip()}"
            key = f"{key_prefix}:{bucket}:{request.endpoint}"

            current = cache.get(key)
            if current is not None and int(current) >= limit:
                resp = jsonify(
                    {
                        "code": 429,
                        "message": f"Too many requests. Limit is {limit} requests in {interval} seconds",
                    }
                )
                resp.status_code = 429
                resp.headers["Retry-After"] = str(interval)
                return resp

            if current is None:
                cache.set(key, 1, timeout=interval)
            else:
                cache.set(key, int(current) + 1, timeout=interval)

            return f(*args, **kwargs)

        return wrapper

    return decorator
