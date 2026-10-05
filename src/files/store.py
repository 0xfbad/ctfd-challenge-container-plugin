from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path


class StoreError(Exception):
    pass


class StoreBusy(StoreError):
    pass


class QueueFull(StoreError):
    pass


class RecipeChanged(StoreError):
    pass


class InvalidRecipe(StoreError):
    pass


class ArtifactUnavailable(StoreError):
    pass


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_IMAGE = re.compile(r"(?:[a-z0-9][a-z0-9./:_-]{0,254}@)?sha256:[0-9a-f]{64}\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_OWNER = re.compile(r"(?:user|team):[1-9][0-9]*\Z")
PROTOCOL_VERSION = "personalized-files-v1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def remove_if_exists(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass


def validate_recipe(data):
    allowed = {"version", "image", "outputs", "timeout_seconds", "max_output_bytes"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise InvalidRecipe("unknown recipe fields")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip() or len(version) > 128:
        raise InvalidRecipe("version must contain 1 to 128 printable ASCII characters")
    if any(ord(char) < 32 or ord(char) > 126 for char in version):
        raise InvalidRecipe("version must contain printable ASCII characters")
    image = data.get("image")
    if not isinstance(image, str) or not _IMAGE.fullmatch(image):
        raise InvalidRecipe("image must be an immutable sha256 image ID or repository digest")
    outputs = data.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= 16:
        raise InvalidRecipe("outputs must contain 1 to 16 filenames")
    if any(not isinstance(name, str) or not _NAME.fullmatch(name) for name in outputs):
        raise InvalidRecipe("outputs must use flat ASCII filenames")
    if len(set(outputs)) != len(outputs):
        raise InvalidRecipe("output filenames must be unique")
    recipe: dict[str, str | int | list[str]] = {"version": version, "image": image, "outputs": outputs.copy()}
    for name, default, upper in (("timeout_seconds", 120, 600), ("max_output_bytes", 33554432, 134217728)):
        value = data.get(name, default)
        if type(value) is not int or not 1 <= value <= upper:
            raise InvalidRecipe(f"{name} must be an integer from 1 to {upper}")
        recipe[name] = value
    return recipe


class Store:
    def __init__(self, root, *, max_pending=128, max_jobs=0, max_bytes=None, retention_seconds=None):
        if max_bytes is None:
            try:
                max_bytes = int(os.environ.get("PERSONALIZED_FILES_MAX_BYTES", "0"))
            except ValueError as error:
                raise StoreError("invalid file cache byte limit") from error
        if type(max_bytes) is not int or not 0 <= max_bytes <= 2**63 - 1:
            raise StoreError("invalid file cache byte limit")
        if type(max_jobs) is not int or max_jobs < 0:
            raise StoreError("invalid file catalog limit")
        if type(max_pending) is not int or max_pending < 2:
            raise StoreError("invalid file queue limit")
        if retention_seconds is not None and (type(retention_seconds) is not int or retention_seconds < 0):
            raise StoreError("invalid file retention interval")
        self.root = Path(root)
        self.max_pending = max_pending
        self.max_jobs = max_jobs
        self.max_bytes = max_bytes
        self.retention_seconds = retention_seconds
        self._owner_mode = None
        for name in ("", "recipes", "jobs", "artifacts", "staging"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock("namespace", blocking=True):
            path = self.root / ".namespace.json"
            namespace = self._read(path)
            if namespace is None:
                namespace = secrets.token_hex(16)
                self._write(path, namespace)
            if not isinstance(namespace, str) or not re.fullmatch(r"[0-9a-f]{32}", namespace):
                raise StoreError("invalid artifact store namespace")
            self.namespace = namespace

    @contextmanager
    def lock(self, name="queue", *, shared=False, blocking=False):
        with (self.root / f".{name}.lock").open("a+b") as handle:
            try:
                mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                fcntl.flock(handle, mode if blocking else mode | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StoreBusy("artifact store is busy, retry shortly") from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self, path):
        try:
            with path.open("rb") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise StoreError("artifact metadata is unavailable") from exc

    def _write(self, path, data):
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical(data))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path):
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _recipe_path(self, challenge_id):
        if type(challenge_id) is not int or challenge_id < 1:
            raise InvalidRecipe("challenge ID must be a positive integer")
        return self.root / "recipes" / f"{challenge_id}.json"

    def _job_path(self, key):
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise StoreError("invalid artifact key")
        return self.root / "jobs" / f"{key}.json"

    def get_recipe(self, challenge_id):
        return self._read(self._recipe_path(challenge_id))

    def _available_recipe(self, challenge_id):
        try:
            recipe = self.get_recipe(challenge_id)
            validate_recipe(recipe)
            return recipe
        except StoreError:
            return None

    def set_recipe(self, challenge_id, data):
        recipe = validate_recipe(data)
        with self.lock():
            path = self._recipe_path(challenge_id)
            existing = self._available_recipe(challenge_id)
            if existing == recipe:
                return recipe
            if not path.exists() and len(list((self.root / "recipes").glob("*.json"))) >= 2048:
                raise QueueFull("recipe catalog is full")
            self._write(path, recipe)
            self._retire_obsolete(challenge_id, recipe)
        return recipe

    def delete_recipe(self, challenge_id):
        with self.lock():
            self._recipe_path(challenge_id).unlink(missing_ok=True)
            self._sync_directory(self.root / "recipes")
            self._retire_obsolete(challenge_id, None)

    def _retire_obsolete(self, challenge_id, recipe, jobs=None):
        retained = []
        removed = False
        for job in self._jobs() if jobs is None else jobs:
            if job["challenge_id"] != challenge_id or job["recipe"] == recipe or job["state"] == "ready":
                retained.append(job)
                continue
            if job["state"] == "running":
                job.update(state="failed", error="configuration changed", updated=time.time())
                self._write(self._job_path(job["key"]), job)
                retained.append(job)
                continue
            remove_if_exists(self.root / "artifacts" / job["key"])
            self._job_path(job["key"]).unlink(missing_ok=True)
            removed = True
        if removed:
            self._sync_directory(self.root / "jobs")
        return retained

    def _reusable(self, job):
        if not job or (job["state"] == "failed" and job.get("error") == "configuration changed"):
            return False
        return job["state"] != "ready" or self._complete(job)

    def _jobs(self):
        return [job for path in sorted((self.root / "jobs").glob("*.json")) if (job := self._read(path))]

    def _retire_identity(self, challenge_id, owner, key, jobs):
        return self._retire_matching(
            jobs,
            lambda job: job["challenge_id"] == challenge_id and job["owner"] == owner and job["key"] != key,
        )

    def _retire_matching(self, jobs, obsolete):
        retained = []
        removed = False
        for job in jobs:
            if not obsolete(job):
                retained.append(job)
                continue
            if job["state"] == "running":
                job.update(state="failed", error="configuration changed", obsolete=True, updated=time.time())
                self._write(self._job_path(job["key"]), job)
                retained.append(job)
                continue
            remove_if_exists(self.root / "artifacts" / job["key"])
            self._job_path(job["key"]).unlink(missing_ok=True)
            removed = True
        if removed:
            self._sync_directory(self.root / "jobs")
        return retained

    def _owner_mode_stamp(self, kind):
        metadata = (self.root / ".owner-mode.json").stat()
        return kind, metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size

    def _owner_mode_current(self, kind):
        try:
            return self._owner_mode == self._owner_mode_stamp(kind)
        except FileNotFoundError:
            return False

    def _reconcile_owner_mode(self, kind, jobs, *, background, observed_order):
        path = self.root / ".owner-mode.json"
        try:
            mode = self._read(path)
        except StoreError:
            mode = None
        expected = {"namespace": self.namespace, "kind": kind}
        order = max(self._demand_order(), self._foreground_order(jobs))
        if background and observed_order is None:
            if mode != expected and order > 0:
                raise QueueFull("owner mode has newer download demand, retry later")
            if any(not job.get("background", False) and not job["owner"].startswith(kind + ":") for job in jobs):
                raise QueueFull("owner mode has newer download demand, retry later")
        if observed_order is not None and order > observed_order:
            if background or mode != expected:
                raise QueueFull("owner mode has newer download demand, retry later")
            if any(not job["owner"].startswith(kind + ":") for job in jobs):
                raise QueueFull("owner mode has newer download demand, retry later")
        jobs = self._retire_matching(jobs, lambda job: not job["owner"].startswith(kind + ":"))
        if mode != expected:
            self._next_request_order(jobs, minimum=order)
            self._write(path, expected)
        self._owner_mode = self._owner_mode_stamp(kind)
        return jobs

    def _payload_bytes(self, jobs):
        total = 0
        for job in jobs:
            directory = self.root / "artifacts" / job["key"]
            for path in directory.glob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except FileNotFoundError:
                    continue
        return total

    def available_space(self):
        usage = shutil.disk_usage(self.root)
        reserve = usage.total // 20  # the handout volume shares a disk with database and logs
        return usage.free - reserve

    @staticmethod
    def _request_order(job):
        return job.get("requested_at", int(job["created"] * 1_000_000_000))

    def _foreground_order(self, jobs):
        return max((self._request_order(job) for job in jobs if not job.get("background", False)), default=0)

    def _demand_order(self):
        order = self._read(self.root / ".demand-order.json")
        if order is None:
            return 0
        if type(order) is not int or order < 0:
            raise StoreError("invalid demand request order")
        return order

    def foreground_order(self):
        with self.lock(shared=True):
            return max(self._demand_order(), self._foreground_order(self._jobs()))

    def _next_request_order(self, jobs, *, minimum=0):
        order = max(self._demand_order(), self._foreground_order(jobs), minimum) + 1
        self._write(self.root / ".demand-order.json", order)
        return order

    def summary(self):
        states = ("queued", "running", "ready", "failed")
        with self.lock(shared=True):
            jobs = self._jobs()
            recipes = {}
            for path in sorted((self.root / "recipes").glob("*.json")):
                if not path.stem.isdecimal():
                    continue
                recipe = self._available_recipe(int(path.stem))
                if recipe is not None:
                    recipes[int(path.stem)] = recipe
            rows = {}
            for challenge_id, recipe in recipes.items():
                rows[challenge_id] = {
                    "challenge_id": challenge_id,
                    "version": recipe["version"],
                    "image": recipe["image"],
                    **dict.fromkeys(states, 0),
                    "obsolete": 0,
                }
            for job in jobs:
                row = rows.get(job["challenge_id"])
                if row is None:
                    continue
                state = (
                    job["state"]
                    if recipes[job["challenge_id"]] == job["recipe"] and not job.get("obsolete")
                    else "obsolete"
                )
                row[state] += 1
            counts = {state: sum(job["state"] == state for job in jobs) for state in states}
            payload_bytes = self._payload_bytes(jobs)
            now = time.time()
            pending = [job for job in jobs if job["state"] == "queued"]
            oldest = min((job["created"] for job in pending), default=now)
            active = [job for job in jobs if job["state"] in ("queued", "running")]
            background_pending = sum(job.get("background", False) for job in active)
            return {
                "counts": counts,
                "jobs": len(jobs),
                "max_jobs": self.max_jobs,
                "pending": counts["queued"] + counts["running"],
                "max_pending": self.max_pending,
                "background_pending": background_pending,
                "demand_pending": len(active) - background_pending,
                "max_background_pending": self.max_pending // 2,
                "max_bytes": self.max_bytes,
                "payload_bytes": payload_bytes,
                "oldest_wait_seconds": max(0, int(now - oldest)),
                "retention_seconds": self.retention_seconds,
                "configured_challenges": len(rows),
                "challenges": [rows[key] for key in sorted(rows)[:100]],
                "omitted_challenges": max(0, len(rows) - 100),
            }

    def request(
        self,
        challenge_id,
        owner,
        fingerprint,
        environment,
        *,
        expected_recipe=None,
        background=False,
        observed_order=None,
    ):
        if observed_order is not None and (type(observed_order) is not int or observed_order < 0):
            raise StoreError("invalid observed request order")
        if not isinstance(owner, str) or not _OWNER.fullmatch(owner):
            raise StoreError("invalid artifact owner")
        if not isinstance(fingerprint, str) or not _KEY.fullmatch(fingerprint):
            raise StoreError("invalid identity fingerprint")
        if not isinstance(environment, dict) or not 1 <= len(environment) <= 16:
            raise StoreError("invalid generator environment")
        for name, value in environment.items():
            if (
                not isinstance(name, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
                or not isinstance(value, str)
                or "\0" in value
                or len(value) > 8192
            ):
                raise StoreError("invalid generator environment")
        if len(canonical(environment)) > 16384:
            raise StoreError("invalid generator environment")
        recipe = self.get_recipe(challenge_id)
        if recipe is None or (expected_recipe is not None and recipe != expected_recipe):
            raise RecipeChanged("artifact configuration changed, reload the challenge")
        key = hashlib.sha256(canonical([PROTOCOL_VERSION, challenge_id, owner, fingerprint, recipe])).hexdigest()
        path = self._job_path(key)
        job = self._read(path)
        kind = owner.split(":", 1)[0]
        promote = not background and job and job.get("background", False) and job["state"] in ("queued", "running")
        if self._reusable(job) and not promote and self._owner_mode_current(kind):
            return job

        with self.lock():
            if self.get_recipe(challenge_id) != recipe:
                raise RecipeChanged("artifact configuration changed, reload the challenge")
            jobs = None
            if not self._owner_mode_current(kind):
                jobs = self._reconcile_owner_mode(
                    kind, self._jobs(), background=background, observed_order=observed_order
                )
            job = self._read(path)
            if self._reusable(job):
                if not background and job.get("background", False) and job["state"] in ("queued", "running"):
                    job.update(background=False, requested_at=self._next_request_order(self._jobs()))
                    self._write(path, job)
                return job
            if jobs is None:
                jobs = self._jobs()
            if background and any(
                item["challenge_id"] == challenge_id
                and item["owner"] == owner
                and item["key"] != key
                and not item.get("background", False)
                and (
                    item["state"] in ("queued", "running")
                    or (
                        observed_order is not None
                        and item["state"] == "ready"
                        and self._request_order(item) > observed_order
                    )
                )
                for item in jobs
            ):
                raise QueueFull("artifact identity has an active download, retry later")
            previous_order = self._foreground_order(jobs)
            if not background:
                previous_order = max(previous_order, self._demand_order())
            jobs = self._retire_obsolete(challenge_id, recipe, jobs)
            jobs = self._retire_identity(challenge_id, owner, key, jobs)
            if (self.max_jobs and len(jobs) >= self.max_jobs) or any(item.get("obsolete") for item in jobs):
                self._collect(evict=not background)
                jobs = self._jobs()
            catalog_full = self.max_jobs and not job and len(jobs) >= self.max_jobs
            pending = [item for item in jobs if item["state"] in ("queued", "running")]
            pending_limit = self.max_pending // 2 if background else self.max_pending
            if catalog_full or len(pending) >= pending_limit:
                raise QueueFull("artifact queue is full, retry later")
            if background and self.max_bytes:
                reserved = sum(item["recipe"]["max_output_bytes"] for item in pending)
                payload_bytes = self._payload_bytes([item for item in jobs if item["key"] != key])
                if payload_bytes + reserved + recipe["max_output_bytes"] > self.max_bytes:
                    raise QueueFull("artifact storage is full")
            now = time.time()
            if job:
                job.update(state="queued", attempts=0, next_attempt=0, created=now, updated=now, error=None)
            else:
                job = {
                    "key": key,
                    "challenge_id": challenge_id,
                    "owner": owner,
                    "recipe": recipe,
                    "environment": environment.copy(),
                    "state": "queued",
                    "attempts": 0,
                    "created": now,
                    "updated": now,
                    "next_attempt": 0,
                }
            job["background"] = background
            if background:
                job.pop("requested_at", None)
            else:
                job["requested_at"] = self._next_request_order(jobs, minimum=previous_order)
            job.pop("obsolete", None)
            self._write(path, job)
            return job

    def _complete(self, job):
        directory = self.root / "artifacts" / job["key"]
        return all(
            (directory / name).is_file() and not (directory / name).is_symlink() for name in job["recipe"]["outputs"]
        )

    def open_file(self, job, filename):
        if filename not in job["recipe"]["outputs"] or not _NAME.fullmatch(filename):
            raise ArtifactUnavailable("artifact file is unavailable")
        job_path = self._job_path(job["key"])
        current = self._read(job_path)
        generation = current.get("generation") if current else None
        tracked = isinstance(generation, str) and len(generation) == 32
        lock = nullcontext() if tracked else self.lock(shared=True)  # legacy jobs lack replacement generations
        with lock:
            if not tracked:
                current = self._read(job_path)
            if not current or current["state"] != "ready" or self.get_recipe(job["challenge_id"]) != job["recipe"]:
                return None
            path = self.root / "artifacts" / job["key"] / filename
            try:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return None
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ArtifactUnavailable("artifact file is unavailable")
                if tracked and (
                    self._read(job_path) != current or self.get_recipe(job["challenge_id"]) != job["recipe"]
                ):
                    return None
                handle = os.fdopen(descriptor, "rb")
                descriptor = None
                return handle
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def interrupted(self):
        with self.lock():
            return [job for job in self._jobs() if job["state"] == "running"]

    def current(self, job):
        with self.lock(shared=True):
            current = self._read(self._job_path(job["key"]))
            return (
                current is not None
                and current["state"] == "running"
                and current["attempts"] == job["attempts"]
                and current.get("generation") == job.get("generation")
                and self._available_recipe(job["challenge_id"]) == job["recipe"]
            )

    def fail(self, job, message="generation failed"):
        with self.lock():
            current = self._read(self._job_path(job["key"]))
            if (
                current is None
                or current["state"] != "running"
                or current["attempts"] != job["attempts"]
                or current.get("generation") != job.get("generation")
            ):
                return
            changed = self._available_recipe(current["challenge_id"]) != current["recipe"]
            terminal = current["attempts"] >= 3 or changed
            current.update(
                state="failed" if terminal else "queued",
                error="configuration changed" if changed else message,
                updated=time.time(),
                next_attempt=time.time() + 5 * 2 ** current["attempts"],
            )
            self._write(self._job_path(job["key"]), current)

    def collect(self, *, required_bytes=0, evict=True):
        with self.lock():
            self._collect(required_bytes=required_bytes, evict=evict)

    def _collect(self, *, required_bytes=0, evict=True):
        for directory in (self.root / "recipes", self.root / "jobs"):
            for temporary in directory.glob(".write-*"):
                temporary.unlink()
        jobs = self._jobs()
        now = time.time()
        usage = {}
        if self.max_bytes:
            for job in jobs:
                directory = self.root / "artifacts" / job["key"]
                usage[job["key"]] = sum(path.stat().st_size for path in directory.glob("*") if path.is_file())
        total = sum(usage.values())
        candidates = sorted(
            (job for job in jobs if job["state"] not in ("queued", "running")), key=lambda job: job["updated"]
        )
        remaining = len(jobs)
        recipes = {}
        for job in candidates:
            expired = self.retention_seconds is not None and now - job["updated"] >= self.retention_seconds
            challenge_id = job["challenge_id"]
            if challenge_id not in recipes:
                recipes[challenge_id] = self._available_recipe(challenge_id)
            obsolete = recipes[challenge_id] != job["recipe"] or job.get("obsolete")
            pressure = evict and (
                (self.max_bytes and total + required_bytes > self.max_bytes)
                or (self.max_jobs and remaining >= self.max_jobs)
            )
            if not expired and not obsolete and not pressure:
                continue
            remove_if_exists(self.root / "artifacts" / job["key"])
            self._job_path(job["key"]).unlink(missing_ok=True)
            total -= usage.get(job["key"], 0)
            remaining -= 1
        if self.max_bytes and total + required_bytes > self.max_bytes:
            raise QueueFull("artifact storage is full")

    def claim(self):
        with self.lock(shared=True):
            pending = any(job["state"] == "queued" and job["next_attempt"] <= time.time() for job in self._jobs())
        if not pending:
            return None

        with self.lock():
            jobs = self._jobs()
            candidates = sorted(
                (job for job in jobs if job["state"] == "queued" and job["next_attempt"] <= time.time()),
                key=lambda job: (
                    job.get("background", False),
                    job["created"] if job.get("background", False) else self._request_order(job),
                    job["key"],
                ),
            )
            payload_bytes = (
                self._payload_bytes(jobs)
                if self.max_bytes and any(job.get("background", False) for job in candidates)
                else 0
            )
            available_bytes = self.available_space()
            for job in candidates:
                if self._available_recipe(job["challenge_id"]) != job["recipe"]:
                    job.update(state="failed", error="configuration changed", updated=time.time())
                    self._write(self._job_path(job["key"]), job)
                    continue
                if job["recipe"]["max_output_bytes"] > available_bytes:
                    continue
                if job.get("background", False) and self.max_bytes:
                    required = payload_bytes - self._payload_bytes([job]) + job["recipe"]["max_output_bytes"]
                    if required > self.max_bytes:
                        continue
                job.update(
                    state="running", attempts=job["attempts"] + 1, updated=time.time(), generation=secrets.token_hex(16)
                )
                self._write(self._job_path(job["key"]), job)
                return job
        return None

    def publish(self, job, stage):
        stage = Path(stage)
        outputs = job["recipe"]["outputs"]
        if set(path.name for path in stage.iterdir()) != set(outputs):
            raise ArtifactUnavailable("generator output does not match configured filenames")
        files = [stage / name for name in outputs]
        if any(not path.is_file() or path.is_symlink() for path in files):
            raise ArtifactUnavailable("generator output must contain regular files")
        if sum(path.stat().st_size for path in files) > job["recipe"]["max_output_bytes"]:
            raise ArtifactUnavailable("generator output exceeds its byte limit")
        for path in files:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        self._sync_directory(stage)
        with self.lock():
            current = self._read(self._job_path(job["key"]))
            if (
                not current
                or current["state"] != "running"
                or current["attempts"] != job["attempts"]
                or current.get("generation") != job.get("generation")
            ):
                raise RecipeChanged("generation is no longer current")
            if self._available_recipe(job["challenge_id"]) != job["recipe"]:
                raise RecipeChanged("artifact configuration changed")
            target = self.root / "artifacts" / job["key"]
            remove_if_exists(target)
            os.replace(stage, target)
            self._sync_directory(target.parent)
            current.update(state="ready", updated=time.time(), error=None)
            self._write(self._job_path(job["key"]), current)
