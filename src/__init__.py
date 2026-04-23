from __future__ import annotations

import socket
import time
import logging
import docker
import paramiko
from flask import Flask
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES
import os

from .challenges import ContainerChallenge
from .models import ContainerSettingsModel
from .utils import settings_to_dict, DEFAULTS
from .container_manager import ContainerManager
from .exceptions import ContainerException
from .docker_host_manager import LOCAL_SOCKET_PATH, LOCAL_CONTEXT_NAME
from .models import ContainerInfoModel, ContainerHistoryModel, DockerContextModel
from .views import containers_bp
from .flag_type import register as register_freshness_flag
from .freshness import generate_secret

logger = logging.getLogger(__name__)


def _seed_defaults(app: Flask) -> None:
    from CTFd.models import db

    existing = {s.key: s.value for s in ContainerSettingsModel.query.all()}
    for key, value in DEFAULTS.items():
        if key not in existing:
            db.session.add(ContainerSettingsModel(key=key, value=str(value)))

    if not existing.get("freshness_secret"):
        secret_row = ContainerSettingsModel.query.filter_by(key="freshness_secret").first()
        if secret_row:
            secret_row.value = generate_secret()
        else:
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
            enabled=True,
        )
    )
    db.session.commit()
    logger.info("seeded local docker context")


def _reconcile_containers(app: Flask, container_manager: ContainerManager) -> None:
    from CTFd.models import db

    containers = ContainerInfoModel.query.all()
    removed = 0
    kept = 0

    for container in containers:
        try:
            still_running = container_manager.is_container_running(container.container_id, container.docker_context)
        except (ContainerException, docker.errors.DockerException, paramiko.ssh_exception.SSHException):
            # transient connectivity issue, keep the row and retry on next reconcile cycle
            kept += 1
            continue
        except Exception:
            still_running = False

        if still_running:
            container_manager.reserve_slot(container.docker_context)
            kept += 1
        else:
            history = ContainerHistoryModel.query.filter_by(container_id=container.container_id).first()
            if history:
                history.stopped_at = time.time()
                history.reason = "reconciled"
            db.session.delete(container)
            removed += 1

    if removed:
        db.session.commit()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def _ensure_columns(app: Flask) -> None:
    from CTFd.models import db
    from sqlalchemy import inspect, text

    with app.app_context():
        insp = inspect(db.engine)
        for table, col, col_type, default in [
            ("container_challenges", "max_renewals", "INTEGER", "2"),
            ("container_challenges", "expiration_seconds", "INTEGER", "1800"),
            ("container_info", "renewals_used", "INTEGER", "0"),
        ]:
            cols = {c["name"] for c in insp.get_columns(table)}
            if col not in cols:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type} DEFAULT {default}"))
                logger.info(f"added {col} column to {table}")

        db.session.commit()


def load(app: Flask) -> None:
    app.db.create_all()
    _ensure_columns(app)
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

    with app.app_context():
        _reconcile_containers(app, container_manager)

    app.container_manager = container_manager

    app.register_blueprint(containers_bp)

    # DictLoader lets /admin/config {% include %} this without knowing the plugin folder name
    config_tpl = os.path.join(os.path.dirname(__file__), "templates", "container_config.html")
    with open(config_tpl) as f:
        app.overridden_templates["container_config.html"] = f.read()
