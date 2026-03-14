import threading
from collections import defaultdict
from unittest.mock import patch, MagicMock
from container_manager import ContainerManager, ContainerException, _ThreadLocalClients


class SynchronousPool:
    def __init__(self, maxsize=4):
        self.size = maxsize

    def spawn(self, fn, *args, **kwargs):
        result = fn(*args, **kwargs)

        class FakeResult:
            def get(self):
                return result

        return FakeResult()

    def submit(self, fn, *args, **kwargs):
        result = fn(*args, **kwargs)

        class FakeFuture:
            def result(self):
                return result

        return FakeFuture()


def make_manager(contexts=None):
    if contexts is None:
        contexts = {"default": "unix:///var/run/docker.sock"}

    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm._context_configs = dict(contexts)
    cm._context_weights = {name: 1 for name in contexts}
    cm._health = {name: True for name in contexts}
    cm._container_counts = defaultdict(int)
    cm._context_lock = threading.Lock()
    cm._config_generation = 0
    cm._pool = SynchronousPool()
    cm._semaphores = {}
    cm._thread_local = _ThreadLocalClients()
    return cm


def test_submit_runs_function():
    cm = make_manager()
    result = cm._submit(lambda x: x * 2, 21)
    assert result == 42


def test_submit_propagates_exception():
    cm = make_manager()

    def bad():
        raise ValueError("boom")

    try:
        cm._submit(bad)
        assert False, "should have raised"
    except ValueError as e:
        assert str(e) == "boom"


def test_semaphore_acquire_release():
    cm = make_manager()
    cm._semaphores["default"] = threading.BoundedSemaphore(1)

    assert cm._acquire_semaphore("default")
    cm._release_semaphore("default")

    # should be able to acquire again after release
    assert cm._acquire_semaphore("default")
    cm._release_semaphore("default")


def test_semaphore_missing_context_returns_true():
    cm = make_manager()
    assert cm._acquire_semaphore("nonexistent") is True


def test_thread_local_clients_are_isolated():
    tl = _ThreadLocalClients()
    tl.clients["ctx"] = "main_client"

    result = {}

    def check():
        result["has_ctx"] = "ctx" in tl.clients

    t = threading.Thread(target=check)
    t.start()
    t.join()

    assert tl.clients["ctx"] == "main_client"
    assert result["has_ctx"] is False


def test_get_client_creates_new():
    cm = make_manager()

    mock_client = MagicMock()
    with patch("container_manager.docker.DockerClient", return_value=mock_client):
        client = cm._get_client("default")
        assert client is mock_client


def test_get_client_caches():
    cm = make_manager()

    mock_client = MagicMock()
    with patch("container_manager.docker.DockerClient", return_value=mock_client):
        c1 = cm._get_client("default")
        c2 = cm._get_client("default")
        assert c1 is c2


def test_get_client_unknown_context_raises():
    cm = make_manager()

    try:
        cm._get_client("nonexistent")
        assert False, "should have raised"
    except ContainerException as e:
        assert "not available" in str(e)


def test_clear_thread_local_client():
    cm = make_manager()

    mock_client = MagicMock()
    with patch("container_manager.docker.DockerClient", return_value=mock_client):
        cm._get_client("default")
        assert "default" in cm._thread_local.clients

        cm._clear_thread_local_client("default")
        assert "default" not in cm._thread_local.clients


def test_init_semaphores_creates_per_context():
    cm = make_manager({"a": "url_a", "b": "url_b"})
    cm._semaphores = {}

    cm._init_semaphores()
    assert "a" in cm._semaphores
    assert "b" in cm._semaphores


def test_generation_counter_invalidates_cache():
    cm = make_manager()

    mock_client_1 = MagicMock()
    mock_client_2 = MagicMock()

    with patch("container_manager.docker.DockerClient", return_value=mock_client_1):
        c1 = cm._get_client("default")
        assert c1 is mock_client_1

    # bump generation, simulating a config reload
    cm._config_generation += 1

    with patch("container_manager.docker.DockerClient", return_value=mock_client_2):
        c2 = cm._get_client("default")
        assert c2 is mock_client_2
        assert c2 is not c1
