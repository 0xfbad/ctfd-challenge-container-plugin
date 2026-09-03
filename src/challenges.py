from __future__ import annotations

import logging
import os
import secrets
import threading
import time

from flask import Request, abort
from flask import request as flask_request
from sqlalchemy.exc import IntegrityError

from CTFd.exceptions.challenges import (
    ChallengeCreateException,
    ChallengeUpdateException,
)
from CTFd.models import Teams, Users, db
from CTFd.plugins.challenges import BaseChallenge, calculate_value
from CTFd.plugins.challenges.decay import DECAY_FUNCTIONS
from CTFd.utils.user import get_current_user, get_ip

from .challenge_config import normalize_challenge_fields, normalize_services
from .coordination import InstanceCoordinator
from .event_logger import event_logger, flag_share_message, flag_share_metadata
from .flag_type import flag_share_identity_fields
from .freshness import compute_token, extract_token, render_flag
from .messages import FLAG_NOT_YOURS, TEAM_REQUIRED_FLAG, USER_NOT_FOUND
from .models import (
    ContainerChallengeModel,
    ContainerFlagShareModel,
    ContainerInfoModel,
    ContainerInstanceModel,
    DockerContextModel,
)
from .utils import _TOKEN_LENGTH_KEY, ValidationError, get_setting, is_team_mode, owner_filter, resolve_xid
from .volume_policy import MountConfigError, VolumePolicyError

logger = logging.getLogger(__name__)

_token_map_lock = threading.Lock()
_token_map_cache: dict[tuple[str, int, bool, int], tuple[int, dict[str, tuple[int, str]]]] = {}


def _get_token_length() -> int:
    return int(get_setting(_TOKEN_LENGTH_KEY, 6) or 6)


