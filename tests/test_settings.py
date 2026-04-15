import sys
from unittest.mock import patch, MagicMock
from utils import get_setting, set_setting, _coerce, DEFAULTS

_utils_mod = sys.modules["_cc_plugin.utils"]
_flask_mod = sys.modules["flask"]
_ctfd_models_mod = sys.modules["CTFd.models"]


class FakeSettingsRow:
    def __init__(self, key, value):
        self.key = key
        self.value = value


def _with_mock_model():
    """replace ContainerSettingsModel where it's used (utils module)"""
    mock = MagicMock()
    return patch.object(_utils_mod, "ContainerSettingsModel", mock), mock


def test_get_setting_from_db():
    ctx, mock_model = _with_mock_model()
    with ctx:
        mock_model.query.filter_by.return_value.first.return_value = FakeSettingsRow("max_containers_per_user", "8")

        mock_app = MagicMock()
        mock_app.__bool__ = lambda self: True

        with patch.object(_flask_mod, "current_app", mock_app):
            result = get_setting("max_containers_per_user")
            assert result == 8


def test_get_setting_missing_row_returns_default():
    ctx, mock_model = _with_mock_model()
    with ctx:
        mock_model.query.filter_by.return_value.first.return_value = None

        mock_app = MagicMock()
        mock_app.__bool__ = lambda self: True

        with patch.object(_flask_mod, "current_app", mock_app):
            result = get_setting("max_containers_per_user")
            assert result == DEFAULTS["max_containers_per_user"]


def test_get_setting_db_exception_propagates():
    ctx, mock_model = _with_mock_model()
    with ctx:
        mock_model.query.filter_by.side_effect = Exception("db down")

        mock_app = MagicMock()
        mock_app.__bool__ = lambda self: True

        with patch.object(_flask_mod, "current_app", mock_app):
            import pytest

            with pytest.raises(Exception, match="db down"):
                get_setting("thread_pool_size")


def test_set_setting_updates_existing():
    ctx, mock_model = _with_mock_model()
    with ctx:
        existing_row = FakeSettingsRow("thread_pool_size", "4")
        mock_model.query.filter_by.return_value.first.return_value = existing_row

        mock_db = MagicMock()

        with patch.object(_ctfd_models_mod, "db", mock_db):
            set_setting("thread_pool_size", 8)
            assert existing_row.value == "8"
            mock_db.session.commit.assert_called_once()


def test_set_setting_creates_new():
    ctx, mock_model = _with_mock_model()
    with ctx:
        mock_model.query.filter_by.return_value.first.return_value = None

        mock_db = MagicMock()

        with patch.object(_ctfd_models_mod, "db", mock_db):
            set_setting("new_key", "new_value")
            mock_model.assert_called_once_with(key="new_key", value="new_value")
            mock_db.session.add.assert_called_once()
            mock_db.session.commit.assert_called_once()


def test_coerce_int_from_float_string():
    assert _coerce("3.9", 0) == 3


def test_coerce_preserves_string_type():
    assert _coerce("anything", "default") == "anything"


def test_coerce_bool_yes():
    assert _coerce("yes", False) is True


def test_coerce_bool_no():
    assert _coerce("no", True) is False


def test_get_setting_explicit_default_overrides_defaults_dict():
    with patch.object(_flask_mod, "current_app", None):
        result = get_setting("max_containers_per_user", 99)
        assert result == 99
