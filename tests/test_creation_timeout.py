from unittest.mock import MagicMock

import pytest

from docker_host_manager import CREATE_CLIENT_TIMEOUT, _run_with_creation_timeout


@pytest.mark.parametrize("fails", [False, True])
def test_creation_gets_longer_timeout_and_restores_control_plane_timeout(fails):
    client = MagicMock()
    client.api.timeout = 10
    container = object()

    def create(*args, **kwargs):
        assert client.api.timeout == CREATE_CLIENT_TIMEOUT == 60
        if fails:
            raise RuntimeError("ambiguous Docker failure")
        return container

    client.containers.run.side_effect = create
    if fails:
        with pytest.raises(RuntimeError, match="ambiguous Docker failure"):
            _run_with_creation_timeout(client, "test:latest", detach=True)
    else:
        assert _run_with_creation_timeout(client, "test:latest", detach=True) is container
    assert client.api.timeout == 10
    client.containers.run.assert_called_once_with("test:latest", detach=True)


def test_startup_timeout_survives_manager_and_is_safe_for_students():
    import sys

    from requests.exceptions import Timeout

    from docker_host_manager import DockerHostManager
    from exceptions import ContainerStartTimeout

    client = MagicMock()
    client.api.timeout = 10
    client.containers.run.side_effect = Timeout("private host and Docker details")
    manager = DockerHostManager()
    manager._clear_client = MagicMock()
    with pytest.raises(ContainerStartTimeout) as result:
        manager._invoke_client_op("remote", lambda: _run_with_creation_timeout(client, "test:latest"))
    assert client.api.timeout == 10
    manager._clear_client.assert_called_once_with("remote")
    utils = sys.modules["_cc_plugin.utils"]

    message = utils.sanitize_container_error(
        utils.ContainerStartTimeout(str(result.value))
    )  # tests load a separate exception class for utils
    assert "Container creation timed out" in message
    assert "a few minutes" in message
    assert "private host" not in message
    assert utils.error_body(message)["error_kind"] == "transient"
