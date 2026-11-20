"""GPU admission regressions using in-memory locks/VRAM, never a real GPU/lock."""
import ast
import asyncio
import builtins
import contextlib
import contextvars
import importlib.util
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def _load_gate():
    name = "_v1_gpu_admission_test_gate"
    spec = importlib.util.spec_from_file_location(name, BACKEND / "v1_gpu_gatekeeper.py")
    gate = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: gate, "v1_gpu_gatekeeper": gate, "backend.v1_gpu_gatekeeper": gate}):
        spec.loader.exec_module(gate)
    return gate


class _StopRequested(RuntimeError):
    pass


class _Handle:
    def __init__(self):
        self.closed = False

    def fileno(self):
        return id(self)

    def seek(self, offset):
        pass

    def close(self):
        self.closed = True


class _AdmissionDouble:
    """Replace every external GPU/lock/settings operation before entering gate."""
    def __init__(self, *, busy=False, safe=True, stop_at=None):
        self.gate = _load_gate()
        self.now = 0.0
        self.busy = busy
        self.safe = safe
        self.stop_at = stop_at
        self.predicate_calls = []
        self.lock_calls = []
        self.handles = []
        self.stop = contextvars.ContextVar("gpu_admission_test_stop", default=self.should_stop)
        self.gate.os = SimpleNamespace(name="nt")
        self.gate.time = SimpleNamespace(monotonic=lambda: self.now, sleep=self.sleep)
        self.gate.gc = SimpleNamespace(collect=lambda: None)
        self.gate.GPU_LOCK_FILE = SimpleNamespace(parent=SimpleNamespace(mkdir=lambda **kwargs: None))
        self.gate.open = self.open
        self.gate.logger = Mock()
        self.vram_check = Mock(side_effect=lambda **kwargs: (self.safe, "mock VRAM pressure"))
        self.modules = {
            "batch_control": SimpleNamespace(stop_check=self.stop),
            "msvcrt": SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=self.locking),
            "worker_settings": SimpleNamespace(get_worker_settings=lambda: {"auto_vram_guard": True}),
            "v1_vram_monitor": SimpleNamespace(get_vram_stats=lambda: {}, is_vram_safe_for_gpu_step=self.vram_check),
            "torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False, is_initialized=lambda: False)),
        }

    def should_stop(self):
        self.predicate_calls.append(self.now)
        return self.stop_at is not None and self.now >= self.stop_at

    def sleep(self, seconds):
        self.now += seconds

    def open(self, *args, **kwargs):
        handle = _Handle()
        self.handles.append(handle)
        return handle

    def locking(self, fd, mode, size):
        self.lock_calls.append(mode)
        if mode == 1 and self.busy:
            raise OSError("mock other GPU holder")

    def __enter__(self):
        self.patch = patch.dict(sys.modules, self.modules)
        self.patch.__enter__()
        return self

    def __exit__(self, *args):
        return self.patch.__exit__(*args)


