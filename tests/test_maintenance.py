from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import container_manager as container_manager_module
from container_manager import ContainerManager


def manager_for_test() -> ContainerManager:
    manager = ContainerManager.__new__(ContainerManager)
    manager.app = MagicMock()
    manager.app.app_context.return_value = nullcontext()
    return manager


def test_due_maintenance_job_runs_and_records_start():
    manager = manager_for_test()
    operation = MagicMock()
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = None

    with (
        patch.object(container_manager_module, "_maintenance_lock", return_value=nullcontext(True)),
        patch.object(container_manager_module, "ContainerMaintenanceModel", model),
        patch.object(container_manager_module, "db") as database,
    ):
        assert manager._run_maintenance_job("expiry", 5, operation) is True

    operation.assert_called_once_with()
    database.session.add.assert_called_once()
    database.session.commit.assert_called_once_with()


def test_recent_maintenance_job_is_not_repeated():
    manager = manager_for_test()
    operation = MagicMock()
    model = MagicMock()
    model.query.filter_by.return_value.first.return_value = SimpleNamespace(last_started=10_000)

    with (
        patch.object(container_manager_module, "_maintenance_lock", return_value=nullcontext(True)),
        patch.object(container_manager_module, "ContainerMaintenanceModel", model),
        patch.object(container_manager_module.time, "time", return_value=10_001),
    ):
        assert manager._run_maintenance_job("expiry", 5, operation) is False

    operation.assert_not_called()


def test_worker_without_distributed_lock_does_not_run_job():
    manager = manager_for_test()
    operation = MagicMock()

    with patch.object(container_manager_module, "_maintenance_lock", return_value=nullcontext(False)):
        assert manager._run_maintenance_job("expiry", 5, operation) is False

    operation.assert_not_called()
