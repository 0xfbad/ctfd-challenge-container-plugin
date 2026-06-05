import inspect
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import docker
import paramiko
import pytest

from docker_host_manager import (
    DockerHostManager,
    DEFAULT_CLIENT_TIMEOUT,
    PULL_CLIENT_TIMEOUT,
    THREADPOOL_SIZE,
)
from exceptions import ContainerUnavailableException


def make_host_manager(contexts=None):
    if contexts is None:
        contexts = {"default": "unix:///var/run/docker.sock"}

    hm = DockerHostManager()
    hm._context_configs = dict(contexts)
    hm._pub_hostnames = {name: "localhost" for name in contexts}
    return hm


def test_call_propagates_exception_unchanged():
    hm = make_host_manager()

    sentinel = docker.errors.DockerException("boom")

    def _fail():
        raise sentinel

    with pytest.raises(docker.errors.DockerException) as exc_info:
        hm._call("default", _fail)
    assert exc_info.value is sentinel


def test_per_context_pool_isolation():
    hm = make_host_manager({"a": "url_a", "b": "url_b"})
    pool_a = hm._get_threadpool("a")
    pool_b = hm._get_threadpool("b")
    assert pool_a is not pool_b


def test_pool_caching_same_context():
    hm = make_host_manager()
    first = hm._get_threadpool("default")
    second = hm._get_threadpool("default")
    assert first is second
    assert len(hm._threadpools) == 1


def test_pool_maxsize_uses_threadpool_size_constant():
    assert THREADPOOL_SIZE == 4

    captured = {}
    real_pool_cls = __import__("gevent.threadpool", fromlist=["ThreadPool"]).ThreadPool

    class TrackingPool(real_pool_cls):
        def __init__(self, maxsize=None):
            captured["maxsize"] = maxsize
            self.maxsize = maxsize
            super().__init__(maxsize=maxsize)

    hm = make_host_manager()
    with patch("docker_host_manager.gevent.threadpool.ThreadPool", TrackingPool):
        pool = hm._get_threadpool("default")
    assert captured["maxsize"] == 4
    assert pool.maxsize == 4


def test_pull_image_uses_fresh_client_with_pull_timeout():
    hm = make_host_manager({"default": "ssh://root@host"})

    cached_client = MagicMock(name="cached_client")
    pull_client = MagicMock(name="pull_client")
    pull_client.images.pull.return_value = None

    # prime the cache so we can prove pull_image bypasses it
    with patch("docker_host_manager.docker.DockerClient", return_value=cached_client):
        hm._get_client("default")
    # cache is thread-local: keys are (context_name, thread_ident)
    default_clients = [v for k, v in hm._clients.items() if k[0] == "default"]
    assert default_clients == [cached_client]

    with patch("docker_host_manager.docker.DockerClient", return_value=pull_client) as mock_ctor:
        result = hm.pull_image("default", "ubuntu:latest")

    assert result == "ok"
    mock_ctor.assert_called_once_with(base_url="ssh://root@host", timeout=PULL_CLIENT_TIMEOUT)
    pull_client.images.pull.assert_called_once_with("ubuntu:latest")
    pull_client.close.assert_called_once()
    # cached client untouched, no pulls dispatched through it
    cached_client.images.pull.assert_not_called()


def test_acquire_semaphore_default_timeout_is_ten():
    sig = inspect.signature(DockerHostManager.acquire_semaphore)
    assert sig.parameters["timeout"].default == 10


def test_get_client_passes_default_client_timeout():
    assert DEFAULT_CLIENT_TIMEOUT == 10

    hm = make_host_manager({"default": "ssh://root@host"})
    mock_client = MagicMock()

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client) as mock_ctor:
        hm._get_client("default")

    mock_ctor.assert_called_once_with(base_url="ssh://root@host", timeout=DEFAULT_CLIENT_TIMEOUT)


def test_load_contexts_skips_failed_ping_keeps_successful():
    hm = DockerHostManager()

    ok_client = MagicMock(name="ok_client")
    bad_client = MagicMock(name="bad_client")
    bad_client.ping.side_effect = docker.errors.DockerException("nope")

    def _client_factory(base_url, timeout=None):
        if "good" in base_url:
            return ok_client
        return bad_client

    contexts = [
        SimpleNamespace(context_name="good", hostname="good-host", pub_hostname="good.example"),
        SimpleNamespace(context_name="bad", hostname="bad-host", pub_hostname="bad.example"),
    ]

    # _resolve_endpoint without docker context metadata falls back to ssh url
    with (
        patch("docker_host_manager._scan_context_meta", return_value=None),
        patch("docker_host_manager.docker.DockerClient", side_effect=_client_factory),
    ):
        hm.load_contexts(contexts)

    assert "good" in hm._context_configs
    assert hm._context_configs["good"] == "ssh://root@good-host"
    assert "bad" not in hm._context_configs
    ok_client.close.assert_called()
    bad_client.close.assert_called()