class ScopedGPUStopTests(unittest.TestCase):
    def test_busy_gpu_poll_observes_scoped_stop_before_deadline(self):
        with _AdmissionDouble(busy=True, stop_at=0.5) as env:
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                with env.gate.gpu_gatekeeper(timeout_seconds=3):
                    self.fail("stopped admission entered body")
            self.assertEqual(env.now, 0.5)
            self.assertEqual(env.lock_calls.count(1), 1)
            self.assertTrue(all(h.closed for h in env.handles))
            self.assertIsNone(env.gate._CURRENT_HOLDER)

    def test_unsafe_vram_poll_observes_scoped_stop_before_deadline(self):
        with _AdmissionDouble(safe=False, stop_at=0.5) as env:
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                with env.gate.gpu_gatekeeper(timeout_seconds=3):
                    self.fail("stopped admission entered body")
            self.assertLessEqual(env.now, 1)
            self.assertEqual(env.vram_check.call_count, 1)
            self.assertEqual(env.lock_calls.count(2), 1)
            self.assertTrue(all(h.closed for h in env.handles))

    def test_stop_already_set_does_not_open_or_lock(self):
        with _AdmissionDouble(stop_at=0) as env:
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                with env.gate.gpu_gatekeeper():
                    self.fail("stopped admission entered body")
            self.assertEqual(env.handles, [])
            self.assertEqual(env.lock_calls, [])

    def test_stop_after_vram_check_closes_acquired_handle_without_body(self):
        with _AdmissionDouble() as env:
            def vram_check(**kwargs):
                env.stop_at = 0
                return True, ""
            env.vram_check.side_effect = vram_check
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                with env.gate.gpu_gatekeeper():
                    self.fail("late stop entered body")
            self.assertTrue(all(h.closed for h in env.handles))
            self.assertIsNone(env.gate._CURRENT_HOLDER)
            self.assertEqual(env.gate._RECURSION_DEPTH, 0)

    def test_stopped_reentrant_attempt_preserves_existing_owner(self):
        with _AdmissionDouble() as env:
            with env.gate.gpu_gatekeeper():
                owner = env.gate._CURRENT_HOLDER
                env.stop_at = 0
                with self.assertRaisesRegex(RuntimeError, "stop requested"):
                    with env.gate.gpu_gatekeeper():
                        self.fail("stopped nested admission entered body")
                self.assertEqual(env.gate._CURRENT_HOLDER, owner)
                self.assertEqual(env.gate._RECURSION_DEPTH, 1)
                self.assertFalse(env.handles[0].closed)
            self.assertTrue(env.handles[0].closed)
            self.assertIsNone(env.gate._CURRENT_HOLDER)

    def test_out_of_order_reentrant_exit_releases_when_last_lease_exits(self):
        with _AdmissionDouble() as env:
            env.gate.set_gpu_context_id("same-job")
            outer = env.gate.gpu_gatekeeper()
            inner = env.gate.gpu_gatekeeper()
            outer.__enter__()
            inner.__enter__()
            outer.__exit__(None, None, None)
            self.assertEqual(env.gate._RECURSION_DEPTH, 1)
            self.assertFalse(env.handles[0].closed)
            inner.__exit__(None, None, None)
            self.assertEqual(env.gate._RECURSION_DEPTH, 0)
            self.assertIsNone(env.gate._CURRENT_HOLDER)
            self.assertIsNone(env.gate._HELD_HANDLE)
            self.assertEqual(env.lock_calls, [1, 2])
            self.assertTrue(env.handles[0].closed)

    def test_failed_same_context_competing_attempt_does_not_release_owner(self):
        with _AdmissionDouble(busy=True, stop_at=0.5) as env:
            cid = env.gate.get_gpu_context_id()
            owner_handle = _Handle()
            def concurrent_open(*args, **kwargs):
                # Another same-context caller acquired after our reentry check.
                env.gate._CURRENT_HOLDER = cid
                env.gate._RECURSION_DEPTH = 1
                env.gate._HELD_HANDLE = owner_handle
                return env.open(*args, **kwargs)
            env.gate.open = concurrent_open
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                with env.gate.gpu_gatekeeper(timeout_seconds=3):
                    self.fail("stopped competing attempt entered body")
            self.assertEqual(env.gate._CURRENT_HOLDER, cid)
            self.assertEqual(env.gate._RECURSION_DEPTH, 1)
            self.assertIs(env.gate._HELD_HANDLE, owner_handle)
            self.assertFalse(owner_handle.closed)
            self.assertTrue(env.handles[0].closed)
            self.assertNotIn(2, env.lock_calls)
            env.gate._release_gpu_context(cid, "mock-owner")
            self.assertTrue(owner_handle.closed)

    def test_successful_nested_reentrancy_keeps_one_file_lease(self):
        with _AdmissionDouble() as env:
            with env.gate.gpu_gatekeeper():
                with env.gate.gpu_gatekeeper():
                    self.assertEqual(env.gate._RECURSION_DEPTH, 2)
                    self.assertEqual(len(env.handles), 1)
                self.assertEqual(env.gate._RECURSION_DEPTH, 1)
            self.assertEqual(env.lock_calls, [1, 2])
            self.assertTrue(env.handles[0].closed)


