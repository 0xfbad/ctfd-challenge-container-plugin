import ast
import logging
import math
import re
import sys
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src"
LOGGER = logging.getLogger("network_orphan_checks")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def source_module(filename, class_name, methods, namespace, assignments=(), functions=()):
    path = SOURCE / filename
    tree = ast.parse(path.read_text())
    klass = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name))
    selected = [node for node in klass.body if isinstance(node, ast.FunctionDef) and node.name in methods]
    selected.extend(
        (
            node
            for node in klass.body
            if isinstance(node, ast.Assign)
            and any((isinstance(target, ast.Name) and target.id in assignments for target in node.targets))
        )
    )
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
    helpers.extend(
        (
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any((isinstance(target, ast.Name) and target.id in assignments for target in node.targets))
        )
    )
    module = types.ModuleType("_network_check_" + path.stem)
    module.__dict__.update(namespace)
    extracted = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            *helpers,
            ast.ClassDef(name=class_name, bases=[], keywords=[], body=selected, decorator_list=[]),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(extracted)
    exec(compile(extracted, str(path), "exec"), module.__dict__)
    sys.modules[module.__name__] = module
    return module


ContainerUnavailableException = type("ContainerUnavailableException", (RuntimeError,), {})
DockerException = type("DockerException", (RuntimeError,), {})
APIError = type("APIError", (DockerException,), {})
NotFound = type("NotFound", (APIError,), {})
HOST_MODULE = source_module(
    "docker_host_manager.py",
    "DockerHostManager",
    {
        "__init__",
        "_mark_connected",
        "_mark_connect_failed",
        "_cooling_down",
        "_call",
        "_close_clients",
        "_get_client",
        "_release_client",
        "_clear_client",
        "_invoke_client_op",
        "_call_with_client_op",
        "_parse_container_created",
        "_list_containers",
        "list_containers_by_label",
        "list_resources_by_label",
        "force_remove_resources_by_label",
    },
    {
        "threading": threading,
        "time": time,
        "datetime": datetime,
        "logger": LOGGER,
        "gevent": SimpleNamespace(monkey=SimpleNamespace(is_module_patched=lambda name: False)),
        "docker": SimpleNamespace(
            errors=SimpleNamespace(APIError=APIError, DockerException=DockerException, NotFound=NotFound)
        ),
        "paramiko": SimpleNamespace(
            ssh_exception=SimpleNamespace(SSHException=type("SSHException", (RuntimeError,), {}))
        ),
        "ContainerUnavailableException": ContainerUnavailableException,
        "ContainerStartTimeout": type("ContainerStartTimeout", (RuntimeError,), {}),
        "_new_docker_client": MagicMock(),
        "_confirm_removal_in_progress": lambda *args: False,
    },
    assignments={"CLIENT_FAILURE_COOLDOWN"},
)
DockerHostManager = HOST_MODULE.DockerHostManager
MANAGER_MODULE = source_module(
    "container_manager.py",
    "ContainerManager",
    {"_reconcile_orphans"},
    {
        "time": time,
        "math": math,
        "re": re,
        "logger": LOGGER,
        "ContainerInstanceModel": MagicMock(),
        "event_logger": SimpleNamespace(log_event=MagicMock()),
    },
    assignments={"_RESERVATION_ID", "RECONCILE_INSTANCE_LABEL", "RECONCILE_SAFETY_AGE_SECONDS"},
    functions={"_valid_reservation_identity"},
)
ContainerManager = MANAGER_MODULE.ContainerManager
NOW = 1000000.0
INSTANCE_ID = "a" * 32


def entry(created_ts, instance_id=INSTANCE_ID, name="resource"):
    return {"name": name, "id": name, "instance_id": instance_id, "created_ts": created_ts}


def reconciler(entries):
    manager = ContainerManager.__new__(ContainerManager)
    manager.host_manager = MagicMock()
    manager.host_manager.get_connected_contexts.return_value = ["local"]
    manager.host_manager.list_resources_by_label.return_value = entries
    return manager


def sweep(manager, *, active_ids=(), newly_reserved=False):
    with (
        patch("_network_check_container_manager.time.time", return_value=NOW),
        patch("_network_check_container_manager.ContainerInstanceModel") as model,
        patch("_network_check_container_manager.event_logger.log_event") as events,
    ):
        model.query.with_entities.return_value.all.return_value = [SimpleNamespace(id=value) for value in active_ids]
        model.query.filter_by.return_value.first.return_value = (
            SimpleNamespace(id=INSTANCE_ID) if newly_reserved else None
        )
        manager._reconcile_orphans()
    return (model, events)


def host_with_client(client):
    host = DockerHostManager()
    host._context_configs = {"local": "unix:///var/run/docker.sock"}
    factory = patch("_network_check_docker_host_manager._new_docker_client", return_value=client)
    return (host, factory)


