import threading
from unittest.mock import patch, MagicMock
from docker_host_manager import DockerHostManager


def make_host_manager(contexts=None):
    if contexts is None:
        contexts = {"default": "unix:///var/run/docker.sock"}

    hm = DockerHostManager()
    hm._context_configs = dict(contexts)
    hm._pub_hostnames = {name: "localhost" for name in contexts}
    return hm


def test_semaphore_acquire_release():
    hm = make_host_manager()
    hm._semaphores["default"] = threading.BoundedSemaphore(1)

    assert hm.acquire_semaphore("default")
    hm.release_semaphore("default")

    assert hm.acquire_semaphore("default")
    hm.release_semaphore("default")


def test_semaphore_missing_context_returns_true():
    hm = make_host_manager()
    assert hm.acquire_semaphore("nonexistent") is True


def test_get_client_creates_new():
    hm = make_host_manager()

    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        client = hm._get_client("default")
        assert client is mock_client


def test_get_client_caches():
    hm = make_host_manager()

    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        c1 = hm._get_client("default")
        c2 = hm._get_client("default")
        assert c1 is c2


def test_get_client_unknown_context_raises():
    hm = make_host_manager()

    try:
        hm._get_client("nonexistent")
        assert False, "should have raised"
    except Exception as e:
        assert "no client" in str(e)


def test_clear_client_closes_and_removes():
    hm = make_host_manager()

    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
        assert "default" in hm._clients

        hm._clear_client("default")
        assert "default" not in hm._clients
        mock_client.close.assert_called_once()


def test_init_semaphores_creates_per_context():
    hm = make_host_manager({"a": "url_a", "b": "url_b"})
    hm._semaphores = {}

    hm._init_semaphores(2)
    assert "a" in hm._semaphores
    assert "b" in hm._semaphores


def test_generation_counter_invalidates_cache():
    hm = make_host_manager()

    mock_client_1 = MagicMock()
    mock_client_2 = MagicMock()

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client_1):
        c1 = hm._get_client("default")
        assert c1 is mock_client_1

    hm._config_generation += 1

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client_2):
        c2 = hm._get_client("default")
        assert c2 is mock_client_2
        assert c2 is not c1
        mock_client_1.close.assert_called_once()
