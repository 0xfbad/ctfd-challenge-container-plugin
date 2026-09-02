from __future__ import annotations

import logging
import os
import socket

from flask import Flask

from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES

from . import event_bus
from .challenges import ContainerChallenge
from .container_manager import ContainerManager
from .docker_host_manager import LOCAL_CONTEXT_NAME, LOCAL_SOCKET_PATH
from .event_logger import event_logger
from .flag_type import register as register_freshness_flag
from .freshness import generate_secret
from .models import ContainerSettingsModel, DockerContextModel
from .utils import DEFAULTS, settings_to_dict
from .views import containers_bp

logger = logging.getLogger(__name__)


def _seed_defaults(app: Flask) -> None:
    from CTFd.models import db

    existing = {s.key: s.value for s in ContainerSettingsModel.query.all()}
    for key, value in DEFAULTS.items():
        # an empty freshness secret means opt out, so never seed the schema default, generate one below instead
        if key != "freshness_secret" and key not in existing:
            db.session.add(ContainerSettingsModel(key=key, value=str(value)))

    if "freshness_secret" not in existing:
        db.session.add(ContainerSettingsModel(key="freshness_secret", value=generate_secret()))

    db.session.commit()


def _seed_local_context(app: Flask) -> None:
    from CTFd.models import db

    if DockerContextModel.query.count() > 0:
        return

    import docker as docker_lib

    client = None
    try:
        client = docker_lib.DockerClient(base_url=f"unix://{LOCAL_SOCKET_PATH}")
        client.ping()
    except Exception:
        return
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass

    db.session.add(
        DockerContextModel(
            context_name=LOCAL_CONTEXT_NAME,
            hostname=None,
            pub_hostname=socket.gethostname(),
            weight=1,
            state="active",
        )
    )
    db.session.commit()
    logger.info("seeded local docker context")


def load(app: Flask) -> None:
    app.db.create_all()
    # mysql invalidates open transaction metadata after ddl, so drop the scoped session before seeding
    app.db.session.remove()
    CHALLENGE_CLASSES["container"] = ContainerChallenge
    register_freshness_flag()

    plugin_dir = os.path.dirname(os.path.dirname(__file__))
    plugin_name = os.path.basename(plugin_dir)
    assets_path = f"plugins/{plugin_name}/src/assets"
    register_plugin_assets_directory(app, base_path=assets_path)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)

    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    container_manager = ContainerManager(container_settings, app)

    app.container_manager = container_manager

    event_bus.init(app, on_message=event_logger._deliver_local)

    app.register_blueprint(containers_bp)

    # an overridden template lets the admin config page include this without knowing the plugin folder name
    config_tpl = os.path.join(os.path.dirname(__file__), "templates", "container_config.html")
    with open(config_tpl) as f:
        app.overridden_templates["container_config.html"] = f.read()
