import time
from unittest.mock import patch, MagicMock
from types import SimpleNamespace

from challenges import ContainerChallenge
from freshness import compute_token, render_flag

_MOD = "challenges"


def _make_user(user_id, team=None, name="alice"):
    return SimpleNamespace(id=user_id, team=team, name=name)


def test_post_solve_shortens_expiry():
    secret = "testsecret"
    challenge_id = 1
    template = "ctf{%TOKEN%}"
    user = _make_user(10, name="alice")
    token = compute_token(secret, challenge_id, 10)
    submitted = render_flag(template, token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    mock_container = MagicMock()
    mock_container.container_id = "abc123"
    mock_container.expires = int(time.time()) + 3600

    mock_history = MagicMock()
    mock_history.reason = None

    with (
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, d=None: {"freshness_secret": secret, "post_solve_expiry_seconds": 90, "freshness_token_length": 6}.get(k, d),
        ),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
        patch(f"{_MOD}.ContainerInfoModel") as mock_info,
        patch(f"{_MOD}.ContainerHistoryModel") as mock_hist,
        patch(f"{_MOD}.db"),
    ):
        mock_info.query.filter_by.return_value.first.return_value = mock_container
        mock_hist.query.filter_by.return_value.first.return_value = mock_history

        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is True
        assert message == "correct"
        # container's expiry should have been shortened to ~90s from now
        assert mock_container.expires <= int(time.time()) + 91
        assert mock_container.expires >= int(time.time()) + 85
        assert mock_history.reason == "solved"


def test_post_solve_disabled_when_zero():
    secret = "testsecret"
    challenge_id = 1
    template = "ctf{%TOKEN%}"
    user = _make_user(10)
    token = compute_token(secret, challenge_id, 10)
    submitted = render_flag(template, token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    mock_container = MagicMock()
    mock_container.container_id = "abc123"
    original_expires = int(time.time()) + 3600
    mock_container.expires = original_expires

    with (
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, d=None: {"freshness_secret": secret, "post_solve_expiry_seconds": 0, "freshness_token_length": 6}.get(k, d),
        ),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
        patch(f"{_MOD}.ContainerInfoModel") as mock_info,
        patch(f"{_MOD}.ContainerHistoryModel"),
        patch(f"{_MOD}.db"),
    ):
        mock_info.query.filter_by.return_value.first.return_value = mock_container

        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is True
        # expiry should not have changed
        assert mock_container.expires == original_expires


def test_post_solve_no_container_running():
    secret = "testsecret"
    challenge_id = 1
    template = "ctf{%TOKEN%}"
    user = _make_user(10)
    token = compute_token(secret, challenge_id, 10)
    submitted = render_flag(template, token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    with (
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, d=None: {"freshness_secret": secret, "post_solve_expiry_seconds": 90, "freshness_token_length": 6}.get(k, d),
        ),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
        patch(f"{_MOD}.ContainerInfoModel") as mock_info,
        patch(f"{_MOD}.ContainerHistoryModel"),
        patch(f"{_MOD}.db"),
    ):
        mock_info.query.filter_by.return_value.first.return_value = None

        result, message = ContainerChallenge.attempt(challenge, mock_request)

        # should still return correct even with no container
        assert result is True
        assert message == "correct"


def test_post_solve_shortens_zero_expiration_container():
    secret = "testsecret"
    challenge_id = 1
    template = "ctf{%TOKEN%}"
    user = _make_user(10)
    token = compute_token(secret, challenge_id, 10)
    submitted = render_flag(template, token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    mock_container = MagicMock()
    mock_container.container_id = "abc123"
    mock_container.expires = 0
    mock_container.timestamp = int(time.time()) - 300

    with (
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda k, d=None: {"freshness_secret": secret, "post_solve_expiry_seconds": 90, "freshness_token_length": 6}.get(k, d),
        ),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
        patch(f"{_MOD}.ContainerInfoModel") as mock_info,
        patch(f"{_MOD}.ContainerHistoryModel"),
        patch(f"{_MOD}.db"),
    ):
        mock_info.query.filter_by.return_value.first.return_value = mock_container

        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is True
        # expires=0 containers now get shortened like any other
        assert mock_container.expires != 0
        assert mock_container.expires > int(time.time())
