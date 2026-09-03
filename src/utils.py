from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import jsonify, request

from CTFd.utils import get_config

from .exceptions import ContainerException, ContainerUnavailableException
from .messages import (
    CHALLENGE_NOT_FOUND,
    CONTAINER_NOT_FOUND,
    CONTAINER_NOT_FOUND_RESET,
    CPU_LIMIT_INVALID,
    IMAGE_NOT_FOUND,
    MEMORY_LIMIT_INVALID,
    NO_RENEWALS,
    RATE_LIMITED,
    REQUEST_IN_PROGRESS,
    SERVER_ERROR,
)
from .models import ContainerSettingsModel

logger = logging.getLogger(__name__)


SettingValue = int | str
SettingKind = Literal["integer", "duration", "string"]
ApplyMode = Literal["live", "live_disruptive"]


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SettingSpec:
    default: SettingValue
    kind: SettingKind
    minimum: int | None = None
    maximum: int | None = None
    max_length: int | None = None
    allow_empty: bool = False
    apply_mode: ApplyMode = "live"
    sensitive: bool = False

    def parse(self, value: object, key: str) -> SettingValue:
        if self.kind == "string":
            if not isinstance(value, str):
                raise ValidationError(f"{key} must be a string")
            if not value and not self.allow_empty:
                raise ValidationError(f"{key} must not be empty")
            if self.max_length is not None and len(value) > self.max_length:
                raise ValidationError(f"{key} must be at most {self.max_length} characters")
            return value
        if self.kind == "duration":
            return parse_duration_seconds(
                value,
                key,
                minimum=self.minimum if self.minimum is not None else 0,
                maximum=self.maximum,
            )
        return parse_strict_int(value, key, minimum=self.minimum, maximum=self.maximum)


SETTING_SPECS: dict[str, SettingSpec] = {
    "max_containers_per_user": SettingSpec(4, "integer", minimum=1, maximum=100),
    "rate_limit_requests": SettingSpec(45, "integer", minimum=1, maximum=10_000),
    "rate_limit_interval": SettingSpec(60, "duration", minimum=1, maximum=86_400),
    "mutation_rate_limit_requests": SettingSpec(10, "integer", minimum=1, maximum=10_000),
    "mutation_rate_limit_interval": SettingSpec(60, "duration", minimum=1, maximum=86_400),
    "expiration_check_interval": SettingSpec(5, "duration", minimum=1, maximum=3_600),
    "max_concurrent_creates": SettingSpec(2, "integer", minimum=1, maximum=32),
    "freshness_secret": SettingSpec(
        "", "string", max_length=4_096, allow_empty=True, apply_mode="live_disruptive", sensitive=True
    ),
    "freshness_token_length": SettingSpec(6, "integer", minimum=4, maximum=16, apply_mode="live_disruptive"),
    "post_solve_expiry_seconds": SettingSpec(90, "duration", minimum=0, maximum=604_800),
    "default_expiration_seconds": SettingSpec(1_800, "duration", minimum=1, maximum=604_800),
    "default_max_renewals": SettingSpec(2, "integer", minimum=0, maximum=1_000),
    "default_max_memory_mb": SettingSpec(512, "integer", minimum=6, maximum=1_048_576),
    "default_max_cpu_millicores": SettingSpec(1_000, "integer", minimum=10, maximum=1_024_000),
}

DEFAULTS: dict[str, SettingValue] = {key: spec.default for key, spec in SETTING_SPECS.items()}

_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
MAX_JSON_BYTES = 65_536
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 1_000

_TOKEN_LENGTH_KEY = "freshness_token_length"

USERS_MODE = "users"
TEAMS_MODE = "teams"


def get_setting(key: str, default: float | str | bool | None = None) -> int | float | str | bool | None:
    if default is None:
        default = DEFAULTS.get(key)

    try:
        from flask import current_app

        if not current_app:
            return default
    except RuntimeError:  # current_app raises outside an app context
        return default

    row = ContainerSettingsModel.query.filter_by(key=key).first()
    if row is None:
        return default

    try:
        return _coerce(row.value, default)
    except (TypeError, ValueError):
        logger.warning("invalid stored value for container setting %s; using default", key)
        return default


def set_setting(key: str, value: float | str | bool) -> None:
    from CTFd.models import db

    row = ContainerSettingsModel.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = ContainerSettingsModel(key=key, value=str(value))
        db.session.add(row)
    db.session.commit()


