from __future__ import annotations

import os
import secrets
import time
import threading

from flask import Request
from CTFd.plugins.challenges import BaseChallenge
from CTFd.models import db, Users, Teams, Solves
from CTFd.utils.user import get_current_user

from .models import ContainerChallengeModel, ContainerInfoModel, ContainerHistoryModel
from .utils import get_setting, _TOKEN_LENGTH_KEY, is_team_mode
from .freshness import compute_token, render_flag, extract_token
from .event_logger import event_logger

_token_map_lock = threading.Lock()
# cache keyed by (secret, challenge_id, team_mode, token_length) -> (entity_count, {token -> (entity_id, entity_name)})
_token_map_cache: dict[tuple[str, int, bool, int], tuple[int, dict[str, tuple[int, str]]]] = {}


def _get_token_length() -> int:
    return int(get_setting(_TOKEN_LENGTH_KEY, 6) or 6)


def _find_token_owner(
    secret: str, challenge_id: int, submitted_token: str, exclude_xid: int, team_mode: bool
) -> tuple[int, str] | None:
    """cached lookup of which entity owns a freshness token"""
    token_length = _get_token_length()
    entity_class = Teams if team_mode else Users
    cache_key = (secret, challenge_id, team_mode, token_length)
    current_count = entity_class.query.count()

    with _token_map_lock:
        cached = _token_map_cache.get(cache_key)
        if cached and cached[0] == current_count:
            match = cached[1].get(submitted_token)
            if match and match[0] != exclude_xid:
                return match
            return None

    token_map: dict[str, tuple[int, str]] = {}
    for entity in entity_class.query.all():
        token = compute_token(secret, challenge_id, entity.id, length=token_length)
        token_map[token] = (entity.id, getattr(entity, "name", f"id={entity.id}"))

    with _token_map_lock:
        _token_map_cache[cache_key] = (current_count, token_map)

    match = token_map.get(submitted_token)
    if match and match[0] != exclude_xid:
        return match
    return None


_plugin_dir = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_assets = f"/plugins/{_plugin_dir}/src/assets"


def _shorten_after_solve(challenge_id: int, xid: int, team_mode: bool) -> int | None:
    expiry_raw = get_setting("post_solve_expiry_seconds")
    if not expiry_raw:
        return None
    expiry_seconds = int(expiry_raw)

    filter_args = {"challenge_id": challenge_id}
    filter_args["team_id" if team_mode else "user_id"] = xid
    container = ContainerInfoModel.query.filter_by(**filter_args).first()

    if not container:
        return None

    now = int(time.time())
    solve_time = now - container.timestamp if container.timestamp else None

    container.expires = now + expiry_seconds
    db.session.commit()

    history = ContainerHistoryModel.query.filter_by(container_id=container.container_id).first()
    if history:
        history.reason = "solved"
        db.session.commit()

    return solve_time


