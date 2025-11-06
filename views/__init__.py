from flask import Blueprint
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
templates_dir = os.path.join(current_dir, '..', 'templates')
assets_dir = os.path.join(current_dir, '..', 'assets')

containers_bp = Blueprint(
    "containers",
    __name__,
    template_folder=templates_dir,
    static_folder=assets_dir,
    url_prefix="/containers",
)

from . import routes_user
from . import routes_admin
from . import helpers
