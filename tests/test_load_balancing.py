import threading

import pytest
from container_manager import ContainerManager, ContainerException
from unittest.mock import MagicMock, patch


def make_manager(context_weights):
    """context_weights: dict of {name: weight}"""
    cm = object.__new__(ContainerManager)
    cm.settings = {}
    cm.app = MagicMock()
    cm._context_configs = {ctx: f"url_{ctx}" for ctx in context_weights}
    cm._context_weights = dict(context_weights)
    cm._thread_local = MagicMock()
    cm._thread_local.clients = {}
    cm._context_lock = threading.Lock()
    cm._pool = None
    cm._semaphores = {}
    return cm


def _mock_db_counts(counts):
    """Returns a patch that makes ContainerInfoModel.query.all() return rows with docker_context set."""
    rows = []
    for ctx_name, count in counts.items():
        for _ in range(count):
            row = MagicMock()
            row.docker_context = ctx_name
            rows.append(row)

    mock_model = MagicMock()
    mock_model.query.all.return_value = rows
    return patch("container_manager.ContainerInfoModel", mock_model)


def test_single_context():
    cm = make_manager({"default": 1})

    with _mock_db_counts({}):
        assert cm.get_next_context() == "default"


def test_empty_raises():
    cm = make_manager({})
    with pytest.raises(ContainerException, match="no docker contexts"):
        cm.get_next_context()


def test_higher_weight_preferred():
    cm = make_manager({"a": 1, "b": 5})

    with _mock_db_counts({}):
        assert cm.get_next_context() == "b"


def test_least_connections_balancing():
    # weight 2 vs weight 1, both at 0 -> "a" wins (score 2 vs 1)
    cm = make_manager({"a": 2, "b": 1})

    with _mock_db_counts({}):
        assert cm.get_next_context() == "a"

    # "a" has 1 container: score_a = 2/2 = 1, score_b = 1/1 = 1
    # tie broken alphabetically -> "a"
    with _mock_db_counts({"a": 1}):
        assert cm.get_next_context() == "a"

    # "a" has 2 containers: score_a = 2/3 = 0.67, score_b = 1/1 = 1
    with _mock_db_counts({"a": 2}):
        assert cm.get_next_context() == "b"


def test_alphabetical_tiebreak():
    cm = make_manager({"zebra": 1, "alpha": 1})

    with _mock_db_counts({}):
        assert cm.get_next_context() == "alpha"


def test_load_distributes_evenly():
    cm = make_manager({"a": 1, "b": 1, "c": 1})

    with _mock_db_counts({}):
        assert cm.get_next_context() == "a"

    with _mock_db_counts({"a": 1}):
        assert cm.get_next_context() == "b"

    with _mock_db_counts({"a": 1, "b": 1}):
        assert cm.get_next_context() == "c"


def test_concurrent_get_next_context():
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
            with _mock_db_counts({}):
                result = cm.get_next_context()
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
