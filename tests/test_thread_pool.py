from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import docker
import pytest

from docker_host_manager import DockerHostManager, _confirm_removal_in_progress


def make_host_manager(contexts=None):
    if contexts is None:
        contexts = {"default": "unix:///var/run/docker.sock"}

    hm = DockerHostManager()
    hm._context_configs = dict(contexts)
    hm._pub_hostnames = {name: "localhost" for name in contexts}
    return hm


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

    with pytest.raises(Exception, match="no client"):
        hm._get_client("nonexistent")


def test_clear_client_closes_and_removes():
    hm = make_host_manager()

    mock_client = MagicMock()
    with patch("docker_host_manager.docker.DockerClient", return_value=mock_client):
        hm._get_client("default")
        # cache is thread-local: keys are (context_name, thread_ident)
        assert any(k[0] == "default" for k in hm._clients)

        hm._clear_client("default")
        assert not any(k[0] == "default" for k in hm._clients)
        mock_client.close.assert_called_once()


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


def test_confirm_removal_in_progress_waits_for_auto_remove():
    container = MagicMock()
    container.reload.side_effect = [None, docker.errors.NotFound()]
    error = SimpleNamespace(status_code=409, explanation="removal of container is already in progress")

    with patch("docker_host_manager.time.sleep") as sleep:
        assert _confirm_removal_in_progress(container, error) is True

    assert container.reload.call_count == 2
    sleep.assert_called_once_with(0.05)


def test_confirm_removal_in_progress_rejects_unrelated_api_error():
    container = MagicMock()
    error = SimpleNamespace(status_code=500, explanation="daemon unavailable")

    assert _confirm_removal_in_progress(container, error) is False
    container.reload.assert_not_called()


def test_count_resources_by_label_includes_network_only_orphans():
    manager = make_host_manager()
    client = MagicMock()
    client.containers.list.return_value = []
    client.networks.list.return_value = [MagicMock()]

    with patch.object(manager, "_get_client", return_value=client):
        assert manager.count_resources_by_label("default", "ctf.instance_id") == (0, 1)

    client.containers.list.assert_called_once_with(filters={"label": "ctf.instance_id"}, all=True)
    client.networks.list.assert_called_once_with(filters={"label": "ctf.instance_id"})
