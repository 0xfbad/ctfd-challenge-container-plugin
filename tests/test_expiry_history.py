import time
from unittest.mock import patch, MagicMock

from container_manager import ContainerManager


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
    return c


def test_expiry_records_history():
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)
    mock_history = MagicMock()
    mock_history.stopped_at = None
    mock_history.reason = None

    mock_app = MagicMock()

    with (
        patch("container_manager.ContainerInfoModel") as mock_model,
        patch("container_manager.ContainerHistoryModel") as mock_hist,
        patch("container_manager.db") as mock_db,
    ):
        mock_model.query.all.return_value = [expired_container]
        mock_hist.query.filter_by.return_value.first.return_value = mock_history
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        assert mock_history.reason == "expired"
        assert mock_history.stopped_at is not None
        mock_db.session.delete.assert_called_once_with(expired_container)
        mock_db.session.commit.assert_called_once()


def test_expiry_preserves_solved_reason():
    """If the history reason is already 'solved', the expiry job should not overwrite it."""
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)
    mock_history = MagicMock()
    mock_history.stopped_at = None
    mock_history.reason = "solved"

    mock_app = MagicMock()

    with (
        patch("container_manager.ContainerInfoModel") as mock_model,
        patch("container_manager.ContainerHistoryModel") as mock_hist,
        patch("container_manager.db"),
    ):
        mock_model.query.all.return_value = [expired_container]
        mock_hist.query.filter_by.return_value.first.return_value = mock_history
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)

        assert mock_history.reason == "solved"
        assert mock_history.stopped_at is not None


def test_expiry_no_history_row():
    """Expiry should still work even if there's no history row."""
    cm = make_manager()
    now = int(time.time())

    expired_container = _make_container("abc", now - 100)

    mock_app = MagicMock()

    with (
        patch("container_manager.ContainerInfoModel") as mock_model,
        patch("container_manager.ContainerHistoryModel") as mock_hist,
        patch("container_manager.db"),
    ):
        mock_model.query.all.return_value = [expired_container]
        mock_hist.query.filter_by.return_value.first.return_value = None
        cm.kill_container = MagicMock()

        cm.kill_expired_containers(mock_app)
