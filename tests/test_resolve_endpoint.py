import json
from unittest.mock import patch

from container_manager import _resolve_endpoint


def test_ssh_hostname_with_user():
    result = _resolve_endpoint("x", "user@host")
    assert result == "ssh://user@host"


def test_ssh_hostname_without_user():
    result = _resolve_endpoint("x", "host")
    assert result == "ssh://root@host"


def test_no_hostname_socket_fallback(tmp_path):
    sock = tmp_path / "docker.sock"
    sock.touch()

    with patch("container_manager.LOCAL_SOCKET_PATH", str(sock)):
        result = _resolve_endpoint("x", None)
    assert result == f"unix://{sock}"


def test_no_hostname_no_socket_returns_none(tmp_path):
    with patch("container_manager.LOCAL_SOCKET_PATH", str(tmp_path / "nonexistent.sock")):
        result = _resolve_endpoint("x", None)
    assert result is None


def test_meta_json_priority(tmp_path):
    meta_dir = tmp_path / ".docker" / "contexts" / "meta" / "myctx"
    meta_dir.mkdir(parents=True)
    meta_file = meta_dir / "meta.json"
    meta_file.write_text(json.dumps({"Endpoints": {"docker": {"Host": "tcp://10.0.0.1:2375"}}}))

    with patch("os.path.expanduser", return_value=str(meta_file)):
        result = _resolve_endpoint("myctx", "user@host")

    # meta.json endpoint takes priority over SSH hostname
    assert result == "tcp://10.0.0.1:2375"