def _find_token_owner(
    secret: str, challenge_id: int, submitted_token: str, exclude_xid: int, team_mode: bool
) -> tuple[int, str] | None:
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

    entities = entity_class.query.all()
    token_map: dict[str, tuple[int, str]] = {}
    for entity in entities:
        token = compute_token(secret, challenge_id, entity.id, length=token_length)
        token_map[token] = (entity.id, getattr(entity, "name", f"id={entity.id}"))

    with _token_map_lock:
        # count the snapshot not the live table, a racing signup then leaves a mismatch that invalidates the cache
        _token_map_cache[cache_key] = (len(entities), token_map)

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

    container = ContainerInfoModel.query.filter_by(
        challenge_id=challenge_id, is_entry=True, **owner_filter(xid, team_mode)
    ).first()

    if not container:
        return None

    now = int(time.time())
    solve_time = now - container.timestamp if container.timestamp else None

    target_expires = now + expiry_seconds
    update = InstanceCoordinator.shorten_after_solve(container.instance_id, target_expires, solved_at=now)
    if update is None:
        return None

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

    @classmethod
    def _handle_ssh_password(cls, data: dict[str, str | None], existing_password: str | None = None) -> None:
        mode = data.pop("ssh_password_mode", None)
        if mode == "auto":
            data["ssh_password"] = existing_password or secrets.token_urlsafe(8)
        elif mode == "none":
            data["ssh_password"] = None

    @classmethod
    def create(cls, request: Request) -> ContainerChallengeModel:
        raw = request.form or request.get_json(silent=True) or {}
        data = dict(raw)

        cls._handle_ssh_password(data)
        try:
            data = normalize_challenge_fields(data)
        except (ValidationError, MountConfigError, VolumePolicyError) as exc:
            raise ChallengeCreateException(str(exc)) from exc

        context_name = data.get("docker_context")
        if context_name and not DockerContextModel.query.filter_by(context_name=context_name).first():
            raise ChallengeCreateException(f"Docker context '{context_name}' does not exist")

        for attr in ("initial", "minimum", "decay"):
            if attr in data:
                try:
                    data[attr] = float(str(data[attr]))
                except (ValueError, TypeError):
                    raise ChallengeCreateException(f"Invalid input for '{attr}'")

        challenge = cls.challenge_model(**data)

        if challenge.function in DECAY_FUNCTIONS:
            if data.get("value") and not data.get("initial"):
                challenge.initial = data["value"]

            for attr in ("initial", "minimum", "decay"):
                if getattr(challenge, attr) is None:
                    db.session.rollback()
                    raise ChallengeCreateException(f"Missing '{attr}' but function is {challenge.function}")

        db.session.add(challenge)
        db.session.commit()

        if challenge.function in DECAY_FUNCTIONS:
            calculate_value(challenge)

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
        "function",
        "initial",
        "minimum",
        "decay",
    }

    _RUNTIME_FIELDS = {
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
        raw = request.form or request.get_json(silent=True) or {}
        data = dict(raw)

        cls._handle_ssh_password(data, existing_password=challenge.ssh_password)

        existing_service_names: set[str] = set()
        if challenge.services_json:
            _, existing_services = normalize_services(challenge.services_json)
            existing_service_names = set(existing_services)
        try:
            data = normalize_challenge_fields(data, existing_service_names=existing_service_names)
        except (ValidationError, MountConfigError, VolumePolicyError) as exc:
            db.session.rollback()
            raise ChallengeUpdateException(str(exc)) from exc

        changed_runtime_fields = {
            field for field in cls._RUNTIME_FIELDS if field in data and data[field] != getattr(challenge, field)
        }
        if changed_runtime_fields and ContainerInstanceModel.query.filter_by(challenge_id=challenge.id).count():
            fields = ", ".join(sorted(changed_runtime_fields))
            raise ChallengeUpdateException(f"stop all active instances before changing runtime configuration: {fields}")

        context_name = data.get("docker_context")
        if context_name and not DockerContextModel.query.filter_by(context_name=context_name).first():
            raise ChallengeUpdateException(f"Docker context '{context_name}' does not exist")

        for attr, value in data.items():
            if attr not in cls._UPDATABLE_FIELDS:
                continue
            if attr in ("initial", "minimum", "decay"):
                try:
                    value = float(str(value))
                except (ValueError, TypeError):
                    db.session.rollback()
                    raise ChallengeUpdateException(f"Invalid input for '{attr}'")
            setattr(challenge, attr, value)

        for attr in ("initial", "minimum", "decay"):
            if challenge.function in DECAY_FUNCTIONS and getattr(challenge, attr) is None:
                db.session.rollback()
                raise ChallengeUpdateException(f"Missing '{attr}' but function is {challenge.function}")

        db.session.commit()

        if challenge.function in DECAY_FUNCTIONS:
            return calculate_value(challenge)

        return challenge

    @classmethod
    def delete(cls, challenge: ContainerChallengeModel) -> None:
        if ContainerInstanceModel.query.filter_by(challenge_id=challenge.id).count():
            abort(409, description="stop and clean all active instances before deleting this challenge")
        super().delete(challenge)

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
            return False, USER_NOT_FOUND

        team_mode = bool(is_team_mode())
        xid = resolve_xid(user)
        if xid is None:
            return False, TEAM_REQUIRED_FLAG

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
                return True, "correct"

            submitted_token = extract_token(template, submission)
            if submitted_token is None:
                continue

            owner = _find_token_owner(secret, challenge.id, submitted_token, xid, team_mode)
            if owner is None:
                continue

            source_id, identifier = owner
            in_team = bool(team_mode and user.team)
            meta = flag_share_metadata(
                challenge.id,
                challenge.name,
                source_id,
                identifier,
                "teams" if team_mode else "users",
                team_id=user.team.id if in_team else None,
                team_name=user.team.name if in_team else None,
            )

            event_logger.log_event(
                "flag_sharing",
                flag_share_message(user.name, identifier, challenge.name),
                user_id=user.id,
                username=user.name,
                level="warning",
                metadata=meta,
            )

            share_row = ContainerFlagShareModel(
                challenge_id=challenge.id,
                submitter_user_id=user.id,
                submitter_team_id=user.team.id if (team_mode and user.team) else None,
                owner_user_id=None if team_mode else source_id,
                owner_team_id=source_id if team_mode else None,
                **flag_share_identity_fields(
                    user=user,
                    challenge_id=challenge.id,
                    submitted_token=submitted_token,
                    secret=secret,
                ),
                ip=get_ip(flask_request),
                timestamp=time.time(),
            )

            try:
                db.session.add(share_row)
                db.session.commit()
            except IntegrityError:
                # duplicate submit of the same token, the first row stays the record
                db.session.rollback()

            return False, FLAG_NOT_YOURS

        return False, "incorrect"

    @classmethod
    def solve(cls, user, team, challenge: ContainerChallengeModel, request: Request) -> None:
        super().solve(user=user, team=team, challenge=challenge, request=request)
        team_mode = bool(is_team_mode())
        xid = team.id if team_mode and team is not None else user.id
        try:
            solve_time = _shorten_after_solve(challenge.id, xid, team_mode)
        except Exception as error:
            solve_time = None
            logger.exception("solve persisted but container expiry shortening failed")
            event_logger.log_event(
                "solve_reconcile_pending",
                f"solve persisted but timer shortening failed for '{challenge.name}'",
                level="error",
                user_id=user.id,
                username=user.name,
                metadata={
                    "challenge_id": challenge.id,
                    "challenge_name": challenge.name,
                    "reason": str(error),
                },
            )
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
            "initial": challenge.initial if challenge.function != "static" else None,
            "decay": challenge.decay if challenge.function != "static" else None,
            "minimum": challenge.minimum if challenge.function != "static" else None,
            "function": challenge.function,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
        }
        return data
