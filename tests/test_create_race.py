import sys
from unittest.mock import patch, MagicMock

_helpers = sys.modules["_cc_plugin.views.helpers"]
_get_create_lock = _helpers._get_create_lock
_create_container_inner = _helpers._create_container_inner
create_container = _helpers.create_container

_MOD = "_cc_plugin.views.helpers"


def test_same_key_returns_same_lock():
    lock_a = _get_create_lock(1, 10, True)
    lock_b = _get_create_lock(1, 10, True)
    assert lock_a is lock_b


def test_different_keys_return_different_locks():
    lock_a = _get_create_lock(1, 10, True)
    lock_b = _get_create_lock(2, 10, True)
    assert lock_a is not lock_b

    lock_c = _get_create_lock(1, 10, False)
    assert lock_a is not lock_c


def test_db_commit_failure_triggers_rollback_and_kill():
    mock_cm = MagicMock()
    mock_created = MagicMock()
    mock_created.id = "abc123"
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
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, *a: {"max_containers_per_user": 10, "freshness_secret": ""}.get(
                k, a[0] if a else None
            ),
        ),
        patch(f"{_MOD}.db") as mock_db,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = mock_challenge
        mock_cim.query.filter_by.return_value.first.return_value = None
        mock_cim.query.filter_by.return_value.count.return_value = 0

        mock_db.session.commit.side_effect = Exception("db error")

        result = _create_container_inner(1, 10, 10, False)

        assert result[1] == 500
        assert "database error" in result[0]["error"]
        mock_db.session.rollback.assert_called_once()
        mock_cm.kill_container.assert_called_once_with("abc123", "default")


def test_lock_timeout_returns_429():
    busy_lock = MagicMock()
    busy_lock.acquire.return_value = False

    with patch(f"{_MOD}._get_create_lock", return_value=busy_lock):
        result = create_container(999, 999, 999, True)
        assert result[1] == 429
        assert "another container request" in result[0]["error"]
