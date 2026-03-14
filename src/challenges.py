import json

from CTFd.plugins.challenges import BaseChallenge
from CTFd.models import db, Users, Teams
from CTFd.utils.user import get_current_user

from .models import ContainerChallengeModel
from .utils import get_settings_path, get_setting, is_team_mode
from .freshness import compute_token, render_flag, extract_token
from .event_logger import event_logger

with open(get_settings_path(), "r") as f:
    settings = json.load(f)


class ContainerChallenge(BaseChallenge):
    id = settings["plugin-info"]["id"]
    name = settings["plugin-info"]["name"]
    templates = settings["plugin-info"]["templates"]
    scripts = settings["plugin-info"]["scripts"]
    route = settings["plugin-info"]["route"]

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
                return True, "correct"

            submitted_token = extract_token(template, submission)
            if submitted_token is None:
                continue

            entities = Teams.query.all() if team_mode else Users.query.all()
            for entity in entities:
                other_xid = entity.id
                if other_xid == xid:
                    continue

                other_token = compute_token(secret, challenge.id, other_xid)
                if submitted_token == other_token:
                    identifier = getattr(entity, "name", f"id={other_xid}")
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
