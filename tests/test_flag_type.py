from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from flag_type import FreshnessFlag
from freshness import compute_token, render_flag

_MOD = "flag_type"
_DEFAULT_TOKEN_LEN = 6


def _setting_side_effect(secret):
    def _get(key, default=None):
        if key == "freshness_secret":
            return secret
        if key == "freshness_token_length":
            return _DEFAULT_TOKEN_LEN
        return default

    return _get


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
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect(secret)),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, expected) is True


def test_wrong_flag():
    secret = "testsecret"
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)
    user = _make_user(42)

    with (
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect(secret)),
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
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect(secret)),
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
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect(secret)),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, expected.upper()) is True


def test_missing_secret():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)

    with (
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect("")),
        patch(f"{_MOD}.get_current_user", return_value=_make_user(1)),
        patch(f"{_MOD}.is_team_mode", return_value=False),
    ):
        assert FreshnessFlag.compare(key_obj, "anything") is False


def test_missing_user():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)

    with (
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect("secret")),
        patch(f"{_MOD}.get_current_user", return_value=None),
    ):
        assert FreshnessFlag.compare(key_obj, "anything") is False


def test_team_mode_without_team():
    key_obj = _make_key_obj("ctf{%TOKEN%}", 1)
    user = _make_user(42, team=None)

    with (
        patch(f"{_MOD}.get_setting", side_effect=_setting_side_effect("secret")),
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

    def _settings(key, default=None):
        if key == "freshness_secret":
            return secret
        if key == "freshness_token_length":
            return _DEFAULT_TOKEN_LEN
        return default

    with (
        patch("challenges.get_setting", side_effect=_settings),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("challenges.Users") as mock_users,
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.event_logger") as mock_logger,
    ):
        mock_users.query.all.return_value = [user, other_user]
        mock_users.query.count.return_value = 2
        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is False
        assert "another participant" in message
        mock_logger.log_event.assert_called_once()
        call_args = mock_logger.log_event.call_args
        assert call_args[0][0] == "flag_sharing"
        assert "alice" in call_args[0][1]
        assert "bob" in call_args[0][1]


def test_anticheat_persists_flag_share():
    """attempt() must write a ContainerFlagShareModel row when a share is detected."""
    from challenges import ContainerChallenge
    from models import ContainerFlagShareModel

    secret = "testsecret"
    challenge_id = 7
    template = "ctf{persist_%TOKEN%}"

    user = _make_user(11, name="alice")
    other_user = _make_user(22, name="bob")
    other_token = compute_token(secret, challenge_id, 22)
    submitted = render_flag(template, other_token)

    challenge = SimpleNamespace(id=challenge_id, name="persist_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")
    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    def _settings(key, default=None):
        if key == "freshness_secret":
            return secret
        if key == "freshness_token_length":
            return _DEFAULT_TOKEN_LEN
        return default

    with (
        patch("challenges.get_setting", side_effect=_settings),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("challenges.Users") as mock_users,
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.event_logger"),
        patch("challenges.db") as mock_db,
        patch("challenges.ContainerFlagShareModel") as mock_share_cls,
    ):
        mock_users.query.all.return_value = [user, other_user]
        mock_users.query.count.return_value = 2

        result, _ = ContainerChallenge.attempt(challenge, mock_request)

        assert result is False
        mock_share_cls.assert_called_once()
        kwargs = mock_share_cls.call_args.kwargs
        assert kwargs["challenge_id"] == challenge_id
        assert kwargs["submitter_user_id"] == 11
        assert kwargs["submitter_team_id"] is None
        assert kwargs["owner_user_id"] == 22
        assert kwargs["owner_team_id"] is None
        assert kwargs["submitted_token"] == other_token
        assert kwargs["timestamp"] > 0
        mock_db.session.add.assert_called_once_with(mock_share_cls.return_value)
        mock_db.session.commit.assert_called_once()

    # silence unused-import warning since the symbol is the patch target
    assert ContainerFlagShareModel is not None


def test_anticheat_swallows_integrity_error_on_duplicate():
    """double-submit of the same flag share should not raise: the unique constraint dedups silently"""
    from challenges import ContainerChallenge
    from sqlalchemy.exc import IntegrityError

    secret = "testsecret"
    challenge_id = 13
    template = "ctf{dup_%TOKEN%}"

    user = _make_user(15, name="alice")
    other_user = _make_user(16, name="bob")
    other_token = compute_token(secret, challenge_id, 16)
    submitted = render_flag(template, other_token)

    challenge = SimpleNamespace(id=challenge_id, name="dup_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")
    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    def _settings(key, default=None):
        if key == "freshness_secret":
            return secret
        if key == "freshness_token_length":
            return _DEFAULT_TOKEN_LEN
        return default

    with (
        patch("challenges.get_setting", side_effect=_settings),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("challenges.Users") as mock_users,
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.event_logger"),
        patch("challenges.db") as mock_db,
        patch("challenges.ContainerFlagShareModel"),
    ):
        mock_users.query.all.return_value = [user, other_user]
        mock_users.query.count.return_value = 2
        mock_db.session.commit.side_effect = IntegrityError("dup", {}, Exception())

        # should not raise
        result, message = ContainerChallenge.attempt(challenge, mock_request)

        assert result is False
        assert "another participant" in message
        mock_db.session.rollback.assert_called_once()


def test_anticheat_persists_team_mode_share():
    """In team mode the owner is identified by team_id, not user_id."""
    from challenges import ContainerChallenge

    secret = "testsecret"
    challenge_id = 9
    template = "ctf{team_%TOKEN%}"

    submitter_team = _make_team(100, name="alpha")
    owner_team = _make_team(200, name="bravo")
    user = _make_user(11, team=submitter_team, name="alice")

    other_token = compute_token(secret, challenge_id, 200)
    submitted = render_flag(template, other_token)

    challenge = SimpleNamespace(id=challenge_id, name="team_chal")

    mock_request = MagicMock()
    mock_request.form = None
    mock_request.get_json.return_value = {"submission": submitted}

    mock_flag = SimpleNamespace(content=template, challenge_id=challenge_id, data=None, type="freshness")
    mock_flags = MagicMock()
    mock_flags.query.filter_by.return_value.all.return_value = [mock_flag]

    def _settings(key, default=None):
        if key == "freshness_secret":
            return secret
        if key == "freshness_token_length":
            return _DEFAULT_TOKEN_LEN
        return default

    with (
        patch("challenges.get_setting", side_effect=_settings),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=True),
        patch("challenges.Teams") as mock_teams,
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.event_logger"),
        patch("challenges.db"),
        patch("challenges.ContainerFlagShareModel") as mock_share_cls,
    ):
        mock_teams.query.all.return_value = [submitter_team, owner_team]
        mock_teams.query.count.return_value = 2

        result, _ = ContainerChallenge.attempt(challenge, mock_request)

        assert result is False
        kwargs = mock_share_cls.call_args.kwargs
        assert kwargs["submitter_user_id"] == 11
        assert kwargs["submitter_team_id"] == 100
        assert kwargs["owner_user_id"] is None
        assert kwargs["owner_team_id"] == 200


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

    def _settings(key, default=None):
        return {
            "freshness_secret": secret,
            "post_solve_expiry_seconds": 0,
            "freshness_token_length": _DEFAULT_TOKEN_LEN,
        }.get(key, default)

    with (
        patch("challenges.get_setting", side_effect=_settings),
        patch("challenges.get_current_user", return_value=user),
        patch("challenges.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", mock_flags),
        patch("challenges.ContainerInfoModel"),
        patch("challenges.ContainerHistoryModel"),
        patch("challenges.db"),
    ):
        result, message = ContainerChallenge.attempt(challenge, mock_request)
        assert result is True
        assert message == "correct"
