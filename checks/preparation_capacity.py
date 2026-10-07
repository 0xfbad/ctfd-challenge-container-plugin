import ast
import hashlib
import importlib.util
import tempfile
import unittest
from bisect import bisect_left
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SOURCE = Path(__file__).resolve().parents[1] / "src" / "files"
SPEC = importlib.util.spec_from_file_location("_capacity_check_store", SOURCE / "store.py")
assert SPEC is not None and SPEC.loader is not None
store_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store_module)
RECIPE = {"version": "v1", "image": "sha256:" + "a" * 64, "outputs": ["data.txt"]}
SECRET = "fixture-secret"


def fingerprint(owner, secret=SECRET):
    return hashlib.sha256((owner + ":" + secret).encode()).hexdigest()


def preparation(store, *, eligible=range(1, 351), batch_size=64, change=None):
    challenges = MagicMock()
    filtered = challenges.query.with_entities.return_value.filter.return_value
    filtered.__iter__.return_value = iter([(1,)])
    filtered.order_by.return_value.__iter__.return_value = iter([(1,)])
    users = MagicMock()
    users.id.__gt__.side_effect = lambda minimum: minimum
    owner_query = users.query.with_entities.return_value.filter.return_value.distinct.return_value
    selected = {"minimum": 0, "limit": batch_size}

    def after(minimum):
        selected["minimum"] = minimum
        return owner_query

    def limited(limit):
        selected["limit"] = limit
        return owner_query

    owner_query.with_session.return_value = owner_query
    owner_query.filter.side_effect = after
    owner_query.order_by.return_value = owner_query
    owner_query.limit.side_effect = limited
    owner_query.all.side_effect = lambda: [(owner,) for owner in sorted(set(eligible)) if owner > selected["minimum"]][
        : selected["limit"]
    ]
    database = SimpleNamespace(session=MagicMock())
    settings = {"freshness_secret": SECRET, "freshness_token_length": 6}
    changed = [False]
    namespace = {
        "bisect_left": bisect_left,
        "Challenges": challenges,
        "Users": users,
        "Teams": MagicMock(),
        "db": database,
        "get_config": lambda name: False,
        "utils": SimpleNamespace(get_setting=settings.get, is_team_mode=lambda: change == "mode" and changed[0]),
        "_templates": lambda challenge_id: ["edited-template" if change == "templates" and changed[0] else "fixture"],
        "_store_io": lambda operation, *args, **kwargs: operation(*args, **kwargs),
        "_owner_identity": lambda challenge_id, owner, team_mode, secret, *args: (
            f"user:{owner}",
            fingerprint(f"user:{owner}", secret),
            {"NAME": str(owner), "SECRET": secret},
        ),
        "validate_recipe": store_module.validate_recipe,
        "QueueCapacityFull": store_module.QueueCapacityFull,
        "StoreBusy": store_module.StoreBusy,
        "StoreError": store_module.StoreError,
    }
    tree = ast.parse((SOURCE / "prepare.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_prepare_batch")
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE / "prepare.py"), "exec"), namespace)
    request = store.request

    def request_and_edit(*args, **kwargs):
        try:
            return request(*args, **kwargs)
        finally:
            changed[0] = True
            if change == "secret":
                settings["freshness_secret"] = "edited-secret"
            if change == "length":
                settings["freshness_token_length"] = 7

    with patch.object(store, "request", side_effect=request_and_edit) as requests:
        namespace["_prepare_batch"](store, False, SECRET, 6, batch_size)
    return requests


class PreparationCapacityChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="preparation-capacity-check-")
        self.addCleanup(self.directory.cleanup)
        self.store = store_module.Store(self.directory.name)
        self.store.set_recipe(1, RECIPE)

    def request(self, owner, *, background=True, secret=SECRET):
        return self.store.request(
            1, owner, fingerprint(owner, secret), {"NAME": owner, "SECRET": secret}, background=background
        )

    def cursor(self):
        return self.store._read(self.store.root / ".preparation.json")

    def test_saturation_uses_two_scans_and_advances_normal_cursor(self):
        for owner in range(1, 65):
            self.request(f"user:{owner}")
        self.store._write(self.store.root / ".preparation.json", [1, 64])
        with patch.object(self.store, "_jobs", wraps=self.store._jobs) as scans:
            requests = preparation(self.store)
        self.assertEqual(requests.call_count, 1)
        self.assertEqual(scans.call_count, 2)
        self.assertEqual(self.cursor(), [1, 128])
        self.assertEqual(len(self.store._jobs()), 64)

    def test_contiguous_stale_jobs_behind_cursor_refresh_without_worker_drain(self):
        for owner in range(1, 65):
            self.request(f"user:{owner}", secret="old-secret")
        self.store._write(self.store.root / ".preparation.json", [1, 64])
        for _ in range(6):
            preparation(self.store)
        jobs = self.store._jobs()
        self.assertEqual(len(jobs), 64)
        self.assertTrue(all(job["state"] == "queued" and job["environment"]["SECRET"] == SECRET for job in jobs))
        self.assertEqual(self.cursor(), [1, 64])

    def test_sparse_eligible_cohort_wraps_and_refreshes_behind_cursor_without_drain(self):
        self.store.max_pending = 4
        self.request("user:1", secret="old-secret")
        self.request("user:129", secret="old-secret")
        self.store._write(self.store.root / ".preparation.json", [1, 65])
        eligible = [1, 9, 33, 65, 129, 350]
        preparation(self.store, eligible=eligible)
        self.assertEqual(self.cursor(), [1, 0])
        preparation(self.store, eligible=eligible)
        jobs = self.store._jobs()
        self.assertEqual({job["owner"] for job in jobs}, {"user:1", "user:129"})
        self.assertTrue(all(job["environment"]["SECRET"] == SECRET for job in jobs))
        self.assertEqual(self.cursor(), [1, 0])

    def test_capacity_return_allows_absent_owner_on_later_finite_cohort_sweep(self):
        self.store.max_pending = 2
        occupied = self.request("user:100")
        preparation(self.store, eligible=[1, 2], batch_size=2)
        self.assertEqual(self.cursor(), [1, 2])
        self.store._job_path(occupied["key"]).unlink()
        preparation(self.store, eligible=[1, 2], batch_size=2)
        self.assertEqual(self.cursor(), [1, 0])
        preparation(self.store, eligible=[1, 2], batch_size=2)
        self.assertEqual([job["owner"] for job in self.store._jobs()], ["user:1"])
        self.assertEqual(self.cursor(), [1, 2])

    def test_foreground_reserved_capacity_and_parent_queuefull_compatibility(self):
        self.store.max_pending = 4
        self.request("user:100")
        self.request("user:101")
        with self.assertRaises(store_module.QueueCapacityFull):
            self.request("user:102")
        with self.assertRaises(store_module.QueueFull):
            self.request("user:102")
        foreground = self.request("user:1", background=False)
        self.assertFalse(foreground["background"])
        self.assertEqual(len(self.store._jobs()), 3)
        with patch.object(self.store, "available_space", return_value=1 << 30):
            claimed = self.store.claim()
        self.assertEqual(claimed["owner"], "user:1")

    def test_foreground_capacity_constructs_no_snapshot_payload(self):
        self.store.max_pending = 2
        self.request("user:100", background=False)
        self.request("user:101", background=False)
        original = store_module.QueueCapacityFull
        arguments = []

        def capacity(jobs):
            arguments.append(jobs)
            return original(jobs)

        with (
            patch.object(store_module, "QueueCapacityFull", side_effect=capacity),
            self.assertRaises(original) as caught,
        ):
            self.request("user:102", background=False)
        self.assertEqual(arguments, [None])
        self.assertIsNone(caught.exception.existing_owners)

    def test_snapshot_includes_all_job_states_and_other_challenges(self):
        jobs = [
            {"challenge_id": challenge, "owner": f"user:{owner}", "state": state}
            for challenge, owner, state in [(1, 1, "queued"), (1, 2, "running"), (2, 3, "ready"), (2, 4, "failed")]
        ]
        error = store_module.QueueCapacityFull(jobs)
        self.assertEqual(error.existing_owners, {(1, "user:1"), (1, "user:2"), (2, "user:3"), (2, "user:4")})

    def test_malformed_or_missing_snapshot_falls_back_to_no_hint(self):
        for jobs in [None, [{}], [{"challenge_id": 1}], [{"challenge_id": [], "owner": "user:1"}], [None]]:
            with self.subTest(jobs=jobs):
                self.assertIsNone(store_module.QueueCapacityFull(jobs).existing_owners)

    def test_malformed_snapshot_clears_previous_hint_and_restores_ordinary_requests(self):
        request = self.store.request
        owners = []

        def transient(challenge, owner, *args, **kwargs):
            owners.append(owner)
            if owner == "user:1":
                raise store_module.QueueCapacityFull([{"challenge_id": 1, "owner": "user:2"}])
            if owner == "user:2":
                raise store_module.QueueCapacityFull([{}])
            return request(challenge, owner, *args, **kwargs)

        with patch.object(self.store, "request", side_effect=transient):
            preparation(self.store, eligible=[1, 2, 3, 4], batch_size=4)
        self.assertEqual(owners, ["user:1", "user:2", "user:3", "user:4"])
        self.assertEqual({job["owner"] for job in self.store._jobs()}, {"user:3", "user:4"})
        self.assertEqual(self.cursor(), [1, 4])

    def test_obsolete_ready_identity_keeps_authoritative_request_path_at_full_catalogue(self):
        self.store.max_jobs = 2
        old = self.request("user:2", secret="old-secret")
        old["state"] = "ready"
        self.store._write(self.store._job_path(old["key"]), old)
        directory = self.store.root / "artifacts" / old["key"]
        directory.mkdir()
        (directory / "data.txt").write_text("old bytes")
        self.request("user:100")
        preparation(self.store, eligible=[1, 2, 3])
        jobs = self.store._jobs()
        self.assertEqual(len(jobs), 2)
        self.assertFalse(self.store._job_path(old["key"]).exists())
        refreshed = next(job for job in jobs if job["owner"] == "user:2")
        self.assertEqual(refreshed["environment"]["SECRET"], SECRET)
        self.assertEqual(refreshed["state"], "queued")

    def test_busy_preserves_previous_cursor(self):
        request = self.store.request

        def contended(challenge, owner, *args, **kwargs):
            if owner == "user:2":
                with self.store.lock():
                    return request(challenge, owner, *args, **kwargs)
            return request(challenge, owner, *args, **kwargs)

        with patch.object(self.store, "request", side_effect=contended):
            preparation(self.store, eligible=[1, 2, 3])
        self.assertEqual(self.cursor(), [1, 1])
        self.assertEqual([job["owner"] for job in self.store._jobs()], ["user:1"])

    def test_owner_specific_conflict_skips_one_owner_and_continues(self):
        self.request("user:1", background=False, secret="older-identity")
        requests = preparation(self.store)
        self.assertEqual(requests.call_count, 64)
        jobs = self.store._jobs()
        self.assertEqual(sum(job["background"] for job in jobs), 63)
        self.assertEqual(self.cursor(), [1, 64])

    def test_all_freshness_edits_are_checked_before_absent_owner_hint_skip(self):
        for kind in ["secret", "mode", "templates", "length"]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="preparation-freshness-check-") as root:
                store = store_module.Store(root, max_pending=4)
                store.set_recipe(1, RECIPE)
                for owner in ["user:100", "user:101"]:
                    store.request(1, owner, fingerprint(owner), {"NAME": owner}, background=True)
                requests = preparation(store, eligible=[1, 2, 3], change=kind)
                self.assertEqual(requests.call_count, 1)
                self.assertEqual(len(store._jobs()), 2)
                self.assertEqual(store._read(store.root / ".preparation.json"), [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
