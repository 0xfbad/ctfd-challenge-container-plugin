from __future__ import annotations

import hashlib
import hmac as _hmac
from typing import ClassVar

from CTFd.models import Flags
from CTFd.plugins.flags import FLAG_CLASSES, BaseFlag
from CTFd.utils.user import get_current_user

from .freshness import compute_token, render_flag
from .utils import _TOKEN_LENGTH_KEY, get_setting, resolve_xid

_FLAG_SHARE_DIGEST_DOMAIN = b"ctfd-challenge-containers:flag-share-token:v1\x00"


def submitter_user_xid(user: object) -> str:
    """Return the immutable, non-null external identity used for deduplication.

    This deliberately identifies the submitting user even in team mode. Two
    teammates are distinct submitters, and nullable team IDs cannot weaken the
    database uniqueness invariant.
    """

    user_id = getattr(user, "id", None)
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("a persisted submitting user is required")
    return f"user:{user_id}"


def challenge_xid(challenge_id: int) -> str:
    """Return the immutable, non-null challenge identity used for deduplication."""

    if type(challenge_id) is not int or challenge_id <= 0:
        raise ValueError("a persisted challenge is required")
    return f"challenge:{challenge_id}"


def submitted_token_digest(secret: str, submitted_token: str, *, challenge_id: int) -> str:
    """Create a domain-separated keyed digest for storage and uniqueness.

    Freshness tokens are intentionally short, so an unkeyed SHA-256 value is
    vulnerable to cheap enumeration if the database is exposed. The keyed
    digest avoids retaining the submitted token in the database.
    """

    if not isinstance(secret, str) or not secret:
        raise ValueError("freshness secret is required")
    if not isinstance(submitted_token, str) or not submitted_token or len(submitted_token) > 191:
        raise ValueError("submitted token must contain 1-191 characters")
    challenge_identity = challenge_xid(challenge_id).encode("ascii")
    return _hmac.new(
        secret.encode("utf-8"),
        _FLAG_SHARE_DIGEST_DOMAIN + challenge_identity + b"\x00" + submitted_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def flag_share_identity_fields(*, user: object, challenge_id: int, submitted_token: str, secret: str) -> dict[str, str]:
    """Return immutable identities and a keyed digest for incident deduplication."""

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
        secret_raw = get_setting("freshness_secret")
        if not secret_raw:
            return False
        secret = str(secret_raw)

        user = get_current_user()
        if not user:
            return False

        xid = resolve_xid(user)
        if xid is None:
            return False

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
