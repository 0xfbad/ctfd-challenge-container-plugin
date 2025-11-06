from flask import Flask
from CTFd.plugins import register_plugin_assets_directory, register_admin_plugin_menu_bar
from CTFd.plugins.challenges import CHALLENGE_CLASSES
import os

from .challenges import ContainerChallenge
from .models import ContainerSettingsModel
from .utils import settings_to_dict
from .container_manager import ContainerManager
from .views import containers_bp

def load(app: Flask):
	app.db.create_all()
	CHALLENGE_CLASSES["container"] = ContainerChallenge

	plugin_name = os.path.basename(os.path.dirname(__file__))
	assets_path = f"plugins/{plugin_name}/assets"
	register_plugin_assets_directory(app, base_path=assets_path)

	container_settings = settings_to_dict(ContainerSettingsModel.query.all())
	container_manager = ContainerManager(container_settings, app)

	app.container_manager = container_manager

	app.register_blueprint(containers_bp)

	register_admin_plugin_menu_bar(
		title="Containers",
		route="/containers/dashboard"
	)
