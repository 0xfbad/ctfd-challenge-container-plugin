import threading
from collections import defaultdict

import pytest
from container_manager import ContainerManager, ContainerException
from unittest.mock import MagicMock


def make_manager(context_weights, container_counts=None):
    """context_weights: dict of {name: weight}"""
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm._context_configs = {ctx: f"url_{ctx}" for ctx in context_weights}
    cm._context_weights = dict(context_weights)
    cm._health = {ctx: True for ctx in context_weights}
    cm._container_counts = defaultdict(int, container_counts or {})
    cm._thread_local = MagicMock()
    cm._thread_local.clients = {}
    cm._context_lock = threading.Lock()
    cm._config_generation = 0
    cm._pool = None
    cm._semaphores = {}
    return cm


def test_single_context():
    cm = make_manager({"default": 1})
    assert cm.select_and_reserve() == "default"


def test_empty_raises():
    cm = make_manager({})
    with pytest.raises(ContainerException, match="no healthy contexts"):
        cm.select_and_reserve()


def test_higher_weight_preferred():
    cm = make_manager({"a": 1, "b": 5})
    assert cm.select_and_reserve() == "b"


def test_least_connections_balancing():
    # weight 2 vs weight 1, both at 0 -> "a" wins (score 2 vs 1)
    cm = make_manager({"a": 2, "b": 1})
    assert cm.select_and_reserve() == "a"

    # "a" has 1 container: score_a = 2/2 = 1, score_b = 1/1 = 1
    # tie broken alphabetically -> "a"
    cm = make_manager({"a": 2, "b": 1}, container_counts={"a": 1})
    assert cm.select_and_reserve() == "a"

    # "a" has 2 containers: score_a = 2/3 = 0.67, score_b = 1/1 = 1
    cm = make_manager({"a": 2, "b": 1}, container_counts={"a": 2})
    assert cm.select_and_reserve() == "b"


def test_alphabetical_tiebreak():
    cm = make_manager({"zebra": 1, "alpha": 1})
    assert cm.select_and_reserve() == "alpha"


def test_load_distributes_evenly():
    cm = make_manager({"a": 1, "b": 1, "c": 1})
    assert cm.select_and_reserve() == "a"

    cm = make_manager({"a": 1, "b": 1, "c": 1}, container_counts={"a": 1})
    assert cm.select_and_reserve() == "b"

    cm = make_manager({"a": 1, "b": 1, "c": 1}, container_counts={"a": 1, "b": 1})
    assert cm.select_and_reserve() == "c"


def test_unhealthy_context_skipped():
    cm = make_manager({"a": 1, "b": 5})
    cm._health["b"] = False
    assert cm.select_and_reserve() == "a"


def test_all_unhealthy_raises():
    cm = make_manager({"a": 1, "b": 1})
    cm._health["a"] = False
    cm._health["b"] = False
    with pytest.raises(ContainerException, match="no healthy contexts"):
        cm.select_and_reserve()


def test_select_and_reserve_increments():
    cm = make_manager({"a": 1})
    assert cm._container_counts["a"] == 0
    name = cm.select_and_reserve()
    assert name == "a"
    assert cm._container_counts["a"] == 1


def test_select_and_reserve_distributes():
    cm = make_manager({"a": 1, "b": 1})
    first = cm.select_and_reserve()
    second = cm.select_and_reserve()
    assert {first, second} == {"a", "b"}


def test_release_slot():
    cm = make_manager({"a": 1})
    cm.reserve_slot("a")
    assert cm._container_counts["a"] == 1
    cm.release_slot("a")
    assert cm._container_counts["a"] == 0


def test_release_slot_no_negative():
    cm = make_manager({"a": 1})
    cm.release_slot("a")
    assert cm._container_counts["a"] == 0


def test_concurrent_select_and_reserve():
    num_threads = 20
    cm = make_manager({"a": 2, "b": 3, "c": 1})
    barrier = threading.Barrier(num_threads + 1)
    results = []
    errors = []
    results_lock = threading.Lock()
    valid_names = {"a", "b", "c"}

    def worker():
        barrier.wait()
        try:
            result = cm.select_and_reserve()
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