class GPUCleanupTests(unittest.TestCase):
    def test_cleanup_does_not_import_torch_when_parent_has_not_loaded_it(self):
        with _AdmissionDouble() as env:
            imports = []
            original_import = builtins.__import__
            def guarded_import(name, *args, **kwargs):
                if name == "torch":
                    imports.append(name)
                    raise AssertionError("cleanup must not cold-import torch")
                return original_import(name, *args, **kwargs)
            with patch.dict(sys.modules):
                sys.modules.pop("torch", None)
                with patch.object(builtins, "__import__", guarded_import):
                    with env.gate.gpu_gatekeeper():
                        pass
            self.assertEqual(imports, [])
            self.assertTrue(env.handles[0].closed)

    def test_loaded_uninitialized_torch_is_not_probed_or_initialized(self):
        with _AdmissionDouble() as env:
            cuda = SimpleNamespace(
                is_initialized=Mock(return_value=False),
                is_available=Mock(side_effect=AssertionError("must not probe CUDA driver")),
                empty_cache=Mock(),
            )
            sys.modules["torch"] = SimpleNamespace(cuda=cuda)
            with env.gate.gpu_gatekeeper():
                pass
            cuda.is_initialized.assert_called_once_with()
            cuda.is_available.assert_not_called()
            cuda.empty_cache.assert_not_called()

    def test_initialized_cuda_cleanup_keeps_file_lease_without_process_mutex(self):
        with _AdmissionDouble() as env:
            mutex_available = []
            lease_still_held = []
            def empty_cache():
                lease_still_held.append(not env.handles[0].closed)
                def probe():
                    acquired = env.gate._PROCESS_LOCK.acquire(timeout=0.05)
                    mutex_available.append(acquired)
                    if acquired:
                        env.gate._PROCESS_LOCK.release()
                thread = threading.Thread(target=probe)
                thread.start()
                thread.join(1)
                self.assertFalse(thread.is_alive())
            cuda = SimpleNamespace(is_initialized=Mock(return_value=True), is_available=Mock(return_value=True), empty_cache=Mock(side_effect=empty_cache))
            sys.modules["torch"] = SimpleNamespace(cuda=cuda)
            with env.gate.gpu_gatekeeper():
                pass
            self.assertEqual(mutex_available, [True])
            self.assertEqual(lease_still_held, [True])
            cuda.is_initialized.assert_called_once_with()
            cuda.is_available.assert_not_called()
            cuda.empty_cache.assert_called_once_with()
            self.assertTrue(env.handles[0].closed)
            self.assertEqual(env.lock_calls, [1, 2])


class AsyncGPUStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.gate = _load_gate()
        self.stop_var = contextvars.ContextVar("gpu_async_test_stop", default=None)
        self.modules = patch.dict(sys.modules, {"batch_control": SimpleNamespace(stop_check=self.stop_var)})
        self.modules.__enter__()
        self.addCleanup(self.modules.__exit__, None, None, None)

    async def _wait_for(self, predicate):
        for _ in range(400):
            if predicate():
                return
            await asyncio.sleep(0.002)
        self.fail("mock synchronization did not complete")

    async def test_stage2_blocked_guard_keeps_heartbeat_and_stop_prevents_admission(self):
        tree = ast.parse((BACKEND / "v1_orchestrator.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "V1Orchestrator")
        stage = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_stage_separate")
        namespace = {"asyncio": asyncio, "Path": Path, "logger": Mock()}
        exec(compile(ast.Module(body=[stage], type_ignores=[]), str(BACKEND / "v1_orchestrator.py"), "exec"), namespace)
        guard_entered, guard_returned, allow_guard = threading.Event(), threading.Event(), threading.Event()
        stopped = False
        heartbeat_during_guard = []
        def blocked_guard(stage_name):
            guard_entered.set()
            allow_guard.wait(0.5)
            guard_returned.set()
        def check_stop():
            if stopped:
                raise _StopRequested("stop requested while guard was blocked")
        async def noop(*args, **kwargs):
            pass
        async def heartbeat():
            nonlocal stopped
            await self._wait_for(guard_entered.is_set)
            heartbeat_during_guard.append(not guard_returned.is_set())
            stopped = True
            allow_guard.set()
        @contextlib.asynccontextmanager
        async def unexpected_admission(*args, **kwargs):
            self.fail("stopped guard must not begin GPU admission")
            yield
        separator = Mock(side_effect=AssertionError("native separator must not start"))
        run_stage = Mock(side_effect=AssertionError("stage work must not start"))
        context = SimpleNamespace(
            out_dir=Path("mock-output"), check_stop=check_stop, pause_checkpoint=noop,
            can_resume=False, effective_cfg={"separation_mode": "roformer"}, planned={},
            resolved_mode="SHORT", visual_sub_to_speech=False, notify=noop,
            job_id="mock-guard-job", video_dur_s=10, run_stage=run_stage, artifacts_acc={},
        )
        manifest = Mock()
        orchestrator = SimpleNamespace(manifest_manager=manifest, _calculate_stage_timeout=lambda *args: 2, _check_gpu_guard=blocked_guard)
        with patch.dict(sys.modules, {"v1_gpu_gatekeeper": SimpleNamespace(async_gpu_gatekeeper=unexpected_admission), "video_utils": SimpleNamespace(separate_vocals_demucs=separator), "job_tracker": Mock()}):
            pulse = asyncio.create_task(heartbeat())
            try:
                with self.assertRaisesRegex(_StopRequested, "while guard was blocked"):
                    await namespace["_stage_separate"](orchestrator, context, Path("mock-input.wav"))
            finally:
                allow_guard.set()
                await pulse
        self.assertEqual(heartbeat_during_guard, [True])
        self.assertTrue(guard_returned.is_set())
        run_stage.assert_not_called()
        separator.assert_not_called()
        manifest.record_stage_complete.assert_not_called()
        manifest.record_stage_failure.assert_called_once()

    async def test_stage2_stop_during_admission_never_starts_separator(self):
        tree = ast.parse((BACKEND / "v1_orchestrator.py").read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "V1Orchestrator")
        stage = next(node for node in cls.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_stage_separate")
        namespace = {"asyncio": asyncio, "Path": Path, "logger": Mock()}
        exec(compile(ast.Module(body=[stage], type_ignores=[]), str(BACKEND / "v1_orchestrator.py"), "exec"), namespace)
        entered, allow = threading.Event(), threading.Event()
        stopped = False
        observed = []
        released = []
        @contextlib.contextmanager
        def fake_gate(**kwargs):
            entered.set()
            self.assertTrue(allow.wait(2))
            try:
                yield
            finally:
                released.append(True)
        def check_stop():
            if stopped:
                observed.append(True)
                raise _StopRequested("stage2 stopped while queued")
        async def noop(*args, **kwargs):
            pass
        run_stage = Mock(side_effect=AssertionError("separator must not start after stop"))
        separator = Mock(side_effect=AssertionError("native separator must not start"))
        context = SimpleNamespace(
            out_dir=Path("mock-output"), check_stop=check_stop, pause_checkpoint=noop,
            can_resume=False, effective_cfg={"separation_mode": "demucs"}, planned={},
            resolved_mode="SHORT", visual_sub_to_speech=False, notify=noop,
            job_id="mock-stage2-job", video_dur_s=10, run_stage=run_stage, artifacts_acc={},
        )
        manifest = Mock()
        orchestrator = SimpleNamespace(manifest_manager=manifest, _calculate_stage_timeout=lambda *args: 2, _check_gpu_guard=Mock())
        with patch.dict(sys.modules, {"v1_gpu_gatekeeper": self.gate, "video_utils": SimpleNamespace(separate_vocals_demucs=separator), "job_tracker": Mock()}), patch.object(self.gate, "gpu_gatekeeper", fake_gate):
            task = asyncio.create_task(namespace["_stage_separate"](orchestrator, context, Path("mock-input.wav")))
            try:
                await self._wait_for(entered.is_set)
                stopped = True
                await self._wait_for(lambda: bool(observed))
                self.assertFalse(task.done())
            finally:
                allow.set()
            with self.assertRaisesRegex(_StopRequested, "stage2 stopped"):
                await task
        run_stage.assert_not_called()
        separator.assert_not_called()
        manifest.record_stage_complete.assert_not_called()
        manifest.record_stage_failure.assert_called_once()
        self.assertEqual(released, [True])

    async def test_same_context_async_child_outliving_parent_releases_last_lease(self):
        with _AdmissionDouble() as env:
            env.gate.set_gpu_context_id("shared-job")
            inner_entered, allow_inner_exit = asyncio.Event(), asyncio.Event()
            async def child():
                async with env.gate.async_gpu_gatekeeper():
                    inner_entered.set()
                    await allow_inner_exit.wait()
            async with env.gate.async_gpu_gatekeeper():
                task = asyncio.create_task(child())
                await inner_entered.wait()
                self.assertEqual(env.gate._RECURSION_DEPTH, 2)
            self.assertFalse(env.handles[0].closed)
            self.assertEqual(env.gate._RECURSION_DEPTH, 1)
            allow_inner_exit.set()
            await task
            self.assertTrue(env.handles[0].closed)
            self.assertEqual(env.lock_calls, [1, 2])
            self.assertIsNone(env.gate._CURRENT_HOLDER)
            self.assertIsNone(env.gate._HELD_HANDLE)

    async def test_repeated_cancellation_retains_os_lease_until_blocked_gc_finishes(self):
        with _AdmissionDouble() as env:
            gc_entered, allow_gc = threading.Event(), threading.Event()
            def collect():
                gc_entered.set()
                self.assertTrue(allow_gc.wait(2))
            env.gate.gc = SimpleNamespace(collect=collect)
            async def owner():
                async with env.gate.async_gpu_gatekeeper():
                    pass
            task = asyncio.create_task(owner())
            try:
                await self._wait_for(gc_entered.is_set)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertFalse(env.handles[0].closed)
            finally:
                allow_gc.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(env.handles[0].closed)
            self.assertEqual(env.lock_calls, [1, 2])
            self.assertIsNone(env.gate._CURRENT_HOLDER)
            self.assertIsNone(env.gate._HELD_HANDLE)

    async def test_waiting_admission_can_finish_stop_while_prior_gc_remains_blocked(self):
        with _AdmissionDouble() as env:
            env.gate.time = SimpleNamespace(monotonic=time.monotonic, sleep=time.sleep)
            owner = env.gate.gpu_gatekeeper()
            owner.__enter__()
            gc_entered, allow_gc = threading.Event(), threading.Event()
            def collect():
                gc_entered.set()
                self.assertTrue(allow_gc.wait(2))
            env.gate.gc = SimpleNamespace(collect=collect)
            env.busy = True
            stopped = False
            observed_stop = False
            def check_stop():
                if stopped:
                    raise _StopRequested("queued stop during prior cleanup")
            async def waiting_admission():
                async with env.gate.async_gpu_gatekeeper(timeout_seconds=3, check_stop=check_stop):
                    self.fail("busy mock must not admit another GPU body")
            release = asyncio.create_task(asyncio.to_thread(owner.__exit__, None, None, None))
            waiting = None
            try:
                await self._wait_for(gc_entered.is_set)
                waiting = asyncio.create_task(waiting_admission())
                await asyncio.sleep(0.02)
                stopped = True
                done, _ = await asyncio.wait({waiting}, timeout=0.8)
                if waiting in done:
                    with self.assertRaisesRegex(_StopRequested, "during prior cleanup"):
                        await waiting
                    observed_stop = True
                lease_held_during_gc = not env.handles[0].closed
            finally:
                allow_gc.set()
                await release
                if waiting is not None and not observed_stop:
                    with self.assertRaises(_StopRequested):
                        await waiting
            self.assertTrue(observed_stop, "queued cancellation must finish before unrelated GC unblocks")
            self.assertTrue(lease_held_during_gc, "keep OS lease through local CUDA cleanup")
            self.assertTrue(env.handles[0].closed)

    async def test_explicit_stop_prevents_async_acquisition(self):
        gate = Mock()
        def check_stop():
            raise _StopRequested("requested")
        with patch.object(self.gate, "gpu_gatekeeper", gate):
            with self.assertRaises(_StopRequested):
                async with self.gate.async_gpu_gatekeeper(check_stop=check_stop):
                    self.fail("stopped caller entered body")
        gate.assert_not_called()

    async def test_stop_while_waiting_drains_and_releases_late_acquisition(self):
        entered, allow = threading.Event(), threading.Event()
        stopped = False
        stop_observed = []
        calls = []
        @contextlib.contextmanager
        def fake_gate(**kwargs):
            entered.set()
            self.assertTrue(allow.wait(2))
            calls.append("acquired")
            try:
                yield
            finally:
                calls.append("released")
        def check_stop():
            if stopped:
                stop_observed.append(True)
                raise _StopRequested("requested while queued")
        async def owner():
            async with self.gate.async_gpu_gatekeeper(check_stop=check_stop):
                self.fail("stopped caller entered body")
        with patch.object(self.gate, "gpu_gatekeeper", fake_gate):
            task = asyncio.create_task(owner())
            try:
                await self._wait_for(entered.is_set)
                stopped = True
                await self._wait_for(lambda: bool(stop_observed))
                self.assertFalse(task.done(), "late acquire must drain before owner returns")
            finally:
                allow.set()
            with self.assertRaisesRegex(_StopRequested, "while queued"):
                await task
        self.assertEqual(calls, ["acquired", "released"])

    async def test_stop_after_acquire_releases_without_entering_body(self):
        stopped = False
        calls = []
        @contextlib.contextmanager
        def fake_gate(**kwargs):
            nonlocal stopped
            calls.append("acquired")
            stopped = True
            try:
                yield
            finally:
                calls.append("released")
        def check_stop():
            if stopped:
                raise _StopRequested("late stop")
        with patch.object(self.gate, "gpu_gatekeeper", fake_gate):
            with self.assertRaisesRegex(_StopRequested, "late stop"):
                async with self.gate.async_gpu_gatekeeper(check_stop=check_stop):
                    self.fail("late stop entered body")
        self.assertEqual(calls, ["acquired", "released"])

    async def test_repeated_cancellation_during_acquisition_drains_and_releases(self):
        entered, allow = threading.Event(), threading.Event()
        calls = []
        @contextlib.contextmanager
        def fake_gate(**kwargs):
            entered.set()
            self.assertTrue(allow.wait(2))
            calls.append("acquired")
            try:
                yield
            finally:
                calls.append("released")
        async def owner():
            async with self.gate.async_gpu_gatekeeper():
                self.fail("cancelled caller entered body")
        with patch.object(self.gate, "gpu_gatekeeper", fake_gate):
            task = asyncio.create_task(owner())
            try:
                await self._wait_for(entered.is_set)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                allow.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, ["acquired", "released"])

    async def test_repeated_cancellation_during_release_does_not_detach_exit(self):
        exit_started, allow_exit = threading.Event(), threading.Event()
        calls = []
        @contextlib.contextmanager
        def fake_gate(**kwargs):
            try:
                yield
            finally:
                exit_started.set()
                self.assertTrue(allow_exit.wait(2))
                calls.append("released")
        async def owner():
            async with self.gate.async_gpu_gatekeeper():
                calls.append("body")
        with patch.object(self.gate, "gpu_gatekeeper", fake_gate):
            task = asyncio.create_task(owner())
            try:
                await self._wait_for(exit_started.is_set)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            finally:
                allow_exit.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, ["body", "released"])

    async def test_acquisition_retries_share_one_advancing_deadline(self):
        now = [0.0]
        attempts = []
        real_sleep = asyncio.sleep
        @contextlib.contextmanager
        def unavailable(**kwargs):
            attempts.append(kwargs["timeout_seconds"])
            now[0] += 0.6
            raise TimeoutError("mock busy GPU")
            yield
        async def sleep(seconds):
            now[0] += seconds
            await real_sleep(0)
        with patch.object(self.gate, "gpu_gatekeeper", unavailable), patch.object(self.gate, "time", SimpleNamespace(monotonic=lambda: now[0])), patch.object(asyncio, "sleep", sleep):
            with self.assertRaises(TimeoutError):
                async with self.gate.async_gpu_gatekeeper(timeout_seconds=2):
                    self.fail("unavailable gate entered body")
        self.assertEqual(len(attempts), 2)
        self.assertLessEqual(now[0], 2.2)
        self.assertLess(attempts[1], attempts[0])


class OrchestratorAdmissionContractTests(unittest.TestCase):
    def test_all_native_gpu_admission_sites_forward_job_stop_callback(self):
        tree = ast.parse((BACKEND / "v1_orchestrator.py").read_text(encoding="utf-8"))
        sites = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "async_gpu_gatekeeper"]
        self.assertGreaterEqual(len(sites), 7)
        for site in sites:
            with self.subTest(line=site.lineno):
                callback = next((kw.value for kw in site.keywords if kw.arg == "check_stop"), None)
                self.assertIsInstance(callback, ast.Attribute)
                self.assertEqual(callback.attr, "check_stop")
                self.assertIsInstance(callback.value, ast.Name)
                self.assertEqual(callback.value.id, "c")


if __name__ == "__main__":
    unittest.main()
