from __future__ import annotations

import hmac as _hmac

from CTFd.models import Flags
from CTFd.plugins.flags import BaseFlag, FLAG_CLASSES
from CTFd.utils.user import get_current_user

from .freshness import compute_token, render_flag
from .utils import get_setting, _TOKEN_LENGTH_KEY, is_team_mode


class FreshnessFlag(BaseFlag):
    name = "freshness"
    templates = {
        "create": "/plugins/flags/static/create.html",
        "update": "/plugins/flags/static/edit.html",
    }

    @staticmethod
    def compare(chal_key_obj: Flags, provided: str) -> bool:
        secret_raw = get_setting("freshness_secret")
        if not secret_raw:
            return False
        secret = str(secret_raw)

        user = get_current_user()
        if not user:
            return False

        if is_team_mode():
            if not user.team:
                return False
            xid = user.team.id
        else:
            xid = user.id

        template = chal_key_obj.content
        challenge_id = chal_key_obj.challenge_id

        token_length = int(get_setting(_TOKEN_LENGTH_KEY, 6) or 6)
        token = compute_token(secret, challenge_id, xid, length=token_length)
        expected = render_flag(template, token)

        if chal_key_obj.data and chal_key_obj.data.lower() == "case_insensitive":
            return _hmac.compare_digest(expected.lower(), provided.lower())

        return _hmac.compare_digest(expected, provided)


def register() -> None:
    FLAG_CLASSES["freshness"] = FreshnessFlag
