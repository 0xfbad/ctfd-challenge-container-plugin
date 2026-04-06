import json
from unittest.mock import patch

from docker_host_manager import _resolve_endpoint, _scan_context_meta


def test_ssh_hostname_with_user():
    result = _resolve_endpoint("x", "user@host")
    assert result == "ssh://user@host"


def test_ssh_hostname_without_user():
    result = _resolve_endpoint("x", "host")
    assert result == "ssh://root@host"


def test_no_hostname_socket_fallback(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.touch()

    with patch("docker_host_manager.LOCAL_SOCKET_PATH", str(sock)):
        result = _resolve_endpoint("x", None)
    assert result == f"unix://{sock}"


def test_no_hostname_no_socket_returns_none(tmp_path):
    with patch("docker_host_manager.LOCAL_SOCKET_PATH", str(tmp_path / "nonexistent.sock")):
        result = _resolve_endpoint("x", None)
    assert result is None


def test_meta_json_scans_by_name(tmp_path):
    """docker stores contexts by hash, not name. the scanner matches on the Name field."""
    meta_base = tmp_path / "contexts" / "meta"
    hash_dir = meta_base / "abc123hash"
    hash_dir.mkdir(parents=True)
    (hash_dir / "meta.json").write_text(json.dumps({
        "Name": "myctx",
        "Endpoints": {"docker": {"Host": "tcp://10.0.0.1:2375"}},
    }))

    with patch("docker_host_manager.os.path.expanduser", return_value=str(meta_base)):
        result = _resolve_endpoint("myctx", "user@host")

    assert result == "tcp://10.0.0.1:2375"


def test_meta_json_wrong_name_falls_through(tmp_path):
    """if no context matches the Name field, fall through to SSH."""
    meta_base = tmp_path / "contexts" / "meta"
    hash_dir = meta_base / "abc123hash"
    hash_dir.mkdir(parents=True)
    (hash_dir / "meta.json").write_text(json.dumps({
        "Name": "other_context",
        "Endpoints": {"docker": {"Host": "tcp://10.0.0.1:2375"}},
    }))

    with patch("docker_host_manager.os.path.expanduser", return_value=str(meta_base)):
        result = _resolve_endpoint("myctx", "user@host")

    assert result == "ssh://user@host"


def test_scan_context_meta_returns_all(tmp_path):
    meta_base = tmp_path / "contexts" / "meta"
    for i, name in enumerate(["ctx_a", "ctx_b"]):
        d = meta_base / f"hash{i}"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({
            "Name": name,
            "Endpoints": {"docker": {"Host": f"tcp://10.0.0.{i}:2375"}},
        }))

    with patch("docker_host_manager.os.path.expanduser", return_value=str(meta_base)):
        results = _scan_context_meta()

    names = {m["Name"] for m in results}
    assert names == {"ctx_a", "ctx_b"}
