import time
from unittest.mock import MagicMock, patch

from container_manager import ContainerManager


def make_manager():
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm.host_manager = MagicMock()
    cm.host_manager.has_contexts.return_value = True
    cm.orchestrator = MagicMock()
    return cm


def test_only_old_orphan_is_killed():
    """three docker containers: one in DB (skip), one young (skip), one old orphan (kill)"""
    cm = make_manager()
    now = time.time()

    active_instance_id = "deadbeef" * 4
    young_instance_id = "00000001" * 4
    old_instance_id = "00000002" * 4

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    cm.host_manager.list_containers_by_label.return_value = [
        {"name": "active", "id": "c1", "instance_id": active_instance_id, "created_ts": now - 1000},
        {"name": "young", "id": "c2", "instance_id": young_instance_id, "created_ts": now - 60},
        {"name": "old", "id": "c3", "instance_id": old_instance_id, "created_ts": now - 1000},
    ]

    db_row = MagicMock()
    db_row.id = active_instance_id

    with patch("container_manager.ContainerInstanceModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = [db_row]

        cm._reconcile_orphans()

    cm.host_manager.force_remove_resources_by_label.assert_called_once_with(
        "default", f"ctf.instance_id={old_instance_id}"
    )


def test_safety_window_holds_young_container():
    """containers younger than 5 minutes are never killed even if not in DB"""
    cm = make_manager()
    now = time.time()

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    cm.host_manager.list_containers_by_label.return_value = [
        {"name": "young", "id": "c1", "instance_id": "a" * 32, "created_ts": now - 200},
    ]

    with patch("container_manager.ContainerInstanceModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.force_remove_resources_by_label.assert_not_called()


def test_stack_members_are_cleaned_once_by_instance_label():
    cm = make_manager()
    now = time.time()

    instance_id = "f" * 32

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    cm.host_manager.list_containers_by_label.return_value = [
        {"name": "entry", "id": "c1", "instance_id": instance_id, "created_ts": now - 1000},
        {"name": "service", "id": "c2", "instance_id": instance_id, "created_ts": now - 999},
    ]

    with patch("container_manager.ContainerInstanceModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.force_remove_resources_by_label.assert_called_once_with("default", f"ctf.instance_id={instance_id}")


def test_list_failure_does_not_abort_sweep():
    """one context throwing on list shouldn't stop the others from being reaped"""
    cm = make_manager()
    now = time.time()
    instance_id = "feedcafe" * 4

    cm.host_manager.get_connected_contexts.return_value = ["broken", "healthy"]

    def list_label(ctx, _label):
        if ctx == "broken":
            raise RuntimeError("ssh broken")
        return [{"name": "orphan", "id": "c1", "instance_id": instance_id, "created_ts": now - 1000}]

    cm.host_manager.list_containers_by_label.side_effect = list_label
    with patch("container_manager.ContainerInstanceModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.force_remove_resources_by_label.assert_called_once_with("healthy", f"ctf.instance_id={instance_id}")


def test_reconcile_runs_at_end_of_kill_expired():
    """kill_expired_containers must call _reconcile_orphans after expiry processing"""
    cm = make_manager()
    cm._reconcile_orphans = MagicMock()
    mock_app = MagicMock()

    with (
        patch("container_manager.ContainerInstanceModel") as mock_instances,
        patch("container_manager.get_setting", return_value=0),
    ):
        mock_instances.expires.__lt__.return_value = True
        mock_instances.updated_at.__lt__.return_value = True
        mock_instances.provision_deadline.__lt__.return_value = True
        mock_instances.query.filter.return_value.all.return_value = []
        cm.kill_expired_containers(mock_app)

    cm._reconcile_orphans.assert_called_once()