class ContainerChallenge(BaseChallenge):
    id = "container"
    name = "container"
    templates = {
        "create": f"{_assets}/create.html",
        "update": f"{_assets}/update.html",
        "view": f"{_assets}/view.html",
    }
    scripts = {
        "create": f"{_assets}/create.js",
        "update": f"{_assets}/update.js",
        "view": f"{_assets}/view.js",
    }
    route = f"{_assets}/"

    challenge_model = ContainerChallengeModel

    @staticmethod
    def sanitize_value(value: str | None) -> str | None:
        return value if value and value != "" else None

    @classmethod
    def _handle_ssh_password(cls, data: dict[str, str | None], existing_password: str | None = None) -> None:
        mode = data.pop("ssh_password_mode", None)
        if mode == "auto":
            data["ssh_password"] = existing_password or secrets.token_urlsafe(8)
        elif mode == "none":
            data["ssh_password"] = None

    @classmethod
    def create(cls, request: Request) -> ContainerChallengeModel:
        data = request.form or request.get_json()

        cls._handle_ssh_password(data)

        for attr in ("docker_context", "max_memory_mb", "max_cpu", "expiration_seconds", "max_renewals"):
            if attr in data:
                data[attr] = cls.sanitize_value(data[attr])

        challenge = cls.challenge_model(**data)
        db.session.add(challenge)
        db.session.commit()

        return challenge

    _UPDATABLE_FIELDS = {
        "name",
        "description",
        "category",
        "value",
        "state",
        "max_attempts",
        "connection_info",
        "type",
        "image",
        "port",
        "command",
        "volumes",
        "ctype",
        "ssh_username",
        "ssh_password",
        "docker_context",
        "max_memory_mb",
        "max_cpu",
        "expiration_seconds",
        "max_renewals",
        "cap_add",
        "services_json",
        "network_json",
    }

    @classmethod
    def update(cls, challenge: ContainerChallengeModel, request: Request) -> ContainerChallengeModel:
        data = request.form or request.get_json()

        cls._handle_ssh_password(data, existing_password=challenge.ssh_password)

        for attr, value in data.items():
            if attr not in cls._UPDATABLE_FIELDS:
                continue
            if attr in ("docker_context", "max_memory_mb", "max_cpu", "expiration_seconds", "max_renewals"):
                value = cls.sanitize_value(value)
            setattr(challenge, attr, value)

        db.session.commit()
        return challenge

    @classmethod
    def attempt(cls, challenge: ContainerChallengeModel, request: Request) -> tuple[bool, str]:
        data = request.form or request.get_json()
        submission = data["submission"].strip()

        secret_raw = get_setting("freshness_secret")
        if not secret_raw:
            return super().attempt(challenge, request)
        secret = str(secret_raw)

        from CTFd.models import Flags

        freshness_flags = Flags.query.filter_by(challenge_id=challenge.id, type="freshness").all()

        if not freshness_flags:
            return super().attempt(challenge, request)

        user = get_current_user()
        if not user:
            return False, "user not found"

        team_mode = bool(is_team_mode())
        if team_mode:
            if not user.team:
                return False, "you must be on a team to submit flags"
            xid = user.team.id
        else:
            xid = user.id

        for flag in freshness_flags:
            template = flag.content
            token_length = _get_token_length()
            token = compute_token(secret, challenge.id, xid, length=token_length)
            expected = render_flag(template, token)

            case_insensitive = flag.data and flag.data.lower() == "case_insensitive"

            if case_insensitive:
                match = expected.lower() == submission.lower()
            else:
                match = expected == submission

            if match:
                already_solved = Solves.query.filter_by(account_id=xid, challenge_id=challenge.id).first()
                if not already_solved:
                    solve_time = _shorten_after_solve(challenge.id, xid, team_mode)
                    event_logger.log_event(
                        "solved",
                        f"user '{user.name}' solved '{challenge.name}', timer shortened",
                        user_id=user.id,
                        username=user.name,
                        metadata={
                            "challenge_id": challenge.id,
                            "challenge_name": challenge.name,
                            "solve_time": solve_time,
                        },
                    )
                return True, "correct"

            submitted_token = extract_token(template, submission)
            if submitted_token is None:
                continue

            owner = _find_token_owner(secret, challenge.id, submitted_token, xid, team_mode)
            if owner:
                source_id, identifier = owner
                meta = {
                    "challenge_id": challenge.id,
                    "challenge_name": challenge.name,
                    "source_entity": identifier,
                    "source_id": source_id,
                    "source_type": "teams" if team_mode else "users",
                }
                if team_mode and user.team:
                    meta["team_id"] = user.team.id
                    meta["team_name"] = user.team.name

                event_logger.log_event(
                    "flag_sharing",
                    f"user '{user.name}' submitted a flag belonging to '{identifier}' on challenge '{challenge.name}'",
                    user_id=user.id,
                    username=user.name,
                    level="warning",
                    metadata=meta,
                )
                return False, "this flag belongs to another participant. this attempt has been logged."

        return False, "incorrect"

    @classmethod
    def read(cls, challenge: ContainerChallengeModel) -> dict[str, str | int | dict[str, str] | None]:
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "docker_context": challenge.docker_context,
            "image": challenge.image,
            "port": challenge.port,
            "command": challenge.command,
            "ctype": challenge.ctype,
            "ssh_username": challenge.ssh_username,
            "ssh_password": challenge.ssh_password,
            "expiration_seconds": challenge.expiration_seconds,
            "max_memory_mb": challenge.max_memory_mb,
            "max_cpu": challenge.max_cpu,
            "cap_add": challenge.cap_add,
            "services_json": challenge.services_json,
            "network_json": challenge.network_json,
            "description": challenge.description,
            "connection_info": challenge.connection_info,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data
