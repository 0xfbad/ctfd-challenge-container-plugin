from __future__ import annotations

import logging
import os
import re
import select
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from contextlib import closing, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from requests.exceptions import ReadTimeout

if TYPE_CHECKING or __package__:
    from .store import ArtifactUnavailable, QueueFull, RecipeChanged, Store, StoreBusy, StoreError, remove_if_exists
else:
    from store import ArtifactUnavailable, QueueFull, RecipeChanged, Store, StoreBusy, StoreError, remove_if_exists

logger = logging.getLogger("personalized-files")
_EXIT = re.compile(rb"CTFD_ARTIFACT_EXIT:([0-9]+)\n\Z")
_WRAPPER = (
    '(sleep "$CTFD_ARTIFACT_TIMEOUT"; kill -KILL "$$") & '  # the worker can die before cleanup
    '/generate >/dev/null 2>&1; code=$?; printf "CTFD_ARTIFACT_EXIT:%s\\n" "$code"; while :; do sleep 1; done'
)


class GenerationError(StoreError):
    pass


def _wait_for_start(container, deadline, cancelled):
    while True:
        if cancelled.is_set():
            raise RecipeChanged("generation is no longer current")
        if time.monotonic() >= deadline:
            raise GenerationError("generation timed out")
        try:
            container.reload()
        except ReadTimeout:
            continue
        if cancelled.is_set():
            raise RecipeChanged("generation is no longer current")
        if time.monotonic() >= deadline:
            raise GenerationError("generation timed out")
        if container.attrs.get("State", {}).get("Error") or container.status not in ("created", "running"):
            raise GenerationError("generator failed to start")
        if container.status == "running":
            return
        time.sleep(0.2)


def _docker_chunks(connection, deadline, cancelled=None):
    reader = getattr(connection, "recv", None) or connection.read

    def read_exact(size, *, eof_allowed=False):
        result = bytearray()
        while len(result) < size:
            if cancelled is not None and cancelled.is_set():
                raise RecipeChanged("generation is no longer current")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GenerationError("generation timed out")
            wait = min(remaining, 0.2) if cancelled is not None else remaining
            readable, _, _ = select.select([connection], [], [], wait)  # sdk readers omit deadlines
            if not readable:
                continue
            chunk = reader(size - len(result))
            if not chunk:
                if not result and eof_allowed:
                    return None
                raise GenerationError("generator archive stream was interrupted")
            result.extend(chunk)
        return bytes(result)

    while True:
        header = read_exact(8, eof_allowed=True)
        if header is None:
            return
        if header[0] not in (1, 2) or header[1:4] != b"\0\0\0":
            raise GenerationError("invalid generator archive stream")
        remaining = int.from_bytes(header[4:], "big")
        while remaining:
            chunk = read_exact(min(remaining, 65536))
            remaining -= len(chunk)
            yield (chunk, None) if header[0] == 1 else (None, chunk)


class _TarStream:
    def __init__(self, chunks, byte_limit, deadline):
        self.chunks = iter(chunks)
        self.limit = byte_limit
        self.deadline = deadline
        self.total = 0
        self.pending = bytearray()

    def read(self, size):
        while len(self.pending) < size:
            if time.monotonic() >= self.deadline:
                raise GenerationError("generation timed out")
            try:
                stdout, stderr = next(self.chunks)
            except StopIteration:
                break
            if stderr:
                raise GenerationError("could not read generator output")
            if stdout:
                self.total += len(stdout)
                if self.total > self.limit:
                    raise GenerationError("generator archive exceeds its byte limit")
                self.pending.extend(stdout)
        result = bytes(self.pending[:size])
        del self.pending[:size]
        return result

    def drain(self):
        while self.read(65536):
            pass


def unpack_outputs(stream, stage, outputs, byte_limit):
    stage = Path(stage)
    expected = set(outputs)
    found = set()
    total = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            if member.name in (".", "./") and member.isdir():
                continue
            name = member.name.removeprefix("./")
            if name not in expected or name in found or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                raise GenerationError("generator must produce only the configured regular files")
            if member.sparse is not None or member.size < 0:
                raise GenerationError("generator must produce ordinary regular files")
            total += member.size
            if total > byte_limit:
                raise GenerationError("generator output exceeds its byte limit")
            source = archive.extractfile(member)
            if source is None:
                raise GenerationError("generator output is incomplete")
            with source, (stage / name).open("xb") as target:
                shutil.copyfileobj(source, target, length=65536)
            found.add(name)
    if found != expected:
        raise GenerationError("generator did not produce all configured files")


