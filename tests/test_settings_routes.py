import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

routes_admin = sys.modules["_cc_plugin.views.routes_admin"]


def _jsonify(**kwargs):
    return kwargs


def _request(payload):
    request = MagicMock()
    request.is_json = True
    request.get_json.return_value = payload
    return request


def test_settings_update_validates_then_commits_patch_once():
    first = SimpleNamespace(value="4")
    second = SimpleNamespace(value="60")
    model = MagicMock()
    model.query.filter_by.side_effect = [
        MagicMock(first=MagicMock(return_value=first)),
        MagicMock(first=MagicMock(return_value=second)),
    ]
    db = MagicMock()
    manager = MagicMock()
    app = SimpleNamespace(container_manager=manager)

    with (
        patch.object(routes_admin, "request", _request({"max_containers_per_user": 8, "rate_limit_interval": 30})),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", app),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_update_settings()

    assert result["success"] == "settings updated"
    assert first.value == "8"
    assert second.value == "30"
    db.session.commit.assert_called_once_with()
    db.session.rollback.assert_not_called()


def test_settings_update_reports_disruptive_fields():
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = SimpleNamespace(value="old")
    db = MagicMock()
    app = SimpleNamespace(container_manager=MagicMock())

    with (
        patch.object(
            routes_admin,
            "request",
            _request({"max_concurrent_creates": 3, "freshness_token_length": 8}),
        ),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", app),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_update_settings()

    assert result["disruptive"] == ["freshness_token_length"]


def test_invalid_field_rejects_entire_settings_patch_before_database_work():
    model = MagicMock()
    db = MagicMock()

    with (
        patch.object(
            routes_admin,
            "request",
            _request({"max_containers_per_user": 8, "max_concurrent_creates": 0}),
        ),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_update_settings()

    assert status == 400
    assert "at least 1" in result["error"]
    model.query.filter_by.assert_not_called()
    db.session.commit.assert_not_called()


def test_settings_database_failure_rolls_back_complete_patch():
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = SimpleNamespace(value="4")
    db = MagicMock()
    db.session.commit.side_effect = RuntimeError("database unavailable")

    with (
        patch.object(routes_admin, "request", _request({"max_containers_per_user": 8})),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_update_settings()

    assert status == 500
    assert result["error"] == "failed to update settings"
    db.session.rollback.assert_called_once_with()


def test_settings_non_object_json_is_rejected():
    with (
        patch.object(routes_admin, "request", _request(["not", "an", "object"])),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_update_settings()

    assert status == 400
    assert "JSON object" in result["error"]


def test_general_settings_route_rejects_direct_secret_changes():
    with (
        patch.object(routes_admin, "request", _request({"freshness_secret": "do-not-accept"})),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_update_settings()

    assert status == 400
    assert "freshness secret controls" in result["error"]


def test_freshness_secret_can_be_disabled_without_exposing_value():
    row = SimpleNamespace(value="existing")
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = row
    db = MagicMock()

    with (
        patch.object(routes_admin, "request", _request({"action": "disable"})),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "_log_admin_action", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_update_freshness_secret()

    assert result["configured"] is False
    assert row.value == ""
    db.session.commit.assert_called_once_with()


def test_freshness_secret_regeneration_is_server_side():
    row = SimpleNamespace(value="existing")
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = row
    db = MagicMock()

    with (
        patch.object(routes_admin, "request", _request({"action": "regenerate"})),
        patch.object(routes_admin, "ContainerSettingsModel", model),
        patch.object(routes_admin, "generate_secret", return_value="new-secret"),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "_log_admin_action", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_update_freshness_secret()

    assert result["configured"] is True
    assert row.value == "new-secret"


def test_settings_get_masks_freshness_secret():
    def setting(key):
        return "server-secret" if key == "freshness_secret" else routes_admin.DEFAULTS[key]

    with (
        patch.object(routes_admin, "get_setting", side_effect=setting),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_get_settings()

    freshness = result["settings"]["freshness_secret"]
    assert freshness["value"] == ""
    assert freshness["configured"] is True
