import hashlib
import hmac
import re
import secrets
import string

PLACEHOLDER = "%TOKEN%"
BASE36_ALPHABET = string.digits + string.ascii_lowercase
MIN_TOKEN_LENGTH = 6
MAX_TOKEN_LENGTH = 12  # the token draws on 64 hmac bits, which fill 12 base36 characters
TOKEN_LENGTHS = range(MIN_TOKEN_LENGTH, MAX_TOKEN_LENGTH + 1)
COLLISION_SCALE = 5000  # owners squared times this stays under 36 to the length, so two owners share a token on one challenge under 1 in 10000


def generate_secret() -> str:
    return secrets.token_hex(32)


def token_length_for(owner_count: int) -> int:
    length = MIN_TOKEN_LENGTH
    while length < MAX_TOKEN_LENGTH and owner_count * owner_count * COLLISION_SCALE > 36**length:
        length += 1

    return length


def compute_token(secret: str, challenge_id: int | str, xid: int | str, length: int = MIN_TOKEN_LENGTH) -> str:
    msg = f"{challenge_id}:{xid}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()

    num = int.from_bytes(digest[:8], "big")
    chars = []
    for _ in range(length):
        num, rem = divmod(num, 36)
        chars.append(BASE36_ALPHABET[rem])
    return "".join(chars)


def compute_seed(secret: str, challenge_id: int | str, xid: int | str, *, team_mode: bool = False) -> str:
    owner = f"{'team' if team_mode else 'user'}:{xid}"
    message = b"ctfd-freshness:seed:v1\x00" + f"{challenge_id}:{owner}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def render_flag(template: str, token: str) -> str:
    return template.replace(PLACEHOLDER, token)


def accepted_flags(template: str, secret: str, challenge_id: int | str, xid: int | str) -> list[str]:
    return [render_flag(template, compute_token(secret, challenge_id, xid, length)) for length in TOKEN_LENGTHS]


def extract_token(template: str, submission: str) -> str | None:
    if PLACEHOLDER not in template:
        return None

    pattern = "^" + re.escape(template).replace(re.escape(PLACEHOLDER), "(.+)") + "$"
    m = re.match(pattern, submission)
    if m:
        return m.group(1)
    return None
