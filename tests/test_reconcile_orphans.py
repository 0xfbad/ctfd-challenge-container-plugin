import time
from unittest.mock import patch, MagicMock

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

    db_row_id = "deadbeef" * 8
    young_orphan_id = "00000001" * 8
    old_orphan_id = "00000002" * 8

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    cm.host_manager.list_containers_by_label.return_value = [
        {"name": "chal-u1-c1-100", "id": db_row_id, "created_ts": now - 1000},
        {"name": "chal-u2-c2-200", "id": young_orphan_id, "created_ts": now - 60},
        {"name": "chal-u3-c3-300", "id": old_orphan_id, "created_ts": now - 1000},
    ]
    cm.host_manager.list_containers_by_prefix.return_value = []

    db_row = MagicMock()
    db_row.container_id = db_row_id

    with patch("container_manager.ContainerInfoModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = [db_row]

        cm._reconcile_orphans()

    cm.host_manager.stop_container.assert_called_once_with("default", old_orphan_id)


def test_safety_window_holds_young_container():
    """containers younger than 5 minutes are never killed even if not in DB"""
    cm = make_manager()
    now = time.time()

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    cm.host_manager.list_containers_by_label.return_value = [
        {"name": "chal-u1-c1-100", "id": "newbie" * 10, "created_ts": now - 200},
    ]
    cm.host_manager.list_containers_by_prefix.return_value = []

    with patch("container_manager.ContainerInfoModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.stop_container.assert_not_called()


def test_dedupe_across_label_and_prefix():
    """a stack container shows up in both label and prefix lists but only kills once"""
    cm = make_manager()
    now = time.time()

    orphan_id = "0xdead" * 10

    cm.host_manager.get_connected_contexts.return_value = ["default"]
    entry = {"name": "chal-u1-c1-100", "id": orphan_id, "created_ts": now - 1000}
    cm.host_manager.list_containers_by_label.return_value = [entry]
    cm.host_manager.list_containers_by_prefix.return_value = [entry]

    with patch("container_manager.ContainerInfoModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.stop_container.assert_called_once_with("default", orphan_id)


def test_list_failure_does_not_abort_sweep():
    """one context throwing on list shouldn't stop the others from being reaped"""
    cm = make_manager()
    now = time.time()
    orphan_id = "feedcafe" * 8

    cm.host_manager.get_connected_contexts.return_value = ["broken", "healthy"]

    def list_label(ctx, _label):
        if ctx == "broken":
            raise RuntimeError("ssh broken")
        return [{"name": "chal-u9-c9-900", "id": orphan_id, "created_ts": now - 1000}]

    cm.host_manager.list_containers_by_label.side_effect = list_label
    cm.host_manager.list_containers_by_prefix.return_value = []

    with patch("container_manager.ContainerInfoModel") as mock_model:
        mock_model.query.with_entities.return_value.all.return_value = []

        cm._reconcile_orphans()

    cm.host_manager.stop_container.assert_called_once_with("healthy", orphan_id)


def test_reconcile_runs_at_end_of_kill_expired():
    """kill_expired_containers must call _reconcile_orphans after expiry processing"""
    cm = make_manager()
    cm._reconcile_orphans = MagicMock()
    mock_app = MagicMock()

    with patch("container_manager.ContainerInfoModel") as mock_model:
        mock_model.query.filter.return_value.all.return_value = []

        cm.kill_expired_containers(mock_app)

    cm._reconcile_orphans.assert_called_once()
