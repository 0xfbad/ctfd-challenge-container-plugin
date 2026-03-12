import os
import json
from CTFd.utils import get_config


def get_settings_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


settings = json.load(open(get_settings_path()))

USERS_MODE = settings["modes"]["USERS_MODE"]
TEAMS_MODE = settings["modes"]["TEAMS_MODE"]


def settings_to_dict(settings_query):
    return {setting.key: setting.value for setting in settings_query}


def is_team_mode():
    mode = get_config("user_mode")
    return mode == TEAMS_MODE if mode in (TEAMS_MODE, USERS_MODE) else None
