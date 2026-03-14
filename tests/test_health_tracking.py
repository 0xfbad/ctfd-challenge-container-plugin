import threading
from collections import defaultdict

from container_manager import ContainerManager
from unittest.mock import MagicMock


def make_manager(context_weights, health=None):
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm._context_configs = {ctx: f"url_{ctx}" for ctx in context_weights}
    cm._context_weights = dict(context_weights)
    cm._health = {ctx: True for ctx in context_weights}
    if health:
        cm._health.update(health)
    cm._container_counts = defaultdict(int)
    cm._thread_local = MagicMock()
    cm._thread_local.clients = {}
    cm._context_lock = threading.Lock()
    cm._config_generation = 0
    cm._pool = None
    cm._semaphores = {}
    return cm


def test_unhealthy_stays_in_health_dict():
    cm = make_manager({"a": 1, "b": 1})
    cm._health["b"] = False

    assert "b" in cm._health
    assert cm._health["b"] is False
    assert cm._health["a"] is True


def test_select_skips_unhealthy():
    cm = make_manager({"a": 1, "b": 5})
    cm._health["b"] = False

    # even though b has higher weight, it's unhealthy so a is picked
    assert cm.select_and_reserve() == "a"


def test_flipping_health_re_enables_scheduling():
    cm = make_manager({"a": 1, "b": 5})
    cm._health["b"] = False

    assert cm.select_and_reserve() == "a"

    cm._health["b"] = True
    assert cm.select_and_reserve() == "b"
