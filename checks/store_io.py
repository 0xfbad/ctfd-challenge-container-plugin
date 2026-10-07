# ruff: noqa: E402
from gevent import monkey

monkey.patch_all()

import ast
import errno
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import gevent
from gevent import get_hub
from gevent.threadpool import ThreadPool

SOURCE = Path(__file__).resolve().parents[1] / "src" / "files"
TREE = ast.parse((SOURCE / "web.py").read_text())
FUNCTION = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "_store_io")
NAMESPACE = {"get_hub": get_hub, "monkey": monkey}
exec(compile(ast.Module(body=[FUNCTION], type_ignores=[]), str(SOURCE / "web.py"), "exec"), NAMESPACE)
store_io = NAMESPACE["_store_io"]
SPEC = importlib.util.spec_from_file_location("_store_io_check_store", SOURCE / "store.py")
assert SPEC is not None and SPEC.loader is not None
store_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store_module)


def wait_until(condition):
    deadline = time.monotonic() + 5
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError("native operation did not reach expected state")
        gevent.sleep(0.005)


class StoreIoChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="store-io-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = store_module.Store(self.root)
        self.latch = monkey.get_original("_thread", "allocate_lock")()
        self.latch.acquire()

    def tearDown(self):
        if self.latch.locked():
            self.latch.release()
        gevent.sleep(0.01)

    def test_unpatched_process_calls_directly_and_preserves_errors(self):
        script = """import ast,sys
from gevent import monkey
assert not monkey.is_module_patched('threading')
tree=ast.parse(open(sys.argv[1]).read())
function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_store_io')
def no_hub(): raise AssertionError('unpatched execution must not create a hub')
namespace={'monkey':monkey,'get_hub':no_hub}
exec(compile(ast.Module(body=[function],type_ignores=[]),sys.argv[1],'exec'),namespace)
operation=namespace['_store_io']
token=object()
assert operation(lambda value:value,token) is token
error=RuntimeError('direct error')
def fail():raise error
try:operation(fail)
except RuntimeError as caught:assert caught is error
else:raise AssertionError('error was swallowed')
"""
        result = subprocess.run([sys.executable, "-c", script, str(SOURCE / "web.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_operation_and_arguments_preserve_caller_thread(self):
        caller = threading.get_native_id()
        token = object()
        result = store_io(lambda value, *, extra: (threading.get_native_id(), value, extra), token, extra=7)
        self.assertNotEqual(result[0], caller)
        self.assertIs(result[1], token)
        self.assertEqual(result[2], 7)
        self.assertEqual(threading.get_native_id(), caller)

    def test_uncancelled_native_error_propagates_unchanged(self):
        error = RuntimeError("native error")

        def fail():
            raise error

        with self.assertRaises(RuntimeError) as caught:
            store_io(fail)
        self.assertIs(caught.exception, error)

    def test_caught_operation_errors_never_reach_native_hub_callback(self):
        hub = get_hub()
        callbacks = []
        cause = ValueError("original cause")
        errors = [store_module.StoreBusy("busy"), store_module.QueueFull("full"), RuntimeError("unexpected")]

        def fail(error):
            raise error from cause

        with patch.object(hub, "handle_error", lambda *args: callbacks.append(args)):
            for error in errors:
                with self.subTest(error=type(error).__name__):
                    try:
                        store_io(fail, error)
                    except BaseException as caught:
                        self.assertIs(caught, error)
                        self.assertIs(caught.__cause__, cause)
                        self.assertTrue(caught.__suppress_context__)
                        frames = []
                        traceback = caught.__traceback__
                        while traceback is not None:
                            frames.append(traceback.tb_frame.f_code.co_name)
                            traceback = traceback.tb_next
                        self.assertIn("fail", frames)
                        self.assertIn("_store_io", frames)
                    else:
                        self.fail("operation error was swallowed")
            gevent.sleep(0.02)
        self.assertEqual(callbacks, [])

    def test_native_values_keep_identity_including_tuple_and_exception_data(self):
        token = object()
        error = RuntimeError("exception as data")
        values = [None, token, (token, error), error]
        callbacks = []
        with patch.object(get_hub(), "handle_error", lambda *args: callbacks.append(args)):
            for value in values:
                with self.subTest(value=type(value).__name__):
                    self.assertIs(store_io(lambda selected: selected, value), value)
            gevent.sleep(0.02)
        self.assertEqual(callbacks, [])

    def test_repeated_cancellation_keeps_preparation_lock_until_accepted_write_finishes(self):
        entered = self.root / "entered"
        path = self.root / "owned.json"

        def write():
            entered.write_text("entered")
            self.latch.acquire()
            self.latch.release()
            self.store._write(path, {"complete": True})

        def caller():
            with self.store.lock("preparation"):
                store_io(write)

        task = gevent.spawn(caller)
        self.addCleanup(task.join, timeout=5)
        wait_until(entered.exists)
        for _ in range(2):
            task.kill(block=False)
            gevent.sleep(0.02)
            self.assertFalse(task.dead)
            with self.assertRaises(store_module.StoreBusy), self.store.lock("preparation"):
                pass
        self.latch.release()
        task.join(timeout=5)
        self.assertTrue(task.dead)
        self.assertEqual(self.store._read(path), {"complete": True})
        with self.store.lock("preparation"):
            pass

    def test_queued_operation_retains_first_timeout_through_kills_and_native_error(self):
        pool = ThreadPool(1)
        self.addCleanup(pool.kill)
        blocked_started = self.root / "blocked"
        accepted = self.root / "accepted"
        finished = self.root / "finished"
        errors = []
        timeout = gevent.Timeout(0.02)
        self.addCleanup(timeout.cancel)

        def blocker():
            blocked_started.write_text("blocked")
            self.latch.acquire()
            self.latch.release()

        def operation():
            self.store._write(finished, {"complete": True})
            raise RuntimeError("accepted native error")

        spawn = pool.spawn

        def accept(selected_pool, *args, **kwargs):
            self.assertIs(selected_pool, pool)
            result = spawn(*args, **kwargs)
            accepted.write_text("accepted")
            return result

        def caller():
            try:
                with timeout:
                    store_io(operation)
            except BaseException as error:
                errors.append(error)

        blocked = pool.spawn(blocker)
        wait_until(blocked_started.exists)
        with patch.object(get_hub(), "threadpool", pool), patch.object(ThreadPool, "spawn", accept):
            task = gevent.spawn(caller)
            wait_until(accepted.exists)
            gevent.sleep(0.04)
            for _ in range(2):
                self.assertFalse(task.dead)
                self.assertFalse(finished.exists())
                task.kill(block=False)
                gevent.sleep(0.02)
            self.latch.release()
            task.join(timeout=5)
            blocked.get(timeout=5)
        self.assertEqual(errors, [timeout])
        self.assertEqual(self.store._read(finished), {"complete": True})

    def test_cancelled_unaccepted_file_handle_is_closed_once(self):
        path = self.root / "file.txt"
        path.write_bytes(b"exact file contents")
        entered = self.root / "opened"
        handles = []
        discarded = []

        def opened():
            handle = path.open("rb")
            handles.append(handle)
            entered.write_text("opened")
            self.latch.acquire()
            self.latch.release()
            return handle

        def discard(handle):
            handle.close()
            discarded.append(handle)

        task = gevent.spawn(store_io, opened, discard=discard)
        wait_until(entered.exists)
        descriptor = handles[0].fileno()
        for _ in range(2):
            task.kill(block=False)
            gevent.sleep(0.02)
            self.assertFalse(task.dead)
            self.assertFalse(handles[0].closed)
        self.latch.release()
        task.join(timeout=5)
        self.assertEqual(discarded, handles)
        self.assertTrue(handles[0].closed)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_actual_reader_close_error_preserves_exact_first_timeout(self):
        path = self.root / "invalidated.txt"
        path.write_bytes(b"private descriptor fault fixture")
        handles = []
        discarded = []
        disposal = []
        captured = []
        timeout = gevent.Timeout(0.02)
        self.addCleanup(timeout.cancel)

        def opened():
            handle = path.open("rb")
            handles.append(handle)
            self.latch.acquire()
            self.latch.release()
            return handle

        def discard(handle):
            discarded.append(handle)
            try:
                handle.close()
            except OSError as error:
                disposal.append(error)
                raise

        def caller():
            try:
                with self.store.lock("preparation"), timeout:
                    store_io(opened, discard=discard)
            except BaseException as error:
                captured.append(error)

        task = gevent.spawn(caller)
        self.addCleanup(task.join, timeout=5)
        wait_until(lambda: handles)
        descriptor = handles[0].fileno()
        gevent.sleep(0.04)
        for _ in range(2):
            task.kill(block=False)
            gevent.sleep(0.02)
            self.assertFalse(task.dead)
            with self.assertRaises(store_module.StoreBusy), self.store.lock("preparation"):
                pass
        os.close(descriptor)
        self.latch.release()
        task.join(timeout=5)
        self.assertEqual(discarded, handles)
        self.assertTrue(handles[0].closed)
        self.assertEqual(len(disposal), 1)
        self.assertEqual(disposal[0].errno, errno.EBADF)
        self.assertEqual(captured, [timeout])
        self.assertIs(timeout.__context__, disposal[0])
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        with self.store.lock("preparation"):
            pass

    def test_preaccept_timeout_never_leaves_an_operation_queued(self):
        pool = ThreadPool(1)
        self.addCleanup(pool.kill)
        entered = []
        queued = []
        rejected = []
        captured = []
        timeout = gevent.Timeout(0.02)
        self.addCleanup(timeout.cancel)

        def blocker():
            entered.append(True)
            self.latch.acquire()
            self.latch.release()

        def caller():
            try:
                with timeout:
                    store_io(lambda: rejected.append(True))
            except BaseException as error:
                captured.append(error)

        blocked = pool.spawn(blocker)
        wait_until(lambda: entered)
        accepted = pool.spawn(lambda: queued.append(True))
        with patch.object(get_hub(), "threadpool", pool):
            task = gevent.spawn(caller)
            self.addCleanup(task.join, timeout=5)
            gevent.sleep(0.01)
            self.assertFalse(task.dead)
            self.assertFalse(rejected)
            task.join(timeout=1)
            self.assertTrue(task.dead)
            self.assertEqual(captured, [timeout])
            self.latch.release()
            blocked.get(timeout=5)
            accepted.get(timeout=5)
            gevent.sleep(0.02)
        self.assertEqual(queued, [True])
        self.assertFalse(rejected)

    def test_recipe_ids_keeps_only_positive_decimal_names(self):
        for name in ["1.json", "002.json", "0.json", "-1.json", "named.json", ".write-temp", "3.txt"]:
            (self.root / "recipes" / name).write_text(json.dumps({}))
        self.assertEqual(set(store_io(self.store.recipe_ids)), {1, 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
