from __future__ import annotations

import hashlib
import hmac
import json
import re
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import docker
import pymysql
import requests

BASE_URL = "http://127.0.0.1:8000"
STATE_PATH = Path("/var/uploads/cc-e2e-state.json")
IMAGE = "local.test/ctf/python:3.12-alpine"
PASSWORD = "correct horse battery staple"


def check(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def nonce_from(response: requests.Response) -> str:
    patterns = (
        r'name=["\']nonce["\'][^>]*value=["\']([^"\']+)',
        r'value=["\']([^"\']+)["\'][^>]*name=["\']nonce["\']',
        r'csrfNonce["\']?\s*:\s*["\']([^"\']+)',
    )
    for pattern in patterns:
        match = re.search(pattern, response.text)
        if match:
            return match.group(1)
    raise AssertionError(f"CSRF nonce not found at {response.url}")


def request_json(
    session: requests.Session,
    method: str,
    path: str,
    nonce: str,
    *,
    expected: tuple[int, ...] = (200,),
    payload: object | None = None,
) -> dict:
    response = session.request(
        method,
        BASE_URL + path,
        json=payload,
        headers={"CSRF-Token": nonce, "Accept": "application/json"},
        timeout=30,
    )
    check(response.status_code in expected, f"{method} {path}: {response.status_code} {response.text[:1000]}")
    return response.json()


def login(name: str, password: str = PASSWORD) -> tuple[requests.Session, str]:
    session = requests.Session()
    page = session.get(BASE_URL + "/login", timeout=20)
    page.raise_for_status()
    nonce = nonce_from(page)
    response = session.post(
        BASE_URL + "/login",
        data={"name": name, "password": password, "nonce": nonce},
        allow_redirects=False,
        timeout=20,
    )
    check(response.status_code == 302, f"login failed for {name}: {response.status_code} {response.text[:500]}")
    authenticated_page = session.get(BASE_URL + "/", timeout=20)
    authenticated_page.raise_for_status()
    return session, nonce_from(authenticated_page)


def database():
    return pymysql.connect(host="db", user="ctfd", password="ctfd", database="ctfd", autocommit=True)


def rows(sql: str, args: tuple = ()) -> list[dict]:
    connection = database()
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, args)
            return list(cursor.fetchall())
    finally:
        connection.close()


def scalar(sql: str, args: tuple = ()):
    result = rows(sql, args)
    return next(iter(result[0].values())) if result else None


def execute(sql: str, args: tuple = ()) -> None:
    connection = database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, args)
    finally:
        connection.close()


def setup() -> None:
    session = requests.Session()
    page = session.get(BASE_URL + "/setup", timeout=30)
    page.raise_for_status()
    nonce = nonce_from(page)
    response = session.post(
        BASE_URL + "/setup",
        data={
            "ctf_name": "Challenge Containers E2E",
            "ctf_description": "isolated fresh install",
            "name": "admin",
            "email": "admin@example.test",
            "password": PASSWORD,
            "user_mode": "users",
            "ctf_theme": "core-beta",
            "nonce": nonce,
        },
        allow_redirects=False,
        timeout=30,
    )
    check(response.status_code == 302, f"setup failed: {response.status_code} {response.text[:1000]}")


def create_challenge(admin: requests.Session, nonce: str, name: str, **overrides: object) -> int:
    payload: dict[str, object] = {
        "name": name,
        "category": "e2e",
        "description": "isolated lifecycle test",
        "value": 100,
        "state": "visible",
        "type": "container",
        "function": "static",
        "image": IMAGE,
        "port": 8080,
        "command": "python -m http.server 8080 --directory /tmp",
        "ctype": "web",
        "expiration_seconds": 60,
        "max_renewals": 1,
        "max_memory_mb": 64,
        "max_cpu": 0.5,
    }
    payload.update(overrides)
    result = request_json(admin, "POST", "/api/v1/challenges", nonce, payload=payload)
    check(result.get("success") is True, f"challenge creation failed: {result}")
    return int(result["data"]["id"])


def start(session: requests.Session, nonce: str, challenge_id: int, expected=(200,)) -> tuple[dict, int]:
    response = session.post(
        BASE_URL + "/containers/api/request",
        json={"chal_id": challenge_id},
        headers={"CSRF-Token": nonce, "Accept": "application/json"},
        timeout=40,
    )
    check(response.status_code in expected, f"start {challenge_id}: {response.status_code} {response.text[:1000]}")
    return response.json(), response.status_code


