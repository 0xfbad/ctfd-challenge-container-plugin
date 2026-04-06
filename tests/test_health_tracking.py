from collections import defaultdict

from orchestrator import Orchestrator
from unittest.mock import MagicMock


def make_orchestrator(context_weights, health=None):
    host_manager = MagicMock()
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


def test_select_skips_unhealthy():
    orch = make_orchestrator({"a": 1, "b": 5})
    orch.health["b"] = False

    assert orch.select_and_reserve() == "a"


def test_flipping_health_re_enables_scheduling():
    orch = make_orchestrator({"a": 1, "b": 5})
    orch.health["b"] = False

    assert orch.select_and_reserve() == "a"

    orch.health["b"] = True
    assert orch.select_and_reserve() == "b"
