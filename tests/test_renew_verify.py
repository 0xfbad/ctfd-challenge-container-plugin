import sys
from unittest.mock import MagicMock, patch

_helpers = sys.modules["_cc_plugin.views.helpers"]
renew_container = _helpers.renew_container

_MOD = "_cc_plugin.views.helpers"


def _make_challenge():
    challenge = MagicMock()
    challenge.id = 1
    challenge.name = "test"
    challenge.max_renewals = 3
    challenge.expiration_seconds = 1800
    return challenge


def _make_running(stack_id=None):
    rc = MagicMock()
    rc.container_id = "abc123"
    rc.docker_context = "default"
    rc.user_id = 1
    rc.user.name = "user1"
    rc.team_id = None
    rc.team = None
    rc.expires = 9999
    rc.renewals_used = 0
    rc.stack_id = stack_id
    rc.instance_id = "a" * 32
    return rc


def test_renew_schedules_managed_cleanup_for_vanished_container():
    challenge = _make_challenge()
    rc = _make_running()

    mock_cm = MagicMock()
    mock_cm.is_container_running.return_value = False

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
        patch(f"{_MOD}.kill_container") as cleanup,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert "container not found" in result["error"]
    cleanup.assert_called_once_with(rc.container_id)
    mock_cm.is_container_running.assert_called_once_with("abc123", "default")


def test_renew_stack_uses_same_managed_cleanup_path():
    challenge = _make_challenge()
    rc = _make_running(stack_id="stack-xyz")
    mock_cm = MagicMock()
    mock_cm.is_container_running.return_value = False

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
        patch(f"{_MOD}.kill_container") as cleanup,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert "container not found" in result["error"]
    cleanup.assert_called_once_with(rc.container_id)


def test_renew_keeps_row_on_host_unavailable():
    """ContainerException, row stays, host-unavailable error returned"""
    from exceptions import ContainerException

    challenge = _make_challenge()
    rc = _make_running()

    mock_cm = MagicMock()
    mock_cm.is_container_running.side_effect = ContainerException("host down")

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert "temporarily unreachable" in result["error"]


def test_renew_proceeds_when_container_running():
    challenge = _make_challenge()
    rc = _make_running()

    mock_cm = MagicMock()
    mock_cm.is_container_running.return_value = True

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
        patch(f"{_MOD}.get_setting", side_effect=lambda k, *a: a[0] if a else None),
        patch(
            f"{_MOD}.InstanceCoordinator.renew",
            return_value=MagicMock(expires=11_000, renewals_used=1),
        ) as renew,
        patch(f"{_MOD}.build_connection_response", return_value={"status": "success"}),
        patch(f"{_MOD}.event_logger"),
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert result.get("success") == "container renewed"
    renew.assert_called_once()
