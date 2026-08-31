from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import orchestrator as orchestrator_module
from orchestrator import Orchestrator


def make_orchestrator(context_weights, health=None):
    host_manager = MagicMock()
    host_manager.get_connected_contexts.return_value = list(context_weights)
    orch = Orchestrator(host_manager)
    orch.health = {ctx: True for ctx in context_weights}
    if health:
        orch.health.update(health)
    orch.weights = dict(context_weights)
    orch.container_counts = defaultdict(int)
    return orch


def test_unhealthy_stays_in_health_dict():
    orch = make_orchestrator({"a": 1, "b": 1})
    orch.health["b"] = False

    assert "b" in orch.health
    assert orch.health["b"] is False
    assert orch.health["a"] is True


def test_older_health_probe_cannot_overwrite_newer_result():
    orch = make_orchestrator({"a": 1}, health={"a": False})
    orch.host_manager.get_configured_contexts.return_value = ["a"]
    orch.host_manager.ping.return_value = True
    context = SimpleNamespace(
        id=1,
        context_name="a",
        state="active",
        health_state="unhealthy",
    )

    with (
        patch.object(orchestrator_module, "DockerContextModel") as model,
        patch.object(orchestrator_module, "db") as database,
        patch.object(orchestrator_module, "event_logger") as events,
    ):
        model.query.filter_by.return_value.first.return_value = context
        model.health_checked_at.__lt__.return_value = True
        model.query.filter.return_value.update.return_value = 0
        orch.health_check()

    assert orch.health["a"] is False
    events.log_event.assert_not_called()
    database.session.commit.assert_called_once()
