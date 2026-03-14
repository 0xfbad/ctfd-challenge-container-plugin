import socket
import logging
from flask import Flask
from CTFd.plugins import register_plugin_assets_directory, register_admin_plugin_menu_bar
from CTFd.plugins.challenges import CHALLENGE_CLASSES
import os

from .challenges import ContainerChallenge
from .models import ContainerSettingsModel
from .utils import settings_to_dict, DEFAULTS
from .container_manager import ContainerManager, LOCAL_SOCKET_PATH, LOCAL_CONTEXT_NAME
from .models import ContainerInfoModel, DockerContextModel
from .views import containers_bp
from .flag_type import register as register_freshness_flag
from .freshness import generate_secret

logger = logging.getLogger(__name__)


def _seed_defaults(app):
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


def _seed_local_context(app):
    from CTFd.models import db

    if DockerContextModel.query.count() > 0:
        return

    import docker as docker_lib

    try:
        client = docker_lib.DockerClient(base_url=f"unix://{LOCAL_SOCKET_PATH}")
        client.ping()
        client.close()
    except Exception:
        return

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


def _reconcile_containers(app, container_manager):
    from CTFd.models import db

    containers = ContainerInfoModel.query.all()
    removed = 0
    kept = 0

    for container in containers:
        try:
            if container_manager.is_container_running(container.container_id, container.docker_context):
                container_manager.reserve_slot(container.docker_context)
                kept += 1
            else:
                db.session.delete(container)
                removed += 1
        except Exception:
            db.session.delete(container)
            removed += 1

    if removed:
        db.session.commit()

    if removed or kept:
        logger.info(f"reconciled containers on startup: {kept} recovered, {removed} stale records removed")


def load(app: Flask):
    app.db.create_all()
    CHALLENGE_CLASSES["container"] = ContainerChallenge
    register_freshness_flag()

    plugin_name = os.path.basename(os.path.dirname(__file__))
    assets_path = f"plugins/{plugin_name}/assets"
    register_plugin_assets_directory(app, base_path=assets_path)

    with app.app_context():
        _seed_defaults(app)
        _seed_local_context(app)

    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    container_manager = ContainerManager(container_settings, app)

    with app.app_context():
        try:
            _reconcile_containers(app, container_manager)
        except Exception as e:
            logger.error(f"container reconciliation failed: {e}")

    app.container_manager = container_manager

    app.register_blueprint(containers_bp)

    register_admin_plugin_menu_bar(title="Containers", route="/containers/dashboard")
