import json

from CTFd.plugins.challenges import BaseChallenge
from CTFd.models import db

from .models import ContainerChallengeModel
from .utils import get_settings_path

with open(get_settings_path(), 'r') as f:
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
        return value if value and value != '' else None

    @classmethod
    def create(cls, request):
        data = request.form or request.get_json()

        for attr in ("max_memory_mb", "max_cpu", "expiration_minutes"):
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
            if attr in ("max_memory_mb", "max_cpu", "expiration_minutes"):
                value = cls.sanitize_value(value)
            setattr(challenge, attr, value)

        db.session.commit()
        return challenge

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
