import os
import time
import threading

from CTFd.plugins.challenges import BaseChallenge
from CTFd.models import db, Users, Teams
from CTFd.utils.user import get_current_user

from .models import ContainerChallengeModel, ContainerInfoModel, ContainerHistoryModel
from .utils import get_setting, is_team_mode
from .freshness import compute_token, render_flag, extract_token
from .event_logger import event_logger

_token_map_lock = threading.Lock()
_token_map_cache: dict[tuple, tuple] = {}


def _find_token_owner(secret, challenge_id, submitted_token, exclude_xid, team_mode):
    """cached O(1) lookup of which entity owns a freshness token"""
    entity_class = Teams if team_mode else Users
    cache_key = (secret, challenge_id, team_mode)
    current_count = entity_class.query.count()

    with _token_map_lock:
        cached = _token_map_cache.get(cache_key)
        if cached and cached[0] == current_count:
            match = cached[1].get(submitted_token)
            if match and match[0] != exclude_xid:
                return match
            return None

    token_map = {}
    for entity in entity_class.query.all():
        token = compute_token(secret, challenge_id, entity.id)
        token_map[token] = (entity.id, getattr(entity, "name", f"id={entity.id}"))

    with _token_map_lock:
        _token_map_cache[cache_key] = (current_count, token_map)

    match = token_map.get(submitted_token)
    if match and match[0] != exclude_xid:
        return match
    return None


_plugin_dir = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_assets = f"/plugins/{_plugin_dir}/src/assets"


def _shorten_after_solve(challenge_id, xid, team_mode):
    expiry_seconds = get_setting("post_solve_expiry_seconds")
    if not expiry_seconds:
        return

    filter_args = {"challenge_id": challenge_id}
    filter_args["team_id" if team_mode else "user_id"] = xid
    container = ContainerInfoModel.query.filter_by(**filter_args).first()

    if not container or container.expires == 0:
        return

    container.expires = int(time.time()) + expiry_seconds
    db.session.commit()

    history = ContainerHistoryModel.query.filter_by(container_id=container.container_id).first()
    if history:
        history.reason = "solved"
        db.session.commit()


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
    def sanitize_value(value):
        return value if value and value != "" else None

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json()

        for attr in ("docker_context", "max_memory_mb", "max_cpu", "expiration_minutes"):
            if attr in data:
                data[attr] = cls.sanitize_value(data[attr])

        challenge = cls.challenge_model(**data)
        db.session.add(challenge)
        db.session.commit()

        return challenge

    @classmethod
    def update(cls, challenge, request):
        data = request.form or request.get_json()

        for attr, value in data.items():
            if attr in ("docker_context", "max_memory_mb", "max_cpu", "expiration_minutes"):
                value = cls.sanitize_value(value)
            setattr(challenge, attr, value)

        db.session.commit()
        return challenge

    @classmethod
    def attempt(cls, challenge, request):
        data = request.form or request.get_json()
        submission = data["submission"].strip()

        secret = get_setting("freshness_secret")
        if not secret:
            return super().attempt(challenge, request)

        from CTFd.models import Flags

        freshness_flags = Flags.query.filter_by(challenge_id=challenge.id, type="freshness").all()

        if not freshness_flags:
            return super().attempt(challenge, request)

        user = get_current_user()
        if not user:
            return False, "user not found"

        team_mode = is_team_mode()
        if team_mode:
            if not user.team:
                return False, "you must be on a team to submit flags"
            xid = user.team.id
        else:
            xid = user.id

        for flag in freshness_flags:
            template = flag.content
            token = compute_token(secret, challenge.id, xid)
            expected = render_flag(template, token)

            case_insensitive = flag.data and flag.data.lower() == "case_insensitive"

            if case_insensitive:
                match = expected.lower() == submission.lower()
            else:
                match = expected == submission

            if match:
                _shorten_after_solve(challenge.id, xid, team_mode)
                return True, "correct"

            submitted_token = extract_token(template, submission)
            if submitted_token is None:
                continue

            owner = _find_token_owner(secret, challenge.id, submitted_token, xid, team_mode)
            if owner:
                source_id, identifier = owner
                event_logger.log_event(
                    "flag_sharing",
                    f"user '{user.name}' submitted a flag belonging to '{identifier}' on challenge '{challenge.name}'",
                    user_id=user.id,
                    username=user.name,
                    level="warning",
                    metadata={
                        "challenge_id": challenge.id,
                        "challenge_name": challenge.name,
                        "source_entity": identifier,
                        "source_id": source_id,
                        "source_type": "teams" if team_mode else "users",
                    },
                )
                return False, "this flag belongs to another participant. this attempt has been logged."

        return False, "incorrect"

    @classmethod
    def read(cls, challenge):
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
            "expiration_minutes": challenge.expiration_minutes,
            "max_memory_mb": challenge.max_memory_mb,
            "max_cpu": challenge.max_cpu,
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
