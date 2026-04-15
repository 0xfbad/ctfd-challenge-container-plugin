import sys
import time
from unittest.mock import patch, MagicMock

_helpers = sys.modules["_cc_plugin.views.helpers"]
record_history_stop = _helpers.record_history_stop
kill_container = _helpers.kill_container
_create_container_inner = _helpers._create_container_inner

_MOD = "_cc_plugin.views.helpers"


def test_record_history_stop_sets_fields():
    mock_row = MagicMock()
    mock_row.stopped_at = None
    mock_row.reason = None

    with patch(f"{_MOD}.ContainerHistoryModel") as mock_model:
        mock_model.query.filter_by.return_value.first.return_value = mock_row
        record_history_stop("abc123", "stopped")

        assert mock_row.reason == "stopped"
        assert mock_row.stopped_at is not None
        assert mock_row.stopped_at >= time.time() - 5


def test_record_history_stop_missing_row():
    with patch(f"{_MOD}.ContainerHistoryModel") as mock_model:
        mock_model.query.filter_by.return_value.first.return_value = None
        # should not raise
        record_history_stop("nonexistent", "stopped")


def test_kill_container_records_history():
    mock_container = MagicMock()
    mock_container.container_id = "abc123"
    mock_container.docker_context = "default"
    mock_container.challenge_id = 1
    mock_container.challenge.name = "test"
    mock_container.user_id = 1
    mock_container.user.name = "alice"
    mock_container.team_id = None
    mock_container.team.name = None
    mock_container.stack_id = None

    mock_cm = MagicMock()
    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    mock_history = MagicMock()
    mock_history.stopped_at = None
    mock_history.reason = None

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerInfoModel") as mock_info,
        patch(f"{_MOD}.ContainerHistoryModel") as mock_hist_model,
        patch(f"{_MOD}.db") as mock_db,
        patch(f"{_MOD}.log_container_event"),
    ):
        mock_info.query.filter_by.return_value.first.return_value = mock_container
        mock_hist_model.query.filter_by.return_value.first.return_value = mock_history

        result = kill_container("abc123")

        assert "success" in result
        assert mock_history.reason == "stopped"
        assert mock_history.stopped_at is not None
        mock_db.session.delete.assert_called_once_with(mock_container)


def test_create_container_inserts_history():
    mock_cm = MagicMock()
    mock_created = MagicMock()
    mock_created.id = "newcontainer123"
    mock_cm.create_container.return_value = (mock_created, "default")
    mock_cm.get_container_port.return_value = "8080"

    mock_challenge = MagicMock()
    mock_challenge.id = 1
    mock_challenge.image = "test:latest"
    mock_challenge.port = 80
    mock_challenge.command = ""
    mock_challenge.volumes = ""
    mock_challenge.max_memory_mb = None
    mock_challenge.max_cpu = None
    mock_challenge.docker_context = None
    mock_challenge.expiration_seconds = 600
    mock_challenge.ctype = "tcp"
    mock_challenge.ssh_username = None
    mock_challenge.ssh_password = None
    mock_challenge.services_json = None
    mock_challenge.network_json = None
    mock_challenge.cap_add = None

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
        patch(f"{_MOD}.ContainerHistoryModel") as mock_hist_model,
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, *a: {"max_containers_per_user": 10, "freshness_secret": ""}.get(
                k, a[0] if a else None
            ),
        ),
        patch(f"{_MOD}.db") as mock_db,
        patch(f"{_MOD}.get_hostname_for_context", return_value="localhost"),
        patch(f"{_MOD}.log_container_event"),
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = mock_challenge
        mock_cim.query.filter_by.return_value.first.return_value = None
        mock_cim.query.filter_by.return_value.count.return_value = 0

        mock_db.session.commit.side_effect = None

        _create_container_inner(1, 10, 10, False)

        # should have added both ContainerInfoModel and ContainerHistoryModel
        assert mock_db.session.add.call_count == 2
        # verify the history model was constructed with the right kwargs
        mock_hist_model.assert_called_once()
        call_kwargs = mock_hist_model.call_args[1]
        assert call_kwargs["container_id"] == "newcontainer123"
        assert call_kwargs["challenge_id"] == 1
        assert call_kwargs["user_id"] == 10
