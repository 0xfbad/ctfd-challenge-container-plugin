from utils import settings_to_dict, get_setting, _coerce, DEFAULTS


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
    assert _coerce("3.7", 0) == 3


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
        "expiration_check_interval",
        "thread_pool_size",
        "max_concurrent_creates",
        "freshness_secret",
        "post_solve_expiry_seconds",
        "default_expiration_seconds",
        "default_max_renewals",
    }
    assert set(DEFAULTS.keys()) == expected


def test_get_setting_returns_default_outside_app_context():
    result = get_setting("max_containers_per_user")
    assert result == 4


def test_get_setting_with_explicit_default():
    result = get_setting("nonexistent_key", 99)
    assert result == 99
