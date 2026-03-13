from flask import Flask
from CTFd.plugins import register_plugin_assets_directory, register_admin_plugin_menu_bar
from CTFd.plugins.challenges import CHALLENGE_CLASSES
import os

from .challenges import ContainerChallenge
from .models import ContainerSettingsModel
from .utils import settings_to_dict, DEFAULTS
from .container_manager import ContainerManager
from .models import ContainerInfoModel
from .views import containers_bp


def _seed_defaults(app):
    from CTFd.models import db

    existing = {s.key for s in ContainerSettingsModel.query.all()}
    for key, value in DEFAULTS.items():
        if key not in existing:
            db.session.add(ContainerSettingsModel(key=key, value=str(value)))
    db.session.commit()


def _reconcile_containers(app, container_manager):
    from CTFd.models import db

    containers = ContainerInfoModel.query.all()
    removed = 0

    for container in containers:
        try:
            if not container_manager.is_container_running(container.container_id, container.docker_context):
                db.session.delete(container)
                removed += 1
        except Exception:
            db.session.delete(container)
            removed += 1

    if removed:
        db.session.commit()
        print(f"reconciled {removed} stale container records on startup")


def load(app: Flask):
    app.db.create_all()
    CHALLENGE_CLASSES["container"] = ContainerChallenge

    plugin_name = os.path.basename(os.path.dirname(__file__))
    assets_path = f"plugins/{plugin_name}/assets"
    register_plugin_assets_directory(app, base_path=assets_path)

    with app.app_context():
        _seed_defaults(app)

    container_settings = settings_to_dict(ContainerSettingsModel.query.all())
    container_manager = ContainerManager(container_settings, app)

    with app.app_context():
        try:
            _reconcile_containers(app, container_manager)
        except Exception as e:
            print(f"container reconciliation failed: {e}")

    app.container_manager = container_manager

    app.register_blueprint(containers_bp)

    register_admin_plugin_menu_bar(title="Containers", route="/containers/dashboard")