def test_load_contexts_uses_default_client_timeout():
    hm = DockerHostManager()

    mock_client = MagicMock()
    contexts = [SimpleNamespace(context_name="x", hostname="x-host", pub_hostname="x.example")]

    with (
        patch("docker_host_manager._scan_context_meta", return_value=None),
        patch("docker_host_manager.docker.DockerClient", return_value=mock_client) as mock_ctor,
    ):
        hm.load_contexts(contexts)

    mock_ctor.assert_called_once_with(base_url="ssh://root@x-host", timeout=DEFAULT_CLIENT_TIMEOUT)


def test_clear_client_removes_and_closes():
    hm = make_host_manager()
    mock_client = MagicMock()

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")

    # cache is thread-local: keys are (context_name, thread_ident)
    assert any(k[0] == "default" for k in hm._clients)
    hm._clear_client("default")
    assert not any(k[0] == "default" for k in hm._clients)
    mock_client.close.assert_called_once()


def test_clear_client_swallows_close_errors():
    hm = make_host_manager()
    mock_client = MagicMock()
    mock_client.close.side_effect = RuntimeError("close failed")

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")

    # must not raise even though close blows up
    hm._clear_client("default")
    assert not any(k[0] == "default" for k in hm._clients)


def test_call_does_not_deadlock_when_fn_invokes_clear_client():
    # ping() is the natural case: on failure it calls _clear_client, and
    # _clear_client takes self._lock. since _call holds the pool but does
    # not hold self._lock, this should complete cleanly.
    hm = make_host_manager()
    mock_client = MagicMock()
    mock_client.ping.side_effect = docker.errors.DockerException("dead")

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
        assert any(k[0] == "default" for k in hm._clients)

        result = hm.ping("default")

    assert result is False
    assert not any(k[0] == "default" for k in hm._clients)
    mock_client.close.assert_called_once()


def test_invoke_client_op_passes_through_return_value():
    hm = make_host_manager()
    result = hm._invoke_client_op("default", lambda: 42)
    assert result == 42


def test_invoke_client_op_dockerexception_clears_client_and_reraises():
    hm = make_host_manager()
    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
    assert any(k[0] == "default" for k in hm._clients)

    sentinel = docker.errors.DockerException("nope")

    def _do():
        raise sentinel

    with pytest.raises(docker.errors.DockerException) as exc_info:
        hm._invoke_client_op("default", _do)
    assert exc_info.value is sentinel
    assert not any(k[0] == "default" for k in hm._clients)
    mock_client.close.assert_called_once()


def test_invoke_client_op_sshexception_clears_client_and_reraises():
    hm = make_host_manager()
    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")

    sentinel = paramiko.ssh_exception.SSHException("ssh dead")

    def _do():
        raise sentinel

    with pytest.raises(paramiko.ssh_exception.SSHException) as exc_info:
        hm._invoke_client_op("default", _do)
    assert exc_info.value is sentinel
    assert not any(k[0] == "default" for k in hm._clients)


def test_invoke_client_op_broad_exception_maps_to_container_unavailable():
    # the gevent.InvalidThreadUseError case: a non-docker, non-ssh exception
    # must be converted to ContainerUnavailableException and the cached client
    # dropped so the next call rebuilds from scratch
    hm = make_host_manager()
    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
    assert any(k[0] == "default" for k in hm._clients)

    class FakeInvalidThreadUseError(Exception):
        pass

    def _do():
        raise FakeInvalidThreadUseError("hub mismatch")

    with pytest.raises(ContainerUnavailableException) as exc_info:
        hm._invoke_client_op("default", _do)
    assert "default" in str(exc_info.value)
    assert not any(k[0] == "default" for k in hm._clients)
    mock_client.close.assert_called_once()


def test_invoke_client_op_preserves_method_specific_return():
    # method-specific exceptions (NotFound, KeyError) are meant to be caught
    # inside fn and translated to a return value. confirm a fn that handles its
    # own NotFound and returns False is passed through unchanged
    hm = make_host_manager()

    def _do():
        try:
            raise docker.errors.NotFound("missing")
        except docker.errors.NotFound:
            return False

    assert hm._invoke_client_op("default", _do) is False


def test_create_network_clears_client_on_transient_failure():
    # create_network was previously unpatched; confirm the refactor gives it
    # the broad coverage by simulating a non-docker transient error
    hm = make_host_manager()
    mock_client = MagicMock()

    class FakeInvalidThreadUseError(Exception):
        pass

    mock_client.networks.create.side_effect = FakeInvalidThreadUseError("hub mismatch")

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
        assert any(k[0] == "default" for k in hm._clients)

        with pytest.raises(ContainerUnavailableException):
            hm.create_network("default", "net-1")

    assert not any(k[0] == "default" for k in hm._clients)


def test_run_container_on_network_clears_client_on_transient_failure():
    # same coverage check for run_container_on_network (no-port branch)
    hm = make_host_manager()
    mock_client = MagicMock()

    class FakeInvalidThreadUseError(Exception):
        pass

    mock_client.containers.run.side_effect = FakeInvalidThreadUseError("hub mismatch")

    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
        with pytest.raises(ContainerUnavailableException):
            hm.run_container_on_network(
                "default",
                "alpine",
                "net-1",
                "container-1",
                None,
                {},
            )

    assert not any(k[0] == "default" for k in hm._clients)