def _coerce(raw: str, default: float | str | bool | None) -> int | float | str | bool:
    if default is None:
        return raw

    target = type(default)
    if target is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in ("true", "1", "yes"):
                return True
            if normalized in ("false", "0", "no"):
                return False
        raise ValueError("invalid boolean")
    if target is int:
        if isinstance(raw, bool):
            raise ValueError("invalid integer")
        if isinstance(raw, int):
            return raw
        if not isinstance(raw, str) or not _INTEGER_PATTERN.fullmatch(raw.strip()):
            raise ValueError("invalid integer")
        return int(raw)
    if target is float:
        result = float(raw)
        if not math.isfinite(result):
            raise ValueError("invalid float")
        return result
    return raw


def parse_strict_int(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ValidationError(f"{field} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValidationError(f"{field} must be at most {maximum}")
    return value


def parse_duration_seconds(
    value: object,
    field: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    return parse_strict_int(value, field, minimum=minimum, maximum=maximum)


def parse_timezone(value: object, field: str = "timezone") -> ZoneInfo:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValidationError(f"{field} must be a valid IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"{field} must be a valid IANA timezone") from exc


def _reject_json_constant(value: str) -> None:
    raise ValidationError(f"invalid JSON number {value}")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_json_shape(value: Any, *, depth: int, counter: list[int], max_depth: int, max_items: int) -> None:
    if depth > max_depth:
        raise ValidationError(f"JSON nesting must not exceed {max_depth} levels")
    if isinstance(value, dict):
        counter[0] += len(value)
        for child in value.values():
            _validate_json_shape(child, depth=depth + 1, counter=counter, max_depth=max_depth, max_items=max_items)
    elif isinstance(value, list):
        counter[0] += len(value)
        for child in value:
            _validate_json_shape(child, depth=depth + 1, counter=counter, max_depth=max_depth, max_items=max_items)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("JSON numbers must be finite")
    if counter[0] > max_items:
        raise ValidationError(f"JSON must not contain more than {max_items} items")


def parse_json_object(
    value: object,
    field: str,
    *,
    max_bytes: int = MAX_JSON_BYTES,
    max_depth: int = MAX_JSON_DEPTH,
    max_items: int = MAX_JSON_ITEMS,
) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8")) > max_bytes:
            raise ValidationError(f"{field} must not exceed {max_bytes} bytes")
        try:
            parsed = json.loads(
                value,
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except ValidationError:
            raise
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValidationError(f"{field} must be valid JSON") from exc
    elif isinstance(value, Mapping):
        parsed = dict(value)
        try:
            encoded = json.dumps(parsed, allow_nan=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{field} must contain JSON-compatible values") from exc
        if len(encoded) > max_bytes:
            raise ValidationError(f"{field} must not exceed {max_bytes} bytes")
    else:
        raise ValidationError(f"{field} must be a JSON object")

    if not isinstance(parsed, dict):
        raise ValidationError(f"{field} must be a JSON object")
    _validate_json_shape(parsed, depth=1, counter=[0], max_depth=max_depth, max_items=max_items)
    return parsed


def validate_settings_patch(payload: object) -> dict[str, SettingValue]:
    if not isinstance(payload, dict):
        raise ValidationError("settings payload must be a JSON object")
    normalized: dict[str, SettingValue] = {}
    for key, value in payload.items():
        spec = SETTING_SPECS.get(key)
        if spec is None:
            raise ValidationError(f"unknown setting: {key}")
        normalized[key] = spec.parse(value, key)
    return normalized


def settings_to_dict(settings_query: list[ContainerSettingsModel]) -> dict[str, str]:
    return {setting.key: setting.value for setting in settings_query}


def is_team_mode() -> bool | None:
    mode = get_config("user_mode")
    return mode == TEAMS_MODE if mode in (TEAMS_MODE, USERS_MODE) else None


def resolve_xid(user) -> int | None:
    if not is_team_mode():
        return user.id

    if not user.team:
        return None

    return user.team.id


def owner_filter(xid: int, is_team: bool) -> dict[str, int]:
    return {"team_id" if is_team else "user_id": xid}


ErrorKind = Literal["user", "transient", "permanent"]

# mirror of the fallback substring lists in src/assets/view.js, change both together
_PERMANENT_ERROR_PATTERNS: tuple[str, ...] = ("image not found", "challenge not found")
_USER_ERROR_PATTERNS: tuple[str, ...] = (
    "you can only spawn",
    "rate limit",
    "too many",
    "not a member of a team",
    "invalid",
    "no container found",
)


def classify_error_kind(message: str) -> ErrorKind:
    lower = message.lower()
    if any(p in lower for p in _PERMANENT_ERROR_PATTERNS):
        return "permanent"
    if any(p in lower for p in _USER_ERROR_PATTERNS):
        return "user"
    return "transient"


def error_body(message: str, kind: ErrorKind | None = None) -> dict[str, Any]:
    return {"error": message, "error_kind": kind or classify_error_kind(message)}


# equality only, a formatted template can never equal its constant so parameterized messages must not be listed here
_USER_SAFE = frozenset(
    {
        NO_RENEWALS,
        CONTAINER_NOT_FOUND,
        CONTAINER_NOT_FOUND_RESET,
        CHALLENGE_NOT_FOUND,
        REQUEST_IN_PROGRESS,
        IMAGE_NOT_FOUND,
        MEMORY_LIMIT_INVALID,
        CPU_LIMIT_INVALID,
    }
)


def sanitize_container_error(err: ContainerException | Exception) -> str:
    msg = str(err)
    if msg in _USER_SAFE:
        return msg
    logger.error(f"container error (sanitized): {msg}")
    return SERVER_ERROR


def handle_container_errors(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ContainerUnavailableException as err:
            return error_body(sanitize_container_error(err)), 503
        except ContainerException as err:
            return error_body(sanitize_container_error(err)), 500

    return wrapper


RatePolicyValue = int | str | Callable[[], int]

_RATE_LIMIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


@functools.lru_cache(maxsize=4)
def _rate_limit_redis_client(url: str):
    import redis

    return redis.from_url(url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2)


def _increment_rate_limit(key: str, interval: int) -> int:
    from flask import current_app

    redis_url = current_app.config.get("CACHE_REDIS_URL") or current_app.config.get("REDIS_URL")
    if isinstance(redis_url, str) and redis_url:
        client = _rate_limit_redis_client(redis_url)
        return int(client.eval(_RATE_LIMIT_LUA, 1, f"challenge-containers:{key}", interval))

    # no portable atomic increment outside redis, this fallback can undercount under concurrent requests
    from CTFd.cache import cache

    try:
        if cache.add(key, 1, timeout=interval):
            return 1
    except (AttributeError, NotImplementedError):
        pass
    current_count = int(cache.get(key) or 0) + 1
    cache.set(key, current_count, timeout=interval)
    return current_count


def _resolve_rate_policy(value: RatePolicyValue, field: str) -> int:
    resolved: object
    if callable(value):
        resolved = value()
    elif isinstance(value, str):
        default = DEFAULTS.get(value)
        if not isinstance(default, int):
            raise TypeError(f"invalid rate-limit setting key: {value}")
        resolved = get_setting(value, default)
    else:
        resolved = value

    if isinstance(resolved, bool) or not isinstance(resolved, int) or resolved < 1:
        logger.warning("invalid effective %s rate-limit policy; using 1", field)
        return 1
    return resolved


def ratelimit_per_user(
    method: str = "POST",
    limit: RatePolicyValue = 50,
    interval: RatePolicyValue = 300,
    key_prefix: str = "rl_user",
):
    # the ctfd ratelimit decorator keys on ip which throttles every user behind one egress ip
    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            from CTFd.utils.user import get_current_user, get_ip

            if request.method != method:
                return f(*args, **kwargs)

            user = get_current_user()
            if user is not None:
                bucket = f"u{user.id}"
            else:
                bucket = f"ip{get_ip()}"

            effective_limit = _resolve_rate_policy(limit, "request count")
            effective_interval = _resolve_rate_policy(interval, "interval")
            key = f"{key_prefix}:{bucket}:{request.endpoint}:{effective_limit}:{effective_interval}"

            current_count = _increment_rate_limit(key, effective_interval)

            if current_count > effective_limit:
                resp = jsonify(
                    {
                        "code": 429,
                        "message": RATE_LIMITED.format(limit=effective_limit, interval=effective_interval),
                        "error_kind": "user",
                    }
                )
                resp.status_code = 429
                resp.headers["Retry-After"] = str(effective_interval)
                return resp

            return f(*args, **kwargs)

        return wrapper

    return decorator
