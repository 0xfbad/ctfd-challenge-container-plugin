import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from utils import (
    DEFAULTS,
    ValidationError,
    _coerce,
    _increment_rate_limit,
    get_setting,
    parse_duration_seconds,
    parse_json_object,
    parse_strict_int,
    parse_timezone,
    ratelimit_per_user,
    settings_to_dict,
    validate_settings_patch,
)

_flask_mod = sys.modules["flask"]


class FakeSetting:
    def __init__(self, key, value):
        self.key = key
        self.value = value


def test_basic_conversion():
    result = settings_to_dict([FakeSetting("docker_host", "localhost"), FakeSetting("max_containers", "4")])
    assert result == {"docker_host": "localhost", "max_containers": "4"}


def test_empty_query():
    assert settings_to_dict([]) == {}


def test_single_setting():
    result = settings_to_dict([FakeSetting("key", "val")])
    assert result == {"key": "val"}


def test_none_value():
    result = settings_to_dict([FakeSetting("key", None)])
    assert result == {"key": None}


def test_coerce_int():
    assert _coerce("42", 0) == 42


def test_coerce_int_rejects_fractional_value():
    with pytest.raises(ValueError):
        _coerce("3.7", 0)


def test_coerce_float():
    assert _coerce("3.14", 0.0) == 3.14


def test_coerce_bool():
    assert _coerce("true", False) is True
    assert _coerce("false", True) is False
    assert _coerce("1", False) is True


def test_coerce_string():
    assert _coerce("hello", "default") == "hello"


def test_coerce_none_default():
    assert _coerce("hello", None) == "hello"


def test_defaults_dict_has_expected_keys():
    expected = {
        "max_containers_per_user",
        "rate_limit_requests",
        "rate_limit_interval",
        "mutation_rate_limit_requests",
        "mutation_rate_limit_interval",
        "expiration_check_interval",
        "max_concurrent_creates",
        "freshness_secret",
        "freshness_token_length",
        "post_solve_expiry_seconds",
        "default_expiration_seconds",
        "default_max_renewals",
        "default_max_memory_mb",
        "default_max_cpu_millicores",
    }
    assert set(DEFAULTS.keys()) == expected


def test_get_setting_returns_default_outside_app_context():
    with patch.object(_flask_mod, "current_app", None):
        result = get_setting("max_containers_per_user")
        assert result == 4


def test_get_setting_with_explicit_default():
    with patch.object(_flask_mod, "current_app", None):
        result = get_setting("nonexistent_key", 99)
        assert result == 99


@pytest.mark.parametrize("value", [True, 3.0, "3", None])
def test_parse_strict_int_rejects_non_json_integers(value):
    with pytest.raises(ValidationError, match="must be an integer"):
        parse_strict_int(value, "count")


def test_parse_strict_int_checks_range():
    assert parse_strict_int(4, "count", minimum=1, maximum=4) == 4
    with pytest.raises(ValidationError, match="at least 1"):
        parse_strict_int(0, "count", minimum=1)
    with pytest.raises(ValidationError, match="at most 4"):
        parse_strict_int(5, "count", maximum=4)


def test_parse_duration_is_integer_seconds_with_bounds():
    assert parse_duration_seconds(0, "duration", minimum=0, maximum=60) == 0
    with pytest.raises(ValidationError):
        parse_duration_seconds(-1, "duration", minimum=0)


def test_parse_timezone_accepts_iana_and_rejects_invalid():
    assert parse_timezone("UTC").key == "UTC"
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        parse_timezone("not/a-zone")


def test_parse_json_object_rejects_duplicate_keys_and_non_objects():
    with pytest.raises(ValidationError, match="duplicate JSON key"):
        parse_json_object('{"a": 1, "a": 2}', "config")
    with pytest.raises(ValidationError, match="JSON object"):
        parse_json_object("[]", "config")


def test_parse_json_object_limits_size_depth_and_item_count():
    with pytest.raises(ValidationError, match="bytes"):
        parse_json_object('{"value": "long"}', "config", max_bytes=5)
    with pytest.raises(ValidationError, match="nesting"):
        parse_json_object('{"a": {"b": {"c": 1}}}', "config", max_depth=2)
    with pytest.raises(ValidationError, match="more than 1 items"):
        parse_json_object('{"a": 1, "b": 2}', "config", max_items=1)


def test_parse_json_object_rejects_non_finite_numbers():
    with pytest.raises(ValidationError, match="invalid JSON number"):
        parse_json_object('{"value": NaN}', "config")


def test_validate_settings_patch_normalizes_complete_valid_patch():
    result = validate_settings_patch(
        {
            "max_containers_per_user": 8,
            "post_solve_expiry_seconds": 0,
            "freshness_secret": "",
        }
    )
    assert result == {
        "max_containers_per_user": 8,
        "post_solve_expiry_seconds": 0,
        "freshness_secret": "",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "JSON object"),
        ({"unknown": 1}, "unknown setting"),
        ({"max_concurrent_creates": 0}, "at least 1"),
        ({"max_concurrent_creates": 33}, "at most 32"),
        ({"rate_limit_requests": True}, "must be an integer"),
        ({"default_expiration_seconds": 0}, "at least 1"),
        ({"freshness_token_length": 17}, "at most 16"),
    ],
)
def test_validate_settings_patch_rejects_invalid_patch(payload, message):
    with pytest.raises(ValidationError, match=message):
        validate_settings_patch(payload)


def test_rate_limit_resolves_default_policy_per_request_and_keys_by_policy():
    class FakeCache:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, timeout):
            self.values[key] = value

    cache = FakeCache()
    cache_module = types.ModuleType("CTFd.cache")
    cache_module.cache = cache
    user_module = sys.modules["CTFd.utils.user"]
    policy = {"rate_limit_requests": 45, "rate_limit_interval": 60}
    fake_request = SimpleNamespace(method="GET", endpoint="containers.status")

    @ratelimit_per_user(method="GET", limit=45, interval=60)
    def endpoint():
        return "ok"

    with (
        patch.dict(sys.modules, {"CTFd.cache": cache_module}),
        patch.object(user_module, "get_current_user", return_value=SimpleNamespace(id=7)),
        patch("utils.request", fake_request),
        patch("utils.get_setting", side_effect=lambda key, default=None: policy.get(key, default)),
    ):
        assert endpoint() == "ok"
        policy.update(rate_limit_requests=20, rate_limit_interval=30)
        assert endpoint() == "ok"

    assert any(key.endswith(":45:60") for key in cache.values)
    assert any(key.endswith(":20:30") for key in cache.values)


def test_redis_rate_limit_uses_atomic_counter_with_first_hit_expiry():
    client = MagicMock()
    client.eval.return_value = 2
    config = _flask_mod.current_app.config

    with (
        patch.object(config, "get", side_effect=lambda key: "redis://cache/0" if key == "CACHE_REDIS_URL" else None),
        patch("utils._rate_limit_redis_client", return_value=client),
    ):
        assert _increment_rate_limit("rl_user:u7:endpoint:10:60", 60) == 2

    script, key_count, key, interval = client.eval.call_args.args
    assert "INCR" in script and "EXPIRE" in script
    assert (key_count, key, interval) == (1, "challenge-containers:rl_user:u7:endpoint:10:60", 60)
