from CTFd.utils import get_config


DEFAULTS = {
    "max_containers_per_user": 4,
    "rate_limit_requests": 500,
    "rate_limit_interval": 10,
    "expiration_check_interval": 5,
    "thread_pool_size": 4,
    "max_concurrent_creates": 2,
    "freshness_secret": "",
    "post_solve_expiry_seconds": 90,
}

USERS_MODE = "users"
TEAMS_MODE = "teams"


def get_setting(key, default=None):
    from .models import ContainerSettingsModel

    if default is None:
        default = DEFAULTS.get(key)

    try:
        from flask import current_app

        if not current_app:
            return default

        row = ContainerSettingsModel.query.filter_by(key=key).first()
        if row is None:
            return default

        return _coerce(row.value, default)
    except Exception:
        return default


def set_setting(key, value):
    from .models import ContainerSettingsModel
    from CTFd.models import db

    row = ContainerSettingsModel.query.filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        row = ContainerSettingsModel(key=key, value=str(value))
        db.session.add(row)
    db.session.commit()


def _coerce(raw, default):
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


def settings_to_dict(settings_query):
    return {setting.key: setting.value for setting in settings_query}


def is_team_mode():
    mode = get_config("user_mode")
    return mode == TEAMS_MODE if mode in (TEAMS_MODE, USERS_MODE) else None
