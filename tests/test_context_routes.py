import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

routes_admin = sys.modules["_cc_plugin.views.routes_admin"]


def _jsonify(**kwargs):
    return kwargs


def _request(payload=None, *, force="false"):
    request = MagicMock()
    request.is_json = True
    request.get_json.return_value = payload
    request.args.get.return_value = force
    return request


def test_add_context_uses_single_state_field():
    db = MagicMock()
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = None
    created = SimpleNamespace(id=5)
    model.return_value = created
    manager = MagicMock()
    payload = {
        "context_name": "worker-1",
        "pub_hostname": "worker-1.example.test",
        "weight": 2,
        "state": "disabled",
    }

    with (
        patch.object(routes_admin, "request", _request(payload)),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", SimpleNamespace(container_manager=manager)),
        patch.object(routes_admin, "event_logger", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_api_add_context()

    assert result["success"] == "context added"
    assert model.call_args.kwargs["state"] == "disabled"
    assert "enabled" not in model.call_args.kwargs
    db.session.commit.assert_called_once_with()
    manager.load_docker_contexts.assert_called_once_with()


def test_update_context_changes_state_atomically():
    context = SimpleNamespace(state="active", hostname=None, pub_hostname="host", weight=1, context_name="worker-1")
    model = MagicMock()
    model.query.get.return_value = context
    db = MagicMock()
    manager = MagicMock()
    payload = {"state": "draining", "weight": 3}

    with (
        patch.object(routes_admin, "request", _request(payload)),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", SimpleNamespace(container_manager=manager)),
        patch.object(routes_admin, "event_logger", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_api_update_context(1)

    assert result["success"] == "context updated"
    assert context.state == "draining"
    assert context.weight == 3
    db.session.commit.assert_called_once()


def test_delete_context_with_references_requires_force_and_keeps_record():
    context = SimpleNamespace(id=1, context_name="worker-1", hostname=None, state="active")
    model = MagicMock()
    model.query.get.return_value = context
    db = MagicMock()

    with (
        patch.object(routes_admin, "request", _request(force="false")),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "_context_reference_counts", return_value=(2, 1)),
        patch.object(
            routes_admin.ContainerChallengeModel,
            "query",
            MagicMock(**{"filter_by.return_value.count.return_value": 0}),
            create=True,
        ),
        patch.object(routes_admin, "_context_is_reachable", return_value=True),
        patch.object(routes_admin, "_context_docker_resource_counts", return_value=(0, 0)),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_api_delete_context(1)

    assert status == 409
    assert result["physical_references"] == 2
    db.session.delete.assert_not_called()
    db.session.commit.assert_not_called()


def test_force_delete_with_references_retires_tombstone():
    context = SimpleNamespace(id=1, context_name="worker-1", hostname=None, state="active")
    model = MagicMock()
    model.query.get.return_value = context
    db = MagicMock()
    manager = MagicMock()

    with (
        patch.object(routes_admin, "request", _request(force="true")),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "_context_reference_counts", return_value=(2, 1)),
        patch.object(
            routes_admin.ContainerChallengeModel,
            "query",
            MagicMock(**{"filter_by.return_value.count.return_value": 0}),
            create=True,
        ),
        patch.object(routes_admin, "_context_is_reachable", return_value=False),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", SimpleNamespace(container_manager=manager)),
        patch.object(routes_admin, "event_logger", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_api_delete_context(1)

    assert result["state"] == "retired_orphaned"
    assert context.state == "retired_orphaned"
    db.session.delete.assert_not_called()
    db.session.commit.assert_called_once_with()
    manager.load_docker_contexts.assert_called_once_with()


def test_reachable_unreferenced_context_can_be_hard_deleted():
    context = SimpleNamespace(id=1, context_name="worker-1", hostname=None, state="disabled")
    model = MagicMock()
    model.query.get.return_value = context
    db = MagicMock()
    manager = MagicMock()

    with (
        patch.object(routes_admin, "request", _request(force="false")),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "_context_reference_counts", return_value=(0, 0)),
        patch.object(
            routes_admin.ContainerChallengeModel,
            "query",
            MagicMock(**{"filter_by.return_value.count.return_value": 0}),
            create=True,
        ),
        patch.object(routes_admin, "_context_is_reachable", return_value=True),
        patch.object(routes_admin, "_context_docker_resource_counts", return_value=(0, 0)),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "current_app", SimpleNamespace(container_manager=manager)),
        patch.object(routes_admin, "event_logger", MagicMock()),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result = routes_admin.route_api_delete_context(1)

    assert result["state"] == "deleted"
    db.session.delete.assert_called_once_with(context)
    db.session.commit.assert_called_once_with()


def test_endpoint_change_rejects_untracked_docker_resources():
    context = SimpleNamespace(
        state="active", hostname="old-host", pub_hostname="public", weight=1, context_name="worker-1"
    )
    model = MagicMock()
    model.query.get.return_value = context

    with (
        patch.object(routes_admin, "request", _request({"hostname": "new-host"})),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "_context_reference_counts", return_value=(0, 0)),
        patch.object(routes_admin, "_context_docker_resource_counts", return_value=(1, 1)),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_api_update_context(1)

    assert status == 409
    assert result["docker_containers"] == 1
    assert result["docker_networks"] == 1


def test_hard_delete_rejects_untracked_docker_resources():
    context = SimpleNamespace(id=1, context_name="worker-1", hostname=None, state="disabled")
    model = MagicMock()
    model.query.get.return_value = context
    db = MagicMock()

    with (
        patch.object(routes_admin, "request", _request(force="false")),
        patch.object(routes_admin, "DockerContextModel", model),
        patch.object(routes_admin, "_context_reference_counts", return_value=(0, 0)),
        patch.object(
            routes_admin.ContainerChallengeModel,
            "query",
            MagicMock(**{"filter_by.return_value.count.return_value": 0}),
            create=True,
        ),
        patch.object(routes_admin, "_context_is_reachable", return_value=True),
        patch.object(routes_admin, "_context_docker_resource_counts", return_value=(0, 1)),
        patch.object(routes_admin, "db", db),
        patch.object(routes_admin, "jsonify", side_effect=_jsonify),
    ):
        result, status = routes_admin.route_api_delete_context(1)

    assert status == 409
    assert result["docker_networks"] == 1
    db.session.delete.assert_not_called()
