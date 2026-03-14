import hashlib
import hmac
import re
import secrets
import string

PLACEHOLDER = "%TOKEN%"
BASE36_ALPHABET = string.digits + string.ascii_lowercase


def generate_secret() -> str:
    return secrets.token_hex(32)


def compute_token(secret: str, challenge_id: int | str, xid: int | str, length: int = 4) -> str:
    msg = f"{challenge_id}:{xid}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()

    num = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(length):
        num, rem = divmod(num, 36)
        chars.append(BASE36_ALPHABET[rem])
    return "".join(chars)


def render_flag(template: str, token: str) -> str:
    return template.replace(PLACEHOLDER, token)


def extract_token(template: str, submission: str) -> str | None:
    if PLACEHOLDER not in template:
        return None

    pattern = "^" + re.escape(template).replace(re.escape(PLACEHOLDER), "(.+)") + "$"
    m = re.match(pattern, submission)
    if m:
        return m.group(1)
    return None
