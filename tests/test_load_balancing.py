import threading
from collections import defaultdict

from orchestrator import Orchestrator
from unittest.mock import MagicMock


def make_orchestrator(context_weights, container_counts=None):
    host_manager = MagicMock()
    orch = Orchestrator(host_manager)
    orch.health = {ctx: True for ctx in context_weights}
    orch.weights = dict(context_weights)
    orch.container_counts = defaultdict(int, container_counts or {})
    return orch


def test_single_context():
    orch = make_orchestrator({"default": 1})
    assert orch.select_and_reserve() == "default"


def test_empty_returns_none():
    orch = make_orchestrator({})
    assert orch.select_and_reserve() is None


def test_higher_weight_preferred():
    orch = make_orchestrator({"a": 1, "b": 5})
    assert orch.select_and_reserve() == "b"


def test_least_connections_balancing():
    orch = make_orchestrator({"a": 2, "b": 1})
    assert orch.select_and_reserve() == "a"

    # a has 1 container: score_a = 2/2 = 1, score_b = 1/1 = 1
    # tie broken alphabetically
    orch = make_orchestrator({"a": 2, "b": 1}, container_counts={"a": 1})
    assert orch.select_and_reserve() == "a"

    # a has 2 containers: score_a = 2/3 = 0.67, score_b = 1/1 = 1
    orch = make_orchestrator({"a": 2, "b": 1}, container_counts={"a": 2})
    assert orch.select_and_reserve() == "b"


def test_alphabetical_tiebreak():
    orch = make_orchestrator({"zebra": 1, "alpha": 1})
    assert orch.select_and_reserve() == "alpha"


def test_load_distributes_evenly():
    orch = make_orchestrator({"a": 1, "b": 1, "c": 1})
    assert orch.select_and_reserve() == "a"

    orch = make_orchestrator({"a": 1, "b": 1, "c": 1}, container_counts={"a": 1})
    assert orch.select_and_reserve() == "b"

    orch = make_orchestrator({"a": 1, "b": 1, "c": 1}, container_counts={"a": 1, "b": 1})
    assert orch.select_and_reserve() == "c"


def test_unhealthy_context_skipped():
    orch = make_orchestrator({"a": 1, "b": 5})
    orch.health["b"] = False
    assert orch.select_and_reserve() == "a"


def test_all_unhealthy_returns_none():
    orch = make_orchestrator({"a": 1, "b": 1})
    orch.health["a"] = False
    orch.health["b"] = False
    assert orch.select_and_reserve() is None


def test_select_and_reserve_increments():
    orch = make_orchestrator({"a": 1})
    assert orch.container_counts["a"] == 0
    name = orch.select_and_reserve()
    assert name == "a"
    assert orch.container_counts["a"] == 1


def test_select_and_reserve_distributes():
    orch = make_orchestrator({"a": 1, "b": 1})
    first = orch.select_and_reserve()
    second = orch.select_and_reserve()
    assert {first, second} == {"a", "b"}


def test_release_slot():
    orch = make_orchestrator({"a": 1})
    orch.reserve_slot("a")
    assert orch.container_counts["a"] == 1
    orch.release_slot("a")
    assert orch.container_counts["a"] == 0


def test_release_slot_no_negative():
    orch = make_orchestrator({"a": 1})
    orch.release_slot("a")
    assert orch.container_counts["a"] == 0


def test_concurrent_select_and_reserve():
    num_threads = 20
    orch = make_orchestrator({"a": 2, "b": 3, "c": 1})
    barrier = threading.Barrier(num_threads + 1)
    results = []
    errors = []
    results_lock = threading.Lock()
    valid_names = {"a", "b", "c"}

    def worker():
        barrier.wait()
        try:
            result = orch.select_and_reserve()
            with results_lock:
                results.append(result)
        except Exception as e:
            with results_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    barrier.wait()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == num_threads
    assert all(r in valid_names for r in results)
