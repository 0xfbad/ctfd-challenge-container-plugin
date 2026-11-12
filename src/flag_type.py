from __future__ import annotations

import hashlib
import hmac as _hmac
from typing import ClassVar

from CTFd.models import Flags
from CTFd.plugins.flags import FLAG_CLASSES, BaseFlag
from CTFd.utils.user import get_current_user

from .freshness import accepted_flags
from .utils import get_freshness_secret, resolve_xid

_FLAG_SHARE_DIGEST_DOMAIN = b"ctfd-challenge-containers:flag-share-token:v1\x00"


def submitter_user_xid(user: object) -> str:
    user_id = getattr(user, "id", None)  # team members must remain distinct submitters
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("a persisted submitting user is required")
    return f"user:{user_id}"


def challenge_xid(challenge_id: int) -> str:
    if type(challenge_id) is not int or challenge_id <= 0:
        raise ValueError("a persisted challenge is required")
    return f"challenge:{challenge_id}"


def submitted_token_digest(secret: str, submitted_token: str, *, challenge_id: int) -> str:
    if not isinstance(secret, str) or not secret:
        raise ValueError("freshness secret is required")
    if not isinstance(submitted_token, str) or not submitted_token or len(submitted_token) > 191:
        raise ValueError("submitted token must contain 1-191 characters")
    challenge_identity = challenge_xid(challenge_id).encode("ascii")
    return _hmac.new(  # short tokens require a key to prevent enumeration from a leaked database
        secret.encode("utf-8"),
        _FLAG_SHARE_DIGEST_DOMAIN + challenge_identity + b"\x00" + submitted_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def flag_share_identity_fields(*, user: object, challenge_id: int, submitted_token: str, secret: str) -> dict[str, str]:
    return {
        "submitter_user_xid": submitter_user_xid(user),
        "challenge_xid": challenge_xid(challenge_id),
        "submitted_token_digest": submitted_token_digest(secret, submitted_token, challenge_id=challenge_id),
    }


class FreshnessFlag(BaseFlag):
    name = "freshness"
    templates: ClassVar[dict[str, str]] = {
        "create": "/plugins/flags/static/create.html",
        "update": "/plugins/flags/static/edit.html",
    }

    @staticmethod
    def compare(chal_key_obj: Flags, provided: str) -> bool:
        user = get_current_user()
        if not user:
            return False

        xid = resolve_xid(user)
        if xid is None:
            return False

        expected = accepted_flags(chal_key_obj.content, get_freshness_secret(), chal_key_obj.challenge_id, xid)

        if chal_key_obj.data and chal_key_obj.data.lower() == "case_insensitive":
            expected = [candidate.lower() for candidate in expected]
            provided = provided.lower()

        return any(_hmac.compare_digest(candidate, provided) for candidate in expected)


def register() -> None:
    FLAG_CLASSES["freshness"] = FreshnessFlag
