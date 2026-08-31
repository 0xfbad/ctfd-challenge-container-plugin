import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from container_manager import ContainerManager
from coordination import InstanceReservation
from exceptions import ContainerException, ContainerUnavailableException

_helpers = sys.modules["_cc_plugin.views.helpers"]
_cleanup_failed_reservation = _helpers._cleanup_failed_reservation


def _reservation():
    return InstanceReservation(
        instance_id="a" * 32,
        provision_token="b" * 32,
        context_id=1,
        context_name="host-a",
        quota_slot=0,
        create_slot=0,
        placement_units=1,
        state="provisioning",
        created=True,
    )


def test_ambiguous_docker_failure_is_deferred_without_false_absence_proof():
    manager = SimpleNamespace(host_manager=MagicMock())
    with (
        patch("_cc_plugin.views.helpers.InstanceCoordinator.mark_cleanup_pending", return_value=True) as pending,
        patch("_cc_plugin.views.helpers.InstanceCoordinator.delete_after_confirmed_cleanup") as delete,
    ):
        assert not _cleanup_failed_reservation(
            manager, _reservation(), RuntimeError("timeout"), ambiguous_external_io=True
        )
    pending.assert_called_once()
    delete.assert_not_called()
    manager.host_manager.force_remove_resources_by_label.assert_not_called()


def test_explicit_missing_context_never_fans_out():
    manager = ContainerManager.__new__(ContainerManager)
    manager._ensure_connected = MagicMock()
    manager.host_manager = MagicMock()
    manager.host_manager._context_configs = {"host-a": "ssh://host-a"}

    with pytest.raises(ContainerUnavailableException, match="not configured"):
        manager.is_container_running("container-id", "missing-host")
    manager.host_manager.get_connected_contexts.assert_not_called()


@pytest.mark.parametrize("instance_id,provision_token", [(None, None), ("bad", "b" * 32), ("a" * 32, "BAD")])
def test_managed_create_requires_immutable_hex_labels(instance_id, provision_token):
    manager = ContainerManager.__new__(ContainerManager)
    manager._ensure_connected = MagicMock()
    manager.host_manager = MagicMock()

    with pytest.raises(ContainerException, match="valid instance and provision"):
        manager.create_container(
            1,
            2,
            3,
            "image:tag",
            8080,
            "",
            512,
            1.0,
            context_name="host-a",
            instance_id=instance_id,
            provision_token=provision_token,
        )
    manager.host_manager.run_container.assert_not_called()
