import time
from unittest.mock import patch, MagicMock

from container_manager import ContainerManager, ContainerException


def make_manager():
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm.host_manager = MagicMock()
    cm.host_manager.has_contexts.return_value = True
    cm.orchestrator = MagicMock()
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
    c.stack_id = None
    c.is_entry = True
    return c


def test_kill_failure_skips_db_delete():
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)

    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model, patch("container_manager.db") as mock_db:
        mock_model.query.filter.return_value.all.return_value = [expired_container]

        cm.kill_container = MagicMock(side_effect=ContainerException("docker down"))

        cm.kill_expired_containers(mock_app)

        mock_db.session.delete.assert_not_called()
        mock_db.session.commit.assert_not_called()


def test_expires_zero_never_expired():
    cm = make_manager()

    never_expire = _make_container("abc", 0)

    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model, patch("container_manager.db") as mock_db:
        mock_model.query.filter.return_value.all.return_value = [never_expire]
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        cm.kill_container.assert_not_called()
        mock_db.session.delete.assert_not_called()


def test_successful_kill_deletes_db_row():
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)

    mock_app = MagicMock()

    with (
        patch("container_manager.ContainerInfoModel") as mock_model,
        patch("container_manager.ContainerHistoryModel") as mock_hist,
        patch("container_manager.db") as mock_db,
    ):
        mock_model.query.filter.return_value.all.return_value = [expired_container]
        mock_hist.query.filter_by.return_value.first.return_value = None
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        cm.kill_container.assert_called_once_with("abc", "default")
        mock_db.session.delete.assert_called_once_with(expired_container)
        mock_db.session.commit.assert_called_once()
