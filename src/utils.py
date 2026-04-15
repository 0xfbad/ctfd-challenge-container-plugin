from __future__ import annotations

from CTFd.utils import get_config

from .models import ContainerSettingsModel


DEFAULTS: dict[str, int | str] = {
    "max_containers_per_user": 4,
    "rate_limit_requests": 45,
    "rate_limit_interval": 60,
    "expiration_check_interval": 5,
    "thread_pool_size": 4,
    "max_concurrent_creates": 2,
    "freshness_secret": "",
    "post_solve_expiry_seconds": 90,
    "default_expiration_seconds": 1800,
    "default_max_renewals": 2,
}

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