class NetworkOrphanChecks(unittest.TestCase):
    def test_old_orphan_of_either_kind_uses_exact_immutable_label(self):
        for resource_name in ["network", "container"]:
            with self.subTest(resource_name=resource_name):
                manager = reconciler([entry(NOW - 301, name=resource_name)])
                _, events = sweep(manager)
                manager.host_manager.list_resources_by_label.assert_called_once_with("local", "ctf.instance_id")
                manager.host_manager.force_remove_resources_by_label.assert_called_once_with(
                    "local", f"ctf.instance_id={INSTANCE_ID}"
                )
                assert events.call_args.args[0] == "orphan_reaped"

    def test_young_network_protects_old_container_for_same_reservation(self):
        for reverse in [False, True]:
            with self.subTest(reverse=reverse):
                entries = [entry(NOW - 1000, name="container"), entry(NOW - 299, name="network")]
                manager = reconciler(entries[::-1] if reverse else entries)
                sweep(manager)
                manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_all_old_stack_resources_are_removed_once_and_age_uses_youngest(self):
        manager = reconciler([entry(NOW - 1000, name="container"), entry(NOW - 300, name="network")])
        _, events = sweep(manager)
        manager.host_manager.force_remove_resources_by_label.assert_called_once_with(
            "local", f"ctf.instance_id={INSTANCE_ID}"
        )
        assert events.call_args.kwargs["metadata"]["age_seconds"] == 300

    def test_unknown_or_invalid_member_age_protects_entire_reservation(self):
        for invalid_age in [None, 0, -1, "invalid", [], float("nan"), float("inf"), -float("inf"), NOW + 1]:
            for reverse in [False, True]:
                with self.subTest(invalid_age=invalid_age, reverse=reverse):
                    entries = [entry(NOW - 1000), entry(invalid_age, name="network")]
                    manager = reconciler(entries[::-1] if reverse else entries)
                    sweep(manager)
                    manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_invalid_identity_never_authorizes_resource_removal(self):
        for invalid_id in ["", None, "old-name", "A" * 32, "a" * 31, "a" * 33, "a" * 32 + "=other"]:
            with self.subTest(invalid_id=invalid_id):
                manager = reconciler([entry(NOW - 1000, instance_id=invalid_id)])
                model, _ = sweep(manager)
                model.query.filter_by.assert_not_called()
                manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_global_reservation_protects_resource_even_without_context_placement(self):
        manager = reconciler([entry(NOW - 1000, name="network")])
        model, _ = sweep(manager, active_ids=[INSTANCE_ID])
        model.query.filter_by.assert_not_called()
        manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_reservation_appearing_after_initial_query_is_rechecked_before_remove(self):
        manager = reconciler([entry(NOW - 1000, name="network")])
        model, _ = sweep(manager, newly_reserved=True)
        model.query.filter_by.assert_called_once_with(id=INSTANCE_ID)
        manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_failed_context_discovery_does_not_block_healthy_context(self):
        manager = reconciler([])
        manager.host_manager.get_connected_contexts.return_value = ["failed", "healthy"]
        manager.host_manager.list_resources_by_label.side_effect = [RuntimeError("list failed"), [entry(NOW - 1000)]]
        sweep(manager)
        manager.host_manager.force_remove_resources_by_label.assert_called_once_with(
            "healthy", f"ctf.instance_id={INSTANCE_ID}"
        )

    def test_failed_removal_is_retried_without_emitting_success(self):
        manager = reconciler([entry(NOW - 1000, name="network")])
        manager.host_manager.force_remove_resources_by_label.side_effect = RuntimeError("attached foreign endpoint")
        _, failed_events = sweep(manager)
        failed_events.assert_not_called()
        manager.host_manager.force_remove_resources_by_label.side_effect = None
        _, recovered_events = sweep(manager)
        assert manager.host_manager.force_remove_resources_by_label.call_count == 2
        recovered_events.assert_called_once()

    def test_strict_discovery_uses_both_label_layouts_in_one_client_lease(self):
        client = MagicMock()
        created = "2026-10-07T00:00:00.123456789Z"
        container = SimpleNamespace(
            name="container",
            id="container-id",
            attrs={"Created": created, "Config": {"Labels": {"ctf.instance_id": INSTANCE_ID}}},
        )
        network = SimpleNamespace(
            name="network", id="network-id", attrs={"Created": created, "Labels": {"ctf.instance_id": INSTANCE_ID}}
        )
        client.containers.list.return_value = [container]
        client.networks.list.return_value = [network]
        host, factory = host_with_client(client)
        with factory as constructor:
            result = host.list_resources_by_label("local", "ctf.instance_id")
        constructor.assert_called_once()
        client.containers.list.assert_called_once_with(
            all=True, filters={"label": "ctf.instance_id"}, ignore_removed=True
        )
        client.networks.list.assert_called_once_with(filters={"label": "ctf.instance_id"})
        assert [value["instance_id"] for value in result] == [INSTANCE_ID, INSTANCE_ID]
        assert [value["id"] for value in result] == ["container-id", "network-id"]
        assert result[0]["created_ts"] == result[1]["created_ts"] > 0
        assert not host._clients
        client.close.assert_not_called()

    def test_failed_kind_does_not_return_partial_discovery_and_clears_transport(self):
        for failed_kind in ["containers", "networks"]:
            with self.subTest(failed_kind=failed_kind):
                client = MagicMock()
                client.containers.list.return_value = [SimpleNamespace(name="container", id="id", attrs={})]
                client.networks.list.return_value = []
                getattr(client, failed_kind).list.side_effect = RuntimeError("transport broken")
                host, factory = host_with_client(client)
                with factory, self.assertRaisesRegex(ContainerUnavailableException, "transient client failure"):
                    host.list_resources_by_label("local", "ctf.instance_id")
                client.close.assert_called_once()
                assert not host._clients
                assert not host._idle_clients

    def test_unknown_creation_time_is_reported_as_unknown_for_age_guard(self):
        for created in ["", None, "not a timestamp", {}, "nan", True, False]:
            with self.subTest(created=created):
                client = MagicMock()
                client.containers.list.return_value = []
                client.networks.list.return_value = [
                    SimpleNamespace(
                        name="network",
                        id="network-id",
                        attrs={"Created": created, "Labels": {"ctf.instance_id": INSTANCE_ID}},
                    )
                ]
                host, factory = host_with_client(client)
                with factory:
                    result = host.list_resources_by_label("local", "ctf.instance_id")
                assert result[0]["created_ts"] == 0

    def test_legacy_container_listing_keeps_best_effort_contract(self):
        client = MagicMock()
        client.containers.list.side_effect = RuntimeError("transport broken")
        host, factory = host_with_client(client)
        with factory:
            assert host.list_containers_by_label("local", "ctf.instance_id") == []
        client.networks.list.assert_not_called()

    def test_filter_expression_does_not_replace_immutable_reservation_identity(self):
        client = MagicMock()
        client.containers.list.return_value = []
        client.networks.list.return_value = [
            SimpleNamespace(
                name="network",
                id="network-id",
                attrs={
                    "Created": "2026-10-07T00:00:00Z",
                    "Labels": {"owned.test": "scope", "ctf.instance_id": INSTANCE_ID},
                },
            )
        ]
        host, factory = host_with_client(client)
        with factory:
            entries = host.list_resources_by_label("local", "owned.test=scope")
        client.networks.list.assert_called_once_with(filters={"label": "owned.test=scope"})
        assert entries[0]["instance_id"] == INSTANCE_ID

    def test_sql_recheck_failure_deletes_nothing(self):
        manager = reconciler([entry(NOW - 1000)])
        with (
            patch("_network_check_container_manager.time.time", return_value=NOW),
            patch("_network_check_container_manager.ContainerInstanceModel") as model,
        ):
            model.query.with_entities.return_value.all.return_value = []
            model.query.filter_by.return_value.first.side_effect = RuntimeError("database unavailable")
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                manager._reconcile_orphans()
        manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_initial_sql_failure_deletes_nothing(self):
        manager = reconciler([entry(NOW - 1000)])
        with patch("_network_check_container_manager.ContainerInstanceModel") as model:
            model.query.with_entities.return_value.all.side_effect = RuntimeError("database unavailable")
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                manager._reconcile_orphans()
        manager.host_manager.list_resources_by_label.assert_not_called()
        manager.host_manager.force_remove_resources_by_label.assert_not_called()

    def test_distinct_replacement_identity_remains_protected(self):
        replacement_id = "b" * 32
        manager = reconciler([entry(NOW - 1000), entry(NOW - 1000, instance_id=replacement_id)])
        sweep(manager, active_ids=[replacement_id])
        manager.host_manager.force_remove_resources_by_label.assert_called_once_with(
            "local", f"ctf.instance_id={INSTANCE_ID}"
        )

    def test_exact_remover_keeps_foreign_endpoint_attached_on_daemon_error(self):
        client = MagicMock()
        container = MagicMock()
        network = MagicMock()
        network.remove.side_effect = APIError("network has active endpoints")
        client.containers.list.return_value = [container]
        client.networks.list.return_value = [network]
        host, factory = host_with_client(client)
        with factory, self.assertRaisesRegex(APIError, "active endpoints"):
            host.force_remove_resources_by_label("local", f"ctf.instance_id={INSTANCE_ID}")
        container.remove.assert_called_once_with(force=True)
        network.remove.assert_called_once_with()
        network.disconnect.assert_not_called()
        assert not host._clients

    def test_malformed_listing_fails_closed_instead_of_returning_partial_resources(self):
        for malformed in [None, {"unexpected": "schema"}]:
            with self.subTest(malformed=malformed):
                client = MagicMock()
                client.containers.list.return_value = []
                client.networks.list.return_value = malformed
                host, factory = host_with_client(client)
                with factory, self.assertRaisesRegex(ContainerUnavailableException, "transient client failure"):
                    host.list_resources_by_label("local", "ctf.instance_id")
                client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
