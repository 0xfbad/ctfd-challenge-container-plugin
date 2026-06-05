import sys
from unittest.mock import MagicMock, patch

_helpers = sys.modules["_cc_plugin.views.helpers"]
requires_visible_challenge = _helpers.requires_visible_challenge

_MOD = "_cc_plugin.views.helpers"


def _make_challenge(state="visible", chal_id=1):
    challenge = MagicMock()
    challenge.id = chal_id
    challenge.state = state
    return challenge


def _call(chal_id_in_json=1, chal_state="visible", is_admin_val=False, kwargs=None):
    """Wrap a dummy handler and invoke it via the decorator."""
    sentinel = object()

    @requires_visible_challenge
    def handler(*args, **kwargs):
        return sentinel

    request_json = {"chal_id": chal_id_in_json} if chal_id_in_json is not None else None
    challenge = _make_challenge(state=chal_state, chal_id=1) if chal_state else None

    fake_g = MagicMock()
    # ensure stash starts empty so the decorator sets it explicitly
    del fake_g.challenge

    with (
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.is_admin", return_value=is_admin_val),
        patch(f"{_MOD}.g", fake_g),
        patch(f"{_MOD}.request") as mock_request,
    ):
        mock_request.json = request_json
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        result = handler(**(kwargs or {}))

    return result, sentinel, fake_g


def test_visible_challenge_passes_through():
    result, sentinel, fake_g = _call(chal_state="visible")
    assert result is sentinel
    assert fake_g.challenge.state == "visible"


def test_hidden_challenge_non_admin_returns_404():
    result, sentinel, _ = _call(chal_state="hidden", is_admin_val=False)
    assert result == ({"error": "challenge not found"}, 404)


def test_hidden_challenge_admin_passes_through():
    result, sentinel, _ = _call(chal_state="hidden", is_admin_val=True)
    assert result is sentinel


def test_locked_challenge_non_admin_returns_403():
    result, sentinel, _ = _call(chal_state="locked", is_admin_val=False)
    assert result == ({"error": "challenge locked"}, 403)


def test_locked_challenge_admin_passes_through():
    result, sentinel, _ = _call(chal_state="locked", is_admin_val=True)
    assert result is sentinel


def test_missing_challenge_returns_404():
    result, _, _ = _call(chal_state=None)
    assert result == ({"error": "challenge not found"}, 404)


def test_no_chal_id_returns_404():
    result, _, _ = _call(chal_id_in_json=None)
    assert result == ({"error": "challenge not found"}, 404)


def test_chal_id_in_kwargs_not_json():
    """url-path routes (like get_connect_type) pass chal_id via kwargs"""
    sentinel = object()

    @requires_visible_challenge
    def handler(challenge_id=None, **kwargs):
        return sentinel

    challenge = _make_challenge()
    fake_g = MagicMock()
    del fake_g.challenge

    with (
        patch(f"{_MOD}.ContainerChallengeModel") as mock_ccm,
        patch(f"{_MOD}.is_admin", return_value=False),
        patch(f"{_MOD}.g", fake_g),
        patch(f"{_MOD}.request") as mock_request,
    ):
        mock_request.json = None
        mock_ccm.query.filter_by.return_value.first.return_value = challenge
        result = handler(challenge_id=1)

    assert result is sentinel


def test_invalid_chal_id_returns_404():
    """non-integer chal_id should not crash, should 404"""
    result, _, _ = _call(chal_id_in_json="not-a-number")
    assert result == ({"error": "challenge not found"}, 404)


def test_decorator_stashes_challenge_on_g():
    _, _, fake_g = _call(chal_state="visible")
    assert fake_g.challenge is not None
    assert fake_g.challenge.state == "visible"
    assert fake_g.challenge.id == 1
