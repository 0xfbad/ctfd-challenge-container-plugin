import sys
from unittest.mock import patch, MagicMock

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
    return rc


def test_renew_reaps_vanished_row():
    """is_container_running returns False, row deleted, error returned"""
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
        patch(f"{_MOD}.db") as mock_db,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert "container not found" in result["error"]
    mock_db.session.delete.assert_called_once_with(rc)
    mock_db.session.commit.assert_called_once()
    mock_cm.is_container_running.assert_called_once_with("abc123", "default")


def test_renew_reaps_all_stack_siblings():
    """vanished container with stack_id, all sibling rows are deleted"""
    challenge = _make_challenge()
    rc = _make_running(stack_id="stack-xyz")
    sibling = MagicMock()

    mock_cm = MagicMock()
    mock_cm.is_container_running.return_value = False

    mock_app = MagicMock()
    mock_app.container_manager = mock_cm

    with (
        patch(f"{_MOD}.current_app", mock_app),
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.ContainerInfoModel") as mock_cim,
        patch(f"{_MOD}.db") as mock_db,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        # first .filter_by lookup returns rc, second (stack lookup) returns siblings
        mock_cim.query.filter_by.return_value.first.return_value = rc
        mock_cim.query.filter_by.return_value.all.return_value = [rc, sibling]

        result = renew_container(1, 10, False)

    assert "container not found" in result["error"]
    assert mock_db.session.delete.call_count == 2
    mock_db.session.commit.assert_called_once()


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
        patch(f"{_MOD}.db") as mock_db,
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert "temporarily unreachable" in result["error"]
    mock_db.session.delete.assert_not_called()
    mock_db.session.commit.assert_not_called()


def test_renew_proceeds_when_container_running():
    """is_container_running True, renewal flow runs, db.commit called by renewal logic"""
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
        patch(f"{_MOD}.db") as mock_db,
        patch(f"{_MOD}.get_setting", side_effect=lambda k, *a: a[0] if a else None),
        patch(f"{_MOD}.build_connection_response", return_value={"status": "success"}),
        patch(f"{_MOD}.event_logger"),
    ):
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        mock_cim.query.filter_by.return_value.first.return_value = rc

        result = renew_container(1, 10, False)

    assert result.get("success") == "container renewed"
    mock_db.session.delete.assert_not_called()
    mock_db.session.commit.assert_called_once()
