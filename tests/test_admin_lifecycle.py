import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

routes_admin = sys.modules["_cc_plugin.views.routes_admin"]


def _jsonify(*args, **kwargs):
    return args[0] if args else kwargs


def test_purge_includes_logical_instances_without_physical_rows():
    first = SimpleNamespace(id="a" * 32)
    second = SimpleNamespace(id="b" * 32)
    model = MagicMock()
    model.query.all.return_value = [first, second]

    with (
        patch.object(routes_admin, "ContainerInstanceModel", model),
        patch.object(
            routes_admin,
            "cleanup_instance",
            side_effect=[{"success": "container cleaned"}, {"error": "host unavailable"}],
        ) as cleanup,
        patch.object(routes_admin, "_log_admin_action", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_purge_containers()

    assert status == 503
    assert result["purged"] == 1
    assert result["failures"] == [{"instance_id": "b" * 32, "error": "host unavailable"}]
    assert cleanup.call_count == 2
