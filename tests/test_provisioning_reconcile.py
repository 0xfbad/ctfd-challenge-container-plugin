import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from container_manager import ContainerManager


def test_expired_provisioning_is_cleaned_by_immutable_instance_label():
    manager = ContainerManager.__new__(ContainerManager)
    manager.host_manager = MagicMock()
    manager.host_manager.has_contexts.return_value = True
    manager._reconcile_orphans = MagicMock()
    instance = SimpleNamespace(
        id="a" * 32,
        docker_context=SimpleNamespace(context_name="host-a"),
        expires=int(time.time()) + 100,
    )
    app = MagicMock()

    with (
        patch("container_manager.ContainerInstanceModel") as instances,
        patch("container_manager.ContainerInfoModel") as info,
        patch("container_manager.InstanceCoordinator") as coordinator,
        patch("container_manager.get_setting", return_value=0),
        patch("container_manager.db"),
    ):
        instances.expires.__lt__.return_value = True
        instances.updated_at.__lt__.return_value = True
        instances.provision_deadline.__lt__.return_value = True
        instances.query.filter.return_value.all.return_value = [instance]
        info.query.filter_by.return_value.first.return_value = None
        info.query.filter.return_value.filter.return_value.all.return_value = []
        coordinator.claim_operation.return_value = "b" * 32
        coordinator.delete_after_confirmed_cleanup.return_value = True

        manager.kill_expired_containers(app)

    manager.host_manager.force_remove_resources_by_label.assert_called_once_with(
        "host-a", f"ctf.instance_id={instance.id}"
    )
    coordinator.delete_after_confirmed_cleanup.assert_called_once()
    assert coordinator.delete_after_confirmed_cleanup.call_args.kwargs["reason"] == "reconciled"
