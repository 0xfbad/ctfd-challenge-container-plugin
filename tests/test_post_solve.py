import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from challenges import ContainerChallenge, _shorten_after_solve
from freshness import compute_token, render_flag

_MOD = "challenges"


def _request(submission: str) -> MagicMock:
    request = MagicMock()
    request.form = None
    request.get_json.return_value = {"submission": submission}
    return request


def _freshness_case():
    secret = "testsecret"
    challenge = SimpleNamespace(id=1, name="test_chal", function="static")
    user = SimpleNamespace(id=10, team=None, name="alice")
    submitted = render_flag("ctf{%TOKEN%}", compute_token(secret, challenge.id, user.id))
    flag = SimpleNamespace(content="ctf{%TOKEN%}", data=None)
    return secret, challenge, user, submitted, flag


def test_attempt_is_verification_only():
    secret, challenge, user, submitted, flag = _freshness_case()
    flags = MagicMock()
    flags.query.filter_by.return_value.all.return_value = [flag]

    with (
        patch(
            f"{_MOD}.get_setting",
            side_effect=lambda key, default=None: {
                "freshness_secret": secret,
                "freshness_token_length": 6,
            }.get(key, default),
        ),
        patch(f"{_MOD}.get_current_user", return_value=user),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch("CTFd.models.Flags", flags),
        patch(f"{_MOD}._shorten_after_solve") as shorten,
    ):
        assert ContainerChallenge.attempt(challenge, _request(submitted)) == (True, "correct")
        shorten.assert_not_called()


def test_solve_persists_before_container_side_effect():
    _secret, challenge, user, submitted, _flag = _freshness_case()
    calls: list[str] = []

    def base_solve(*_args, **_kwargs):
        calls.append("persist")

    def shorten(*_args, **_kwargs):
        calls.append("shorten")
        return 12

    with (
        patch("CTFd.plugins.challenges.BaseChallenge.solve", side_effect=base_solve),
        patch(f"{_MOD}._shorten_after_solve", side_effect=shorten),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch(f"{_MOD}.event_logger"),
    ):
        ContainerChallenge.solve(user, None, challenge, _request(submitted))

    assert calls == ["persist", "shorten"]


def test_failed_solve_insert_never_shortens():
    _secret, challenge, user, submitted, _flag = _freshness_case()
    with (
        patch("CTFd.plugins.challenges.BaseChallenge.solve", side_effect=RuntimeError("duplicate")),
        patch(f"{_MOD}._shorten_after_solve") as shorten,
    ):
        with pytest.raises(RuntimeError, match="duplicate"):
            ContainerChallenge.solve(user, None, challenge, _request(submitted))
        shorten.assert_not_called()


def test_durable_post_solve_uses_atomic_coordinator_only():
    container = MagicMock(
        instance_id="a" * 32,
        timestamp=int(time.time()) - 30,
        container_id="abc123",
    )
    update = SimpleNamespace(expires=int(time.time()) + 90)
    with (
        patch(f"{_MOD}.get_setting", return_value=90),
        patch(f"{_MOD}.ContainerInfoModel") as info,
        patch(f"{_MOD}.InstanceCoordinator.shorten_after_solve", return_value=update) as shorten,
    ):
        info.query.filter_by.return_value.first.return_value = container
        assert _shorten_after_solve(1, 10, False) is not None

    shorten.assert_called_once()


def test_solve_remains_successful_when_shortening_needs_reconciliation():
    _secret, challenge, user, submitted, _flag = _freshness_case()
    with (
        patch("CTFd.plugins.challenges.BaseChallenge.solve"),
        patch(f"{_MOD}._shorten_after_solve", side_effect=RuntimeError("database unavailable")),
        patch(f"{_MOD}.is_team_mode", return_value=False),
        patch(f"{_MOD}.event_logger") as events,
    ):
        ContainerChallenge.solve(user, None, challenge, _request(submitted))

    assert any(call.args[0] == "solve_reconcile_pending" for call in events.log_event.call_args_list)


def test_post_solve_disabled_when_zero():
    with patch(f"{_MOD}.get_setting", return_value=0), patch(f"{_MOD}.ContainerInfoModel") as info:
        assert _shorten_after_solve(1, 10, False) is None
        info.query.assert_not_called()
