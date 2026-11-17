"""GPU scheduling contracts with real OS locks and no model/GPU inference."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import batch_control
import v1_gpu_gatekeeper as gpu
import v1_vram_monitor as vram
import worker_settings


async def wait_for_thread_event(event):
    async def poll():
        while not event.is_set():
            await asyncio.sleep(.01)
    await asyncio.wait_for(poll(), timeout=5)


@pytest.fixture(autouse=True)
def isolated_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu, "GPU_LOCK_FILE", tmp_path / "gpu.lock")
    monkeypatch.setattr(worker_settings, "get_worker_settings", lambda: {"auto_vram_guard": True})
    monkeypatch.setattr(vram, "is_vram_safe_for_gpu_step", lambda **kw: (True, "fixture"))
    monkeypatch.setattr(vram, "get_vram_stats", lambda: {})
    monkeypatch.setattr(gpu.gc, "collect", lambda: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    context_token = gpu._GPU_CONTEXT_ID.set("fixture-execution")
    scope_token = gpu._GPU_SCOPE.set(None)
    stop_token = batch_control.stop_check.set(None)
    try:
        yield
        assert gpu._CURRENT_HOLDER is None
        assert gpu._HELD_HANDLE is None
        assert gpu._RECURSION_DEPTH == 0
    finally:
        batch_control.stop_check.reset(stop_token)
        gpu._GPU_SCOPE.reset(scope_token)
        gpu._GPU_CONTEXT_ID.reset(context_token)


def test_sync_nested_gate_retains_outer_lock_after_inner_failure():
    with gpu.gpu_gatekeeper("outer", timeout_seconds=1):
        holder = gpu._CURRENT_HOLDER
        with pytest.raises(ValueError, match="inner"):
            with gpu.gpu_gatekeeper("nested", timeout_seconds=.1):
                raise ValueError("inner")
        assert gpu._CURRENT_HOLDER == holder
        assert gpu._RECURSION_DEPTH == 1
    assert gpu._GPU_SCOPE.get() is None


def test_same_job_id_in_distinct_threads_does_not_allow_reentry():
    def contender():
        gpu.set_gpu_context_id("fixture-execution")
        with pytest.raises(TimeoutError):
            with gpu.gpu_gatekeeper("contender", timeout_seconds=.1, poll_interval=.01):
                pytest.fail("Distinct thread bypassed GPU ownership")
        assert gpu._GPU_SCOPE.get() is None

    with gpu.gpu_gatekeeper("owner", timeout_seconds=1):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contender).result(timeout=5)


def test_async_nested_gate_and_native_nested_stage_share_ownership():
    async def scenario():
        async with gpu.async_gpu_gatekeeper("outer", timeout_seconds=1):
            holder = gpu._CURRENT_HOLDER
            async with gpu.async_gpu_gatekeeper("inner", timeout_seconds=.1):
                assert gpu._CURRENT_HOLDER == holder
                assert gpu._RECURSION_DEPTH == 2

            def native_stage():
                with gpu.gpu_gatekeeper("native-inner", timeout_seconds=.1):
                    assert gpu._CURRENT_HOLDER == holder
                    assert gpu._RECURSION_DEPTH == 2

            await batch_control.to_thread_drained(native_stage)
            assert gpu._RECURSION_DEPTH == 1
        assert gpu._GPU_SCOPE.get() is None

    asyncio.run(scenario())


def test_child_async_task_inherits_job_id_but_cannot_reenter_parent_gate():
    async def contender():
        with pytest.raises(TimeoutError):
            async with gpu.async_gpu_gatekeeper("child", timeout_seconds=.1):
                pytest.fail("Sibling task bypassed GPU ownership")

    async def scenario():
        async with gpu.async_gpu_gatekeeper("parent", timeout_seconds=1):
            holder = gpu._CURRENT_HOLDER
            task = asyncio.create_task(contender())
            await asyncio.sleep(.02)
            assert not task.done()  # The event loop continued while the gate waited.
            await asyncio.wait_for(task, 3)
            assert gpu._CURRENT_HOLDER == holder

    asyncio.run(scenario())


def test_cancelled_waiter_is_drained_without_releasing_another_owner():
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def owner():
            async with gpu.async_gpu_gatekeeper("owner", timeout_seconds=1):
                entered.set()
                await release.wait()

        async def waiter():
            async with gpu.async_gpu_gatekeeper("waiter", timeout_seconds=10):
                pytest.fail("Cancelled waiter ran GPU work")

        first = asyncio.create_task(owner())
        await asyncio.wait_for(entered.wait(), 3)
        holder = gpu._CURRENT_HOLDER
        second = asyncio.create_task(waiter())
        await asyncio.sleep(.03)
        second.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(second, 3)
            assert gpu._CURRENT_HOLDER == holder
        finally:
            release.set()
            await first
        async with gpu.async_gpu_gatekeeper("next", timeout_seconds=1):
            pass

    asyncio.run(scenario())


def test_stop_before_acquisition_restores_context():
    token = batch_control.stop_check.set(lambda: True)
    try:
        with pytest.raises(batch_control.BatchStopRequested):
            with gpu.gpu_gatekeeper("stopped", timeout_seconds=1):
                pytest.fail("Stopped work ran")
        assert gpu._GPU_SCOPE.get() is None
    finally:
        batch_control.stop_check.reset(token)


def test_configuration_failure_restores_context(monkeypatch):
    def broken_settings():
        raise OSError("unreadable settings")
    monkeypatch.setattr(worker_settings, "get_worker_settings", broken_settings)
    with pytest.raises(OSError, match="settings"):
        with gpu.gpu_gatekeeper("setup", timeout_seconds=1):
            pass
    assert gpu._GPU_SCOPE.get() is None


def test_stop_during_vram_probe_releases_acquired_os_lock(monkeypatch):
    stopped = threading.Event()
    token = batch_control.stop_check.set(stopped.is_set)
    def probe(**kw):
        stopped.set()
        return True, "safe"
    monkeypatch.setattr(vram, "is_vram_safe_for_gpu_step", probe)
    try:
        with pytest.raises(batch_control.BatchStopRequested):
            with gpu.gpu_gatekeeper("stopped-in-probe", timeout_seconds=1):
                pytest.fail("Stop after probe was ignored")
        assert gpu._GPU_SCOPE.get() is None
    finally:
        batch_control.stop_check.reset(token)
    monkeypatch.setattr(vram, "is_vram_safe_for_gpu_step", lambda **kw: (True, "safe"))
    with gpu.gpu_gatekeeper("next", timeout_seconds=.2):
        pass


def test_cancellation_during_async_cleanup_restores_caller_context(monkeypatch):
    cleanup_started = threading.Event()
    cleanup_release = threading.Event()
    def cleanup():
        cleanup_started.set()
        assert cleanup_release.wait(5)
    monkeypatch.setattr(gpu.gc, "collect", cleanup)

    async def scenario():
        original_stop = batch_control.stop_check.get()
        original_scope = gpu._GPU_SCOPE.get()
        async def owner():
            try:
                async with gpu.async_gpu_gatekeeper("owner", timeout_seconds=1):
                    pass
            except asyncio.CancelledError:
                assert batch_control.stop_check.get() is original_stop
                assert gpu._GPU_SCOPE.get() is original_scope
                raise
        task = asyncio.create_task(owner())
        try:
            await wait_for_thread_event(cleanup_started)
            task.cancel()
            await asyncio.sleep(.02)
            assert not task.done()
        finally:
            cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)

    asyncio.run(scenario())


def test_stage_timeout_keeps_gpu_until_native_thread_drains():
    from v1_stage_runtime import await_stage
    native_started = threading.Event()
    stop_observed = threading.Event()
    native_release = threading.Event()
    def native():
        predicate = batch_control.stop_check.get()
        native_started.set()
        while not predicate():
            time.sleep(.005)
        stop_observed.set()
        assert native_release.wait(5)

    async def scenario():
        async def owner():
            async with gpu.async_gpu_gatekeeper("native", timeout_seconds=1):
                await await_stage(batch_control.to_thread_drained(native), timeout=.03)
        task = asyncio.create_task(owner())
        try:
            await wait_for_thread_event(stop_observed)
            assert native_started.is_set()
            assert not task.done()
            assert gpu._CURRENT_HOLDER is not None
        finally:
            native_release.set()
        with pytest.raises(TimeoutError, match="Stage exceeded"):
            await asyncio.wait_for(task, 3)
        async with gpu.async_gpu_gatekeeper("next", timeout_seconds=1):
            pass

    asyncio.run(scenario())


def test_other_process_cannot_enter_live_gpu_lease(tmp_path):
    code = """
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import v1_gpu_gatekeeper as gpu
import worker_settings
import v1_vram_monitor as vram
gpu.GPU_LOCK_FILE = Path(sys.argv[2])
worker_settings.get_worker_settings = lambda: {'auto_vram_guard': False}
vram.get_vram_stats = lambda: {}
gpu.set_gpu_context_id('fixture-execution')
try:
    with gpu.gpu_gatekeeper('other-process', timeout_seconds=.1, poll_interval=.01):
        raise AssertionError('Cross-process GPU overlap')
except TimeoutError:
    print('blocked-by-owner')
"""
    env = dict(os.environ, AUTODUB_WORKSPACE=str(tmp_path / "child-workspace"), PYTHONDONTWRITEBYTECODE="1")
    with gpu.gpu_gatekeeper("parent", timeout_seconds=1):
        child = subprocess.run([sys.executable, "-B", "-c", code,
                                str(Path(__file__).resolve().parents[1] / "backend"), str(gpu.GPU_LOCK_FILE)],
                               env=env, capture_output=True, text=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 0, child.stdout + child.stderr
    assert "blocked-by-owner" in child.stdout