class DockerGenerator:
    def __init__(self, client=None, *, store=None):
        if client is None:
            import docker

            client = docker.from_env(timeout=5)
        self.client = client
        self.store = store
        self.phase = "idle"

    @staticmethod
    def container_name(job):
        name = "ctfd-personalized-" + job["key"]
        return name + "-" + job["generation"] if job.get("generation") else name

    def cleanup(self, job):
        from docker.errors import NotFound

        if self.store is not None and not job.get("generation"):
            return
        try:
            self.phase = "cleanup-lookup"
            container = self.client.containers.get(self.container_name(job))
        except NotFound:
            return
        labels = {"ctfd.personalized.key": job["key"]}
        if job.get("generation"):
            labels["ctfd.personalized.generation"] = job["generation"]
        if self.store is not None:
            labels["ctfd.personalized.store"] = self.store.namespace
        if any(container.labels.get(name) != value for name, value in labels.items()):
            raise GenerationError("generator container name is already in use")
        self.phase = "cleanup-remove"
        container.remove(force=True)

    def cleanup_all(self, jobs):
        keys = {job["key"] for job in jobs}
        label = f"ctfd.personalized.store={self.store.namespace}" if self.store is not None else "ctfd.personalized.key"
        self.phase = "cleanup-list"
        for container in self.client.containers.list(all=True, filters={"label": label}):
            scoped = self.store is not None and container.labels.get("ctfd.personalized.store") == self.store.namespace
            if scoped or (self.store is None and container.labels.get("ctfd.personalized.key") in keys):
                self.phase = "cleanup-remove"
                container.remove(force=True)

    def __call__(self, job, stage):
        self.phase = "check-current"
        if self.store is not None and not _retry_store(self.store.current, job):
            raise RecipeChanged("generation is no longer current")
        self.cleanup(job)
        recipe = job["recipe"]
        deadline = time.monotonic() + recipe["timeout_seconds"]
        self.phase = "image"
        image = self.client.images.get(recipe["image"])
        if image.attrs.get("Config", {}).get("Volumes"):
            raise GenerationError("generator images must not declare volumes")
        environment = {
            **job["environment"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "CTFD_ARTIFACT_TIMEOUT": str(recipe["timeout_seconds"]),
        }
        labels = {"ctfd.personalized.key": job["key"]}
        if job.get("generation"):
            labels["ctfd.personalized.generation"] = job["generation"]
        if self.store is not None:
            labels["ctfd.personalized.store"] = self.store.namespace
        self.phase = "create"
        container = self.client.containers.create(
            image=image.id,
            entrypoint=["/bin/sh"],
            command=["-c", _WRAPPER],
            name=self.container_name(job),
            environment=environment,
            working_dir="/output",
            user="65534:65534",
            read_only=True,
            network_mode="none",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=64,
            mem_limit=268435456,
            memswap_limit=268435456,
            nano_cpus=1000000000,
            init=True,
            healthcheck={"test": ["NONE"]},
            tmpfs={
                "/output": f"rw,noexec,nosuid,nodev,size={recipe['max_output_bytes']},mode=1777",
                "/tmp": "rw,noexec,nosuid,nodev,size=16777216,mode=1777",
            },
            labels=labels,
            log_config={"type": "json-file", "config": {"max-size": "16k", "max-file": "1"}},
        )

        finished = threading.Event()
        cancelled = threading.Event()

        def stop_obsolete_container():
            while not finished.wait(0.2):
                overdue = time.monotonic() >= deadline
                current = True
                if self.store is not None:
                    try:
                        current = self.store.current(job)
                    except StoreBusy:
                        pass
                    except (StoreError, OSError):
                        current = False
                if not overdue and current:
                    continue
                if not current:
                    cancelled.set()
                with suppress(Exception):
                    container.kill()
                return

        watchdog = threading.Thread(target=stop_obsolete_container, daemon=True)
        watchdog.start()
        failed = False
        try:
            self.phase = "start"
            try:
                container.start()
            except ReadTimeout:
                self.phase = "start-wait"
                _wait_for_start(container, deadline, cancelled)
            while True:
                if cancelled.is_set():
                    raise RecipeChanged("generation is no longer current")
                if time.monotonic() >= deadline:
                    raise GenerationError("generation timed out")
                self.phase = "logs"
                marker = _EXIT.fullmatch(container.logs(stdout=True, stderr=False, tail=1))
                if marker:
                    if int(marker.group(1)):
                        raise GenerationError("generator exited unsuccessfully")
                    break
                self.phase = "reload"
                container.reload()
                if container.status != "running":
                    raise GenerationError("generator stopped before completing")
                time.sleep(0.2)
            self.phase = "exec-create"
            execution = self.client.api.exec_create(
                container.id,
                cmd=["tar", "-C", "/output", "-cf", "-", "."],  # the archive api does not expose this tmpfs
                stdout=True,
                stderr=True,
                user="65534:65534",
            )["Id"]
            self.phase = "exec-start"
            with closing(self.client.api.exec_start(execution, socket=True)) as connection:
                self.phase = "archive"
                stream = _TarStream(
                    _docker_chunks(connection, deadline, cancelled), recipe["max_output_bytes"] + 65536, deadline
                )
                unpack_outputs(stream, stage, recipe["outputs"], recipe["max_output_bytes"])
                stream.drain()
                self.phase = "exec-inspect"
                if self.client.api.exec_inspect(execution)["ExitCode"] != 0:
                    raise GenerationError("could not read generator output")
                self.phase = "check-current"
                if cancelled.is_set() or (self.store is not None and not _retry_store(self.store.current, job)):
                    raise RecipeChanged("generation is no longer current")
                self.phase = "archive-close"
        except BaseException:
            failed = True
            raise
        finally:
            finished.set()
            watchdog.join(timeout=5)
            phase = self.phase
            self.phase = "cleanup-remove"
            try:
                container.remove(force=True)
            except Exception as exc:
                if not failed:
                    raise
                logger.warning(
                    "artifact cleanup failed for challenge %s (%s) phase=%s",
                    job["challenge_id"],
                    type(exc).__name__,
                    self.phase,
                )
            if failed:
                self.phase = phase


def _retry_store(operation, *args, **kwargs):
    deadline = time.monotonic() + 5
    while True:
        try:
            return operation(*args, **kwargs)
        except StoreBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def run_once(store, generator=None, *, maintenance=None):
    job = None
    try:
        with store.lock("worker"):
            generator = generator or DockerGenerator(store=store)
            if maintenance is None or time.monotonic() >= maintenance["next"]:
                reconcile = getattr(generator, "cleanup_all", None)
                if reconcile:
                    with store.lock(shared=True):
                        jobs = store._jobs()
                    reconcile(jobs)
                for interrupted in store.interrupted():
                    cleanup = getattr(generator, "cleanup", None)
                    if cleanup:
                        cleanup(interrupted)
                    store.fail(interrupted, "previous worker interrupted")
                for abandoned in (store.root / "staging").iterdir():
                    if abandoned.is_dir():
                        shutil.rmtree(abandoned)
                    else:
                        abandoned.unlink()
                with suppress(QueueFull):
                    store.collect(evict=False)
                if maintenance is not None:
                    maintenance["next"] = time.monotonic() + 60
            job = store.claim()
            if job is None:
                return False
            stage = Path(tempfile.mkdtemp(dir=store.root / "staging", prefix=job["key"] + "-"))
            phase = "prepare"
            try:
                remove_if_exists(store.root / "artifacts" / job["key"])
                if job.get("background", False):
                    _retry_store(store.collect, evict=False)
                else:
                    _retry_store(store.collect)
                phase = "storage"
                if store.available_space() < job["recipe"]["max_output_bytes"]:
                    raise GenerationError("artifact storage is full")
                phase = "generation"
                generator(job, stage)
                phase = "publish"
                _retry_store(store.publish, job, stage)
            except Exception as exc:
                if maintenance is not None:
                    maintenance["next"] = 0
                message = str(exc) if isinstance(exc, (GenerationError, ArtifactUnavailable)) else "generation failed"
                _retry_store(store.fail, job, message)
                logger.warning(
                    "artifact generation failed for challenge %s (%s) phase=%s",
                    job["challenge_id"],
                    type(exc).__name__,
                    generator.phase if phase == "generation" and isinstance(generator, DockerGenerator) else phase,
                )
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            return True
    except StoreBusy:
        if job is not None and maintenance is not None:
            maintenance["next"] = 0
        return False
    except Exception:
        if job is not None and maintenance is not None:
            maintenance["next"] = 0
        raise


def check():
    import docker

    with closing(docker.from_env(timeout=5)) as client:
        client.ping()
    root = os.environ.get("PERSONALIZED_FILES_ROOT", "/var/personalized-files")
    with tempfile.TemporaryFile(dir=root) as handle:
        handle.write(b"healthcheck")
        handle.flush()
        os.fsync(handle.fileno())


def main():
    logging.basicConfig(level=logging.INFO)
    store = Store(os.environ.get("PERSONALIZED_FILES_ROOT", "/var/personalized-files"))
    generator = None
    maintenance = {"next": 0}
    while True:
        try:
            generator = generator or DockerGenerator(store=store)
            if run_once(store, generator, maintenance=maintenance):
                continue
        except Exception as exc:
            maintenance["next"] = 0
            logger.error(
                "artifact worker unavailable (%s) docker_phase=%s",
                type(exc).__name__,
                generator.phase if generator is not None else "connect",
            )
        time.sleep(1)


if __name__ == "__main__":
    if sys.argv[1:] == ["--check"]:
        check()
    else:
        main()
