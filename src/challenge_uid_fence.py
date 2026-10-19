from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tarfile
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import NoReturn

from .exceptions import ContainerUnavailableException

_ENGINE_PINS = {
    ("29.3.1", "f78c987"): (
        "challenge_seccomp_29.3.1.json",
        "57b55753f08dea3fade366e1a84948e8d257ac7086c6ceb697b5f5092602edfe",
    ),
    ("29.7.2", "6a43e3d"): (
        "challenge_seccomp_29.7.2.json",
        "e4736bb8fc46b6ca03b586549bc2961db58f2ef6c48c25777dc54c02d0bccf67",
    ),
    ("29.8.1", "v29.8.1"): (
        "challenge_seccomp_29.7.2.json",
        "e4736bb8fc46b6ca03b586549bc2961db58f2ef6c48c25777dc54c02d0bccf67",
    ),
}
_SAFE_CAPS = {
    "SYS_CHROOT",
    "SETUID",
    "SETGID",
    "CHOWN",
    "DAC_OVERRIDE",
    "AUDIT_WRITE",
    "NET_ADMIN",
    "NET_RAW",
    "SYS_PTRACE",
    "SYS_NICE",
}
_FORBIDDEN_OVERRIDES = {
    "user",
    "group_add",
    "security_opt",
    "privileged",
    "userns_mode",
    "pid_mode",
    "ipc_mode",
    "runtime",
    "devices",
    "device_requests",
    "volumes_from",
    "cap_drop",
}
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*[$]?\Z")
_DECIMAL = re.compile(r"[0-9]+\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_RESERVATION_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_ID = 0x7FFFFFFF
_MAX_FILE_BYTES = 65535
_MAX_ARCHIVE_BYTES = 131072
_GO_MODE_TYPE = 0x8F280000
_GO_MODE_DIR = 0x80000000
_INSPECTOR_LABEL = "ctf.uid-inspector"


def _reject(message: str) -> NoReturn:
    raise ContainerUnavailableException(f"challenge UID policy: {message}")


def _split_user(value: str) -> tuple[str, str, bool]:
    if not isinstance(value, str) or len(value) > 256 or value.count(":") > 1:
        _reject("unsupported USER syntax")
    user, separator, group = value.partition(":")
    for token in (user, group):
        if token and not (_DECIMAL.fullmatch(token) or _TOKEN.fullmatch(token)):
            _reject("unsupported USER token")
        if _DECIMAL.fullmatch(token) and int(token) > _MAX_ID:
            _reject("USER identity exceeds supported range")
    return user, group, bool(separator)


def _rows(content: bytes, kind: str) -> list[list[str]]:
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError:
        _reject("identity files must use supported ASCII syntax")
    if text and not text.endswith("\n"):
        _reject("identity files require a final newline")
    rows = []
    for line in text.split("\n"):
        line = line.strip(" \t\r\v\f")
        if not line or kind == "group" and line.startswith("#"):
            continue
        fields = line.split(":")
        if len(fields) != (7 if kind == "passwd" else 4) or not _TOKEN.fullmatch(fields[0]):
            _reject("unsupported identity row")
        for index in (2, 3) if kind == "passwd" else (2,):
            if not _DECIMAL.fullmatch(fields[index]) or int(fields[index]) > _MAX_ID:
                _reject("unsupported identity number")
        rows.append(fields)
    return rows


def _resolve(value: str, passwd: list[list[str]], groups: list[list[str]]) -> tuple[int, int, tuple[int, ...]]:
    user, group, _ = _split_user(value)
    numeric = not user or bool(_DECIMAL.fullmatch(user))
    uid = int(user or "0") if numeric else 0
    match = (
        next((row for row in passwd if int(row[2]) == uid), None)
        if numeric
        else next((row for row in passwd if row[0] == user), None)
    )
    if match is None and not numeric:
        _reject("USER name is absent from passwd")
    gid = int(match[3]) if match else 0
    if match:
        uid = int(match[2])
    supplementary = []
    if group:
        numeric_group = bool(_DECIMAL.fullmatch(group))
        selected = (
            next((row for row in groups if int(row[2]) == int(group)), None)
            if numeric_group
            else next((row for row in groups if row[0] == group), None)
        )
        if selected is None and not numeric_group:
            _reject("USER group is absent from group")
        gid = int(selected[2]) if selected else int(group)
    elif match:
        supplementary = [int(row[2]) for row in groups if match[0] in row[3].split(",")]
    return uid, gid, tuple(sorted([gid, *supplementary]))


def _response_stat(response):
    return json.loads(base64.b64decode(response.headers["x-docker-container-path-stat"], validate=True))


def _stat(client, container_id: str, path: str):
    response = client.api.head(
        client.api._url("/containers/{0}/archive", container_id),
        params={"path": path},
        timeout=client.api.timeout,
        headers=client.api._general_configs.get("HttpHeaders"),
        stream=True,
    )
    with closing(response):
        if response.status_code == 404:
            return None
        client.api._raise_for_status(response)
        if response.status_code != 200:
            _reject("identity metadata request did not succeed")
        return _response_stat(response)


def _ordinary(stat, path: str, directory: bool = False) -> None:
    if not isinstance(stat, dict) or stat.get("name") != path.rsplit("/", 1)[-1]:
        _reject("identity path metadata is missing")
    expected_type = _GO_MODE_DIR if directory else 0
    if stat.get("linkTarget") or not isinstance(stat.get("mode"), int) or stat["mode"] & _GO_MODE_TYPE != expected_type:
        _reject("identity paths must be ordinary files and directories")
    if not directory and (not isinstance(stat.get("size"), int) or not 0 <= stat["size"] <= _MAX_FILE_BYTES):
        _reject("identity file exceeds the size limit")


def _read_file(client, container, path: str, deadline: float) -> bytes:
    stat = _stat(client, container.id, path)
    if stat is None:
        return b""
    _ordinary(stat, path)
    response = client.api._get(
        client.api._url("/containers/{0}/archive", container.id),
        params={"path": path},
        stream=True,
        headers={"Accept-Encoding": "identity"},
    )
    with closing(response):
        client.api._raise_for_status(response)
        if _response_stat(response) != stat:
            _reject("identity metadata changed")
        content = bytearray()
        for chunk in response.iter_content(chunk_size=8192):
            if time.monotonic() > deadline or len(content) + len(chunk) > _MAX_ARCHIVE_BYTES:
                _reject("identity archive exceeds its limits")
            content.extend(chunk)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
        member = archive.next()
        if member is None or not member.isfile() or member.name != path.rsplit("/", 1)[-1]:
            _reject("identity archive contains an unexpected member")
        if member.size != stat["size"] or member.size > _MAX_FILE_BYTES:
            _reject("identity archive size differs")
        stream = archive.extractfile(member)
        if stream is None:
            _reject("identity archive has no file data")
        with stream:
            result = stream.read(_MAX_FILE_BYTES + 1)
        if archive.next() is not None or len(result) != member.size:
            _reject("identity archive contains extra data")
        return result


def _named_user(client, image, value: str, launch_labels: dict) -> str:
    if image.attrs["Config"].get("Volumes"):
        _reject("named USER with image VOLUME requires separate qualification")
    labels = {key: launch_labels.get(key) for key in ("ctf.instance_id", "ctf.provision_token")}
    if any(not isinstance(value, str) or not _RESERVATION_ID.fullmatch(value) for value in labels.values()):
        _reject("named USER inspection requires main reservation ownership")
    nonce = uuid.uuid4().hex
    inspector = None
    owned_id = None
    primary_error = None
    previous_timeout = client.api.timeout
    deadline = time.monotonic() + 30
    client.api.timeout = min(previous_timeout, 5)
    try:
        inspector = client.containers.create(
            image.id,
            name=f"chal-identity-{nonce}",
            user="0",
            entrypoint=["/bin/false"],
            command=[],
            network_mode="none",
            healthcheck={"test": ["NONE"]},
            restart_policy={"Name": "no"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            labels={**labels, _INSPECTOR_LABEL: nonce, "ctf.role": "uid-inspector"},
        )
        if not isinstance(inspector.id, str) or not _CONTAINER_ID.fullmatch(inspector.id):
            _reject("inspector identity is invalid")
        created_id = inspector.id
        inspector.reload()
        observed_labels = inspector.attrs.get("Config", {}).get("Labels", {})
        if (
            inspector.id != created_id
            or inspector.attrs.get("Image") != image.id
            or observed_labels.get(_INSPECTOR_LABEL) != nonce
            or observed_labels.get("ctf.role") != "uid-inspector"
            or any(observed_labels.get(key) != value for key, value in labels.items())
        ):
            _reject("inspector ownership differs")
        owned_id = created_id
        if inspector.attrs.get("State", {}).get("Status") != "created":
            _reject("inspector state differs")
        _ordinary(_stat(client, inspector.id, "/etc"), "/etc", directory=True)
        passwd = _rows(_read_file(client, inspector, "/etc/passwd", deadline), "passwd")
        groups = _rows(_read_file(client, inspector, "/etc/group", deadline), "group")
        before = _resolve(value, passwd, groups)
        _, group, explicit_group = _split_user(value)
        pinned = str(before[0]) + (":" + group if explicit_group else "")
        if before != _resolve(pinned, passwd, groups):
            _reject("numeric USER changes initial credentials")
        return pinned
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            if inspector is not None and owned_id is not None and inspector.id == owned_id:
                try:
                    inspector.remove(v=True)
                except BaseException:
                    if primary_error is None:
                        raise
        finally:
            client.api.timeout = previous_timeout


def prepare_challenge(client, image_name: str, kwargs: dict) -> tuple[str, str, list[str]]:
    if _FORBIDDEN_OVERRIDES & kwargs.keys():
        _reject("unsafe launch override")
    caps = kwargs.get("cap_add", [])
    if not isinstance(caps, list) or any(cap not in _SAFE_CAPS for cap in caps):
        _reject("unsupported capabilities")
    version = client.version()
    key = version.get("Version"), version.get("GitCommit")
    if key not in _ENGINE_PINS or version.get("Os") != "linux" or version.get("Arch") != "amd64":
        _reject("unqualified Docker engine")
    info = client.info()
    options = info.get("SecurityOptions", [])
    if (
        not isinstance(options, list)
        or info.get("DefaultRuntime") != "runc"
        or "name=seccomp,profile=builtin" not in options
        or any("userns" in option or "rootless" in option for option in options)
    ):
        _reject("unsupported daemon isolation mode")
    profile_name, profile_digest = _ENGINE_PINS[key]
    profile = Path(__file__).with_name(profile_name).read_bytes()
    if hashlib.sha256(profile).hexdigest() != profile_digest:
        _reject("seccomp profile pin differs")
    image = client.images.get(image_name)
    if not isinstance(image.id, str) or not _IMAGE_ID.fullmatch(image.id):
        _reject("image ID is not immutable")
    value = image.attrs.get("Config", {}).get("User", "")
    user, group, explicit_group = _split_user(value)
    pinned = (
        str(int(user or "0")) + (":" + group if explicit_group else "")
        if not user or _DECIMAL.fullmatch(user)
        else _named_user(client, image, value, kwargs.get("labels", {}))
    )
    return (
        image.id,
        pinned,
        ["no-new-privileges:true", "seccomp=" + profile.decode("ascii").strip()],
    )
