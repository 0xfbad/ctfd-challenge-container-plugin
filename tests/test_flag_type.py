from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from flag_type import FreshnessFlag
from freshness import compute_token, render_flag

_MOD = "flag_type"


def _make_key_obj(content, challenge_id, data=None):
    return SimpleNamespace(content=content, challenge_id=challenge_id, data=data)


def _make_user(user_id, team=None, name="alice"):
    return SimpleNamespace(id=user_id, team=team, name=name)


def _make_team(team_id, name="team1"):
    return SimpleNamespace(id=team_id, name=name)


def test_correct_flag():
    secret = "testsecret"
    challenge_id = 1
    user = _make_user(42)
    token = compute_token(secret, challenge_id, 42)
    expected = render_flag("ctf{%TOKEN%}", token)

    key_obj = _make_key_obj("ctf{%TOKEN%}", challenge_id)

    with (
        patch(f"{_MOD}.get_setting", return_value=secret),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, expected) is True


def test_wrong_flag():
    secret = "testsecret"
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)
    user = _make_user(42)

    with (
        patch(f"{_MOD}.get_setting", return_value=secret),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, "ctf{wrong}") is False


def test_team_mode():
    secret = "testsecret"
    challenge_id = 5
    team = _make_team(99)
    user = _make_user(42, team=team)
    token = compute_token(secret, challenge_id, 99)
    expected = render_flag("flag{%TOKEN%}", token)

    key_obj = _make_key_obj("flag{%TOKEN%}", challenge_id)

    with (
        patch(f"{_MOD}.get_setting", return_value=secret),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=True),
    ):
        assert FreshnessFlag.compare(key_obj, expected) is True


def test_case_insensitive():
    secret = "testsecret"
    challenge_id = 1
    user = _make_user(42)
    token = compute_token(secret, challenge_id, 42)
    expected = render_flag("CTF{%TOKEN%}", token)

    key_obj = _make_key_obj("CTF{%TOKEN%}", challenge_id, data="case_insensitive")

    with (
        patch(f"{_MOD}.get_setting", return_value=secret),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, expected.upper()) is True


def test_missing_secret():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)

    with (
        patch(f"{_MOD}.get_setting", return_value=""),
        patch(f"{_MOD}.get_current_user", return_value=_make_user(1)),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, "anything") is False


def test_missing_user():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)

    with (
        patch(f"{_MOD}.get_setting", return_value="secret"),
        patch(f"{_MOD}.get_current_user", return_value=None),
    ):
        assert FreshnessFlag.compare(key_obj, "anything") is False


def test_team_mode_without_team():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)
    user = _make_user(42, team=None)

    with (
        patch(f"{_MOD}.get_setting", return_value="secret"),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=True),
    ):
        assert FreshnessFlag.compare(key_obj, "anything") is False


def test_anticheat_detection():
    """Test that attempt() detects flag sharing via the challenge class."""
    from challenges import ContainerChallenge
    from freshness import compute_token, render_flag

    secret = "testsecret"
    challenge_id = 1
    template = "ctf{test_%TOKEN%}"

    user = _make_user(10, name="alice")
    other_user = _make_user(20, name="bob")
    other_token = compute_token(secret, challenge_id, 20)
    submitted = render_flag(template, other_token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(
        content=template,
        challenge_id=challenge_id,
        data=None,
        type="freshness",
    )

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    with (
        patch("challenges.get_setting", return_value=secret),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("challenges.Users") as mock_users,
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.event_logger") as mock_logger,
    ):
        mock_users.query.all.return_value = [user, other_user]
        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is False
        assert "another participant" in message
        mock_logger.log_event.assert_called_once()
        call_args = mock_logger.log_event.call_args
        assert call_args[0][0] == "flag_sharing"
        assert "alice" in call_args[0][1]
        assert "bob" in call_args[0][1]


def test_anticheat_correct_flag_passes():
    """Submitting your own correct flag should pass."""
    from challenges import ContainerChallenge

    secret = "testsecret"
    challenge_id = 1
    template = "ctf{test_%TOKEN%}"

    user = _make_user(10, name="alice")
    own_token = compute_token(secret, challenge_id, 10)
    submitted = render_flag(template, own_token)

    challenge = SimpleNamespace(id=challenge_id, name="test_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(
        content=template,
        challenge_id=challenge_id,
        data=None,
        type="freshness",
    )

    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    with (
        patch("challenges.get_setting", return_value=secret),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
    ):
        result, message = ContainerChallenge.attempt(challenge, mock_request)
        assert result is True
        assert message == "correct"