def stop(session: requests.Session, nonce: str, challenge_id: int) -> dict:
    return request_json(session, "POST", "/containers/api/stop", nonce, payload={"chal_id": challenge_id})


def inspect_started(challenge_id: int, response: dict, *, expected_members: int = 1) -> tuple[str, list]:
    check(response.get("status") in {"created", "already_running"}, f"unexpected start response: {response}")
    port = int(response["port"])
    reachable = None
    for _attempt in range(30):
        try:
            reachable = requests.get(f"http://127.0.0.1:{port}/", timeout=2)
            if reachable.status_code < 500:
                break
        except requests.RequestException:
            pass
        time.sleep(0.1)
    check(reachable is not None and reachable.status_code < 500, "published challenge port was not usable")

    instance_rows = rows("SELECT * FROM container_instances WHERE challenge_id=%s", (challenge_id,))
    check(len(instance_rows) == 1 and instance_rows[0]["state"] == "running", "logical instance was not running")
    instance_id = instance_rows[0]["id"]
    physical = rows("SELECT * FROM container_info WHERE instance_id=%s", (instance_id,))
    check(len(physical) == expected_members, f"expected {expected_members} physical rows, got {len(physical)}")
    check(
        scalar("SELECT COUNT(*) FROM container_history WHERE instance_id=%s", (instance_id,)) == expected_members,
        "history rows missing",
    )

    client = docker.from_env()
    containers = client.containers.list(all=True, filters={"label": f"ctf.instance_id={instance_id}"})
    check(len(containers) == expected_members, f"expected {expected_members} labelled Docker containers")
    for container in containers:
        labels = container.attrs["Config"]["Labels"]
        check(labels.get("ctf.instance_id") == instance_id, "instance label mismatch")
        check(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", container.attrs["Config"]["Hostname"]), "unsafe hostname")
        check("no-new-privileges:true" in container.attrs["HostConfig"]["SecurityOpt"], "no-new-privileges missing")
        check("ALL" in container.attrs["HostConfig"]["CapDrop"], "capability drop missing")
        check(container.attrs["HostConfig"]["PidsLimit"] == 256, "PID limit mismatch")
    return instance_id, containers


def compute_token(secret: str, challenge_id: int, user_id: int, length: int = 6) -> str:
    digest = hmac.new(secret.encode(), f"{challenge_id}:{user_id}".encode(), hashlib.sha256).digest()
    number = int.from_bytes(digest[:8], "big")
    alphabet = string.digits + string.ascii_lowercase
    token = []
    for _ in range(length):
        number, remainder = divmod(number, 36)
        token.append(alphabet[remainder])
    return "".join(token)


def bootstrap() -> None:
    setup()
    admin, admin_nonce = login("admin")

    info_columns = rows(
        "SELECT COLUMN_NAME, IS_NULLABLE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA='ctfd' AND TABLE_NAME='container_info'"
    )
    info_map = {row["COLUMN_NAME"]: row["IS_NULLABLE"] for row in info_columns}
    check(info_map.get("instance_id") == "NO", "fresh schema did not enforce container_info.instance_id")
    context_columns = {
        row["COLUMN_NAME"]
        for row in rows(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='ctfd' AND TABLE_NAME='docker_contexts'"
        )
    }
    check("state" in context_columns, "fresh schema is missing docker_contexts.state")
    share_columns = {
        row["COLUMN_NAME"]
        for row in rows(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA='ctfd' AND TABLE_NAME='container_flag_shares'"
        )
    }
    check("submitted_token" not in share_columns, "raw submitted token column exists")

    user_result = request_json(
        admin,
        "POST",
        "/api/v1/users",
        admin_nonce,
        payload={
            "name": "player",
            "email": "player@example.test",
            "password": PASSWORD,
            "verified": True,
        },
    )
    user_id = int(user_result["data"]["id"])

    request_json(
        admin,
        "PUT",
        "/containers/api/settings",
        admin_nonce,
        payload={"post_solve_expiry_seconds": 2, "mutation_rate_limit_requests": 100},
    )

    standalone = create_challenge(admin, admin_nonce, "standalone")
    race = create_challenge(admin, admin_nonce, "race")
    stack = create_challenge(
        admin,
        admin_nonce,
        "stack",
        services_json={"worker": {"image": IMAGE, "command": "sleep 300"}},
    )
    volume = create_challenge(
        admin,
        admin_nonce,
        "volume",
        volumes={
            "schema_version": 1,
            "scope": "entry",
            "mounts": [
                {
                    "type": "volume",
                    "name": "assets",
                    "target": "/opt/challenge/assets",
                    "read_only": True,
                }
            ],
        },
    )
    freshness = create_challenge(admin, admin_nonce, "freshness")
    request_json(
        admin,
        "POST",
        "/api/v1/flags",
        admin_nonce,
        payload={"content": "ctf{e2e_%TOKEN%}", "type": "freshness", "challenge": freshness},
    )

    invalid = admin.post(
        BASE_URL + "/api/v1/challenges",
        json={
            "name": "invalid mount",
            "category": "e2e",
            "description": "must fail",
            "value": 1,
            "state": "hidden",
            "type": "container",
            "image": IMAGE,
            "port": 8080,
            "volumes": {"/host": {"bind": "/data", "mode": "rw"}},
        },
        headers={"CSRF-Token": admin_nonce},
        timeout=20,
    )
    check(invalid.status_code != 200, "unversioned/writable mount configuration was accepted")

    user, user_nonce = login("player")
    created, _ = start(user, user_nonce, standalone)
    instance_id, containers = inspect_started(standalone, created)
    host_config = containers[0].attrs["HostConfig"]
    check(host_config["Memory"] == 64 * 1024 * 1024, "memory limit mismatch")
    check(host_config["CpuQuota"] == 50_000 and host_config["CpuPeriod"] == 100_000, "CPU limit mismatch")

    duplicate, _ = start(user, user_nonce, standalone)
    check(duplicate.get("status") == "already_running", f"duplicate start was not idempotent: {duplicate}")
    renewed = request_json(user, "POST", "/containers/api/renew", user_nonce, payload={"chal_id": standalone})
    check(renewed.get("success") == "container renewed", f"renewal failed: {renewed}")
    exhausted = user.post(
        BASE_URL + "/containers/api/renew",
        json={"chal_id": standalone},
        headers={"CSRF-Token": user_nonce},
        timeout=20,
    )
    check(exhausted.status_code == 200 and "error" in exhausted.json(), "renewal limit was not enforced")

    def race_start() -> tuple[dict, int]:
        race_session, race_nonce = login("player")
        return start(race_session, race_nonce, race, expected=(200, 429))

    with ThreadPoolExecutor(max_workers=2) as pool:
        race_results = list(pool.map(lambda _index: race_start(), range(2)))
    check(
        scalar("SELECT COUNT(*) FROM container_instances WHERE challenge_id=%s", (race,)) == 1,
        "race created duplicate logical instances",
    )
    check(
        len(docker.from_env().containers.list(all=True, filters={"label": "ctf.instance_id"})) == 2,
        f"race created unexpected Docker resources: {race_results}",
    )
    stop(user, user_nonce, race)

    state = {
        "user_id": user_id,
        "standalone": standalone,
        "standalone_instance": instance_id,
        "stack": stack,
        "volume": volume,
        "freshness": freshness,
        "freshness_secret": scalar("SELECT value FROM container_settings WHERE `key`='freshness_secret'"),
    }
    STATE_PATH.write_text(json.dumps(state))
    print("bootstrap phase passed")


def lifecycle() -> None:
    state = json.loads(STATE_PATH.read_text())
    admin, admin_nonce = login("admin")
    user, user_nonce = login("player")
    standalone = int(state["standalone"])

    viewed = request_json(user, "POST", "/containers/api/view_info", user_nonce, payload={"chal_id": standalone})
    check(viewed.get("status") == "already_running", f"instance did not survive restart: {viewed}")

    entry_id = scalar("SELECT container_id FROM container_info WHERE challenge_id=%s AND is_entry=1", (standalone,))
    extension = request_json(
        admin,
        "POST",
        "/containers/api/admin_extend",
        admin_nonce,
        payload={"container_id": entry_id},
    )
    check("success" in extension, f"admin extension failed: {extension}")
    logical_expiry = scalar("SELECT expires FROM container_instances WHERE challenge_id=%s", (standalone,))
    physical_expiry = scalar("SELECT expires FROM container_info WHERE challenge_id=%s", (standalone,))
    check(logical_expiry == physical_expiry, "admin extension did not update logical and physical expiry")

    runtime_update = admin.patch(
        BASE_URL + f"/api/v1/challenges/{standalone}",
        json={"command": "sleep 300"},
        headers={"CSRF-Token": admin_nonce},
        timeout=20,
    )
    check(runtime_update.status_code == 500, "live runtime configuration mutation was accepted")
    deletion = admin.delete(
        BASE_URL + f"/api/v1/challenges/{standalone}",
        json={},
        headers={"CSRF-Token": admin_nonce},
        timeout=20,
    )
    check(deletion.status_code == 409, f"active challenge deletion was not rejected: {deletion.status_code}")

    contexts = request_json(admin, "GET", "/containers/api/contexts/list", admin_nonce)["contexts"]
    local_id = next(item["id"] for item in contexts if item["context_name"] == "local")
    endpoint_change = admin.put(
        BASE_URL + f"/containers/api/contexts/update/{local_id}",
        json={"hostname": "other-daemon.example.test"},
        headers={"CSRF-Token": admin_nonce},
        timeout=20,
    )
    check(endpoint_change.status_code == 409, "active context endpoint mutation was accepted")

    check("success" in stop(user, user_nonce, standalone), "standalone cleanup failed")
    check(
        scalar("SELECT COUNT(*) FROM container_instances WHERE challenge_id=%s", (standalone,)) == 0,
        "logical row leaked after stop",
    )
    check(
        not docker.from_env().containers.list(
            all=True, filters={"label": f"ctf.instance_id={state['standalone_instance']}"}
        ),
        "Docker container leaked after stop",
    )

    volume_response, _ = start(user, user_nonce, int(state["volume"]))
    _volume_instance, volume_containers = inspect_started(int(state["volume"]), volume_response)
    mounts = volume_containers[0].attrs["Mounts"]
    check(
        len(mounts) == 1 and mounts[0]["Name"] == "cc-e2e-assets" and mounts[0]["RW"] is False,
        "read-only volume policy failed",
    )
    stop(user, user_nonce, int(state["volume"]))

    stack_response, _ = start(user, user_nonce, int(state["stack"]))
    stack_instance, stack_containers = inspect_started(int(state["stack"]), stack_response, expected_members=2)
    client = docker.from_env()
    networks = client.networks.list(filters={"label": f"ctf.instance_id={stack_instance}"})
    check(len(networks) == 1, "stack network label missing")
    published = [container for container in stack_containers if container.attrs["NetworkSettings"]["Ports"]]
    check(len(published) == 1, "stack published more than the entry port")
    stop(user, user_nonce, int(state["stack"]))
    check(not client.networks.list(filters={"label": f"ctf.instance_id={stack_instance}"}), "stack network leaked")

    fresh_response, _ = start(user, user_nonce, int(state["freshness"]))
    fresh_instance, _ = inspect_started(int(state["freshness"]), fresh_response)
    token = compute_token(state["freshness_secret"], int(state["freshness"]), int(state["user_id"]))
    attempt = request_json(
        user,
        "POST",
        "/api/v1/challenges/attempt",
        user_nonce,
        payload={"challenge_id": int(state["freshness"]), "submission": f"ctf{{e2e_{token}}}"},
    )
    check(attempt.get("data", {}).get("status") == "correct", f"freshness solve failed: {attempt}")
    solved = rows("SELECT solved_at, expires FROM container_instances WHERE id=%s", (fresh_instance,))[0]
    check(solved["solved_at"] is not None, "solve did not mark logical lifecycle")
    check(
        scalar("SELECT reason FROM container_history WHERE instance_id=%s", (fresh_instance,)) == "solved",
        "solve history marker missing",
    )
    state["fresh_instance"] = fresh_instance
    STATE_PATH.write_text(json.dumps(state))
    print("lifecycle phase passed")


def verify_expiry() -> None:
    state = json.loads(STATE_PATH.read_text())
    instance_id = state["fresh_instance"]
    deadline = time.monotonic() + 30
    while (
        scalar("SELECT COUNT(*) FROM container_instances WHERE id=%s", (instance_id,)) and time.monotonic() < deadline
    ):
        time.sleep(0.5)
    check(
        scalar("SELECT COUNT(*) FROM container_instances WHERE id=%s", (instance_id,)) == 0,
        "expired logical instance remains",
    )
    check(
        scalar("SELECT COUNT(*) FROM container_info WHERE instance_id=%s", (instance_id,)) == 0,
        "expired physical rows remain",
    )
    history = rows("SELECT reason, stopped_at FROM container_history WHERE instance_id=%s", (instance_id,))
    check(
        len(history) == 1 and history[0]["reason"] == "solved" and history[0]["stopped_at"] is not None,
        "solved history was not preserved",
    )
    check(
        not docker.from_env().containers.list(all=True, filters={"label": f"ctf.instance_id={instance_id}"}),
        "expired Docker resource remains",
    )
    check(
        scalar(
            "SELECT COUNT(*) FROM solves WHERE challenge_id=%s AND user_id=%s", (state["freshness"], state["user_id"])
        )
        == 1,
        "durable solve missing",
    )
    print("expiry verification passed")


def disable_freshness() -> None:
    admin, admin_nonce = login("admin")
    request_json(
        admin,
        "POST",
        "/containers/api/settings/freshness-secret",
        admin_nonce,
        payload={"action": "disable"},
    )
    check(
        scalar("SELECT value FROM container_settings WHERE `key`='freshness_secret'") == "",
        "clearing the freshness secret was not persisted",
    )
    print("freshness disabled")


def verify_freshness_disabled() -> None:
    state = json.loads(STATE_PATH.read_text())
    check(
        scalar("SELECT value FROM container_settings WHERE `key`='freshness_secret'") == "",
        "plugin startup regenerated an explicitly disabled freshness secret",
    )
    admin, admin_nonce = login("admin")
    settings = request_json(admin, "GET", "/containers/api/settings", admin_nonce)
    check(
        settings["settings"]["freshness_secret"]["configured"] is False
        and settings["settings"]["freshness_secret"]["value"] == "",
        "disabled freshness setting changed after restart",
    )

    context = next(
        item
        for item in request_json(admin, "GET", "/containers/api/contexts/list", admin_nonce)["contexts"]
        if item["context_name"] == "local"
    )
    client = docker.from_env()
    orphan_network = client.networks.create(
        "cc-e2e-orphan-guard",
        labels={"ctf.instance_id": "f" * 32},
    )
    try:
        endpoint_change = admin.put(
            BASE_URL + f"/containers/api/contexts/update/{context['id']}",
            json={"hostname": "worker.example.test"},
            headers={"CSRF-Token": admin_nonce},
            timeout=20,
        )
        check(endpoint_change.status_code == 409, "network-only Docker orphan did not block endpoint mutation")
        check(endpoint_change.json().get("docker_networks") == 1, "network-only orphan was not reported")
    finally:
        orphan_network.remove()

    now = time.time()
    logical_only_id = "e" * 32
    execute(
        "INSERT INTO container_instances "
        "(id, owner_key, user_id, challenge_id, quota_slot, state, state_version, docker_context_id, "
        "placement_units, provision_token, created_at, updated_at, expires, renewals_used) "
        "VALUES (%s, %s, %s, %s, 0, 'cleanup_pending', 0, %s, 1, %s, %s, %s, %s, 0)",
        (
            logical_only_id,
            f"user:{state['user_id']}",
            state["user_id"],
            state["standalone"],
            context["id"],
            "d" * 32,
            now,
            now,
            int(now) + 300,
        ),
    )
    lifecycles = request_json(admin, "GET", "/containers/api/running_containers", admin_nonce)["containers"]
    logical_only = next((row for row in lifecycles if row.get("instance_id") == logical_only_id), None)
    check(logical_only is not None and logical_only["cleanup_only"] is True, "logical-only cleanup was hidden")
    cleanup = request_json(
        admin,
        "POST",
        "/containers/api/cleanup",
        admin_nonce,
        payload={"instance_id": logical_only_id},
    )
    check("success" in cleanup, f"logical-only cleanup retry failed: {cleanup}")
    check(
        scalar("SELECT COUNT(*) FROM container_instances WHERE id=%s", (logical_only_id,)) == 0,
        "logical-only cleanup row remains",
    )
    check(
        scalar("SELECT COUNT(*) FROM container_maintenance WHERE name='expiry'") == 1,
        "automatic maintenance heartbeat missing",
    )
    print("freshness remained disabled after restart")


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase == "bootstrap":
        bootstrap()
    elif phase == "lifecycle":
        lifecycle()
    elif phase == "verify-expiry":
        verify_expiry()
    elif phase == "disable-freshness":
        disable_freshness()
    elif phase == "verify-freshness-disabled":
        verify_freshness_disabled()
    else:
        raise SystemExit(
            "usage: scenario.py bootstrap|lifecycle|verify-expiry|disable-freshness|verify-freshness-disabled"
        )
