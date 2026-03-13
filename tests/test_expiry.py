import time
import threading
from unittest.mock import patch, MagicMock

from container_manager import ContainerManager, ContainerException, _ThreadLocalClients


class SynchronousPool:
    def __init__(self, maxsize=4):
        self.size = maxsize

    def submit(self, fn, *args, **kwargs):
        result = fn(*args, **kwargs)

        class FakeFuture:
            def result(self):
                return result

        return FakeFuture()


def make_manager():
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm._context_configs = {"default": "__from_env__"}
    cm.weighted_contexts = ["default"]
    cm.context_index = 0
    cm._context_lock = threading.Lock()
    cm._config_generation = 0
    cm._pool = SynchronousPool()
    cm._semaphores = {}
    cm._thread_local = _ThreadLocalClients()
    return cm


def _make_container(container_id, expires, challenge_name="test"):
    c = MagicMock()
    c.container_id = container_id
    c.docker_context = "default"
    c.expires = expires
    c.challenge_id = 1
    c.challenge.name = challenge_name
    c.user_id = 1
    c.user.name = "user1"
    c.team_id = None
    c.team.name = None
    return c


def test_kill_failure_skips_db_delete():
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)

    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model, patch("container_manager.db") as mock_db:
        mock_model.query.all.return_value = [expired_container]

        # kill_container raises, simulating docker unreachable
        cm.kill_container = MagicMock(side_effect=ContainerException("docker down"))

        cm.kill_expired_containers(mock_app)

        # DB row should NOT have been deleted since kill failed
        mock_db.session.delete.assert_not_called()
        mock_db.session.commit.assert_not_called()


def test_expires_zero_never_expired():
    cm = make_manager()

    # expires=0 means "no expiration"
    never_expire = _make_container("abc", 0)

    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model, patch("container_manager.db") as mock_db:
        mock_model.query.all.return_value = [never_expire]
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        cm.kill_container.assert_not_called()
        mock_db.session.delete.assert_not_called()


def test_successful_kill_deletes_db_row():
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)

    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model, patch("container_manager.db") as mock_db:
        mock_model.query.all.return_value = [expired_container]
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        cm.kill_container.assert_called_once_with("abc", "default")
        mock_db.session.delete.assert_called_once_with(expired_container)
        mock_db.session.commit.assert_called_once()
