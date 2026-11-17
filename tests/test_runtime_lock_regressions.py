"""Focused regressions for native-stage ownership and Studio artifact lookup."""

import asyncio
import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager
from types import SimpleNamespace


class NativeStageOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_waits_for_to_thread_worker_before_returning(self):
        from backend.v1_stage_runtime import await_stage

        finished = threading.Event()

        def native_work():
            from backend.batch_control import stop_check
            predicate = stop_check.get()
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(0.05)  # Native teardown also belongs to the stage.
            finished.set()

        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await await_stage(asyncio.to_thread(native_work), timeout=0.001)

        self.assertTrue(finished.is_set())
        self.assertGreaterEqual(time.monotonic() - started, 0.05)

    async def test_repeated_cancellation_does_not_release_owner_before_worker(self):
        from backend.v1_stage_runtime import await_stage

        started, finish, finished = threading.Event(), threading.Event(), threading.Event()
        owned = []

        def native_work():
            started.set()
            finish.wait(3)
            finished.set()

        async def owner():
            owned.append(True)
            try:
                await await_stage(asyncio.to_thread(native_work), timeout=10)
            finally:
                owned.clear()

        task = asyncio.create_task(owner())
        try:
            for _ in range(200):
                if started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(started.is_set())
            task.cancel()
            await asyncio.sleep(0.02)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertEqual(owned, [True])
            self.assertFalse(task.done())
        finally:
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertEqual(owned, [])

    async def test_gpu_gate_does_not_retry_timeout_from_stage_body(self):
        from backend import v1_gpu_gatekeeper as gpu
        calls = []

        @contextmanager
        def gate(**kwargs):
            calls.append("acquired")
            try:
                yield
            finally:
                calls.append("released")

        with patch.object(gpu, "gpu_gatekeeper", gate):
            with self.assertRaisesRegex(TimeoutError, "stage failed"):
                async with gpu.async_gpu_gatekeeper():
                    raise TimeoutError("stage failed")
        self.assertEqual(calls, ["acquired", "released"])

    async def test_reentrant_gate_does_not_hold_process_mutex_across_await(self):
        from backend import v1_gpu_gatekeeper as gpu

        @contextmanager
        def gate(**kwargs):
            yield

        def probe_mutex():
            acquired = gpu._PROCESS_LOCK.acquire(timeout=0.2)
            if acquired:
                gpu._PROCESS_LOCK.release()
            return acquired

        with patch.object(gpu, "get_gpu_context_id", return_value="owner"), \
             patch.object(gpu, "_CURRENT_HOLDER", "owner"), \
             patch.object(gpu, "_RECURSION_DEPTH", 1), \
             patch.object(gpu, "gpu_gatekeeper", gate):
            async with gpu.async_gpu_gatekeeper():
                self.assertTrue(await asyncio.to_thread(probe_mutex))


class StudioOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_thread_holds_api_lock_and_batch_lease_until_exit(self):
        from backend import workflow_api as workflow
        import job_tracker
        import stop_service
        import disk_guard
        import sys

        loop = asyncio.get_running_loop()
        lock = asyncio.Lock()
        entered, release = threading.Event(), threading.Event()

        def native_work():
            with workflow._manual_pipeline_ownership("studio-test", loop):
                entered.set()
                release.wait(3)

        with patch.dict(sys.modules, {"main": SimpleNamespace(UNIFIED_PIPELINE_LOCK=lock)}), \
             patch.object(workflow, "WORKSPACE", job_tracker.WORKSPACE), \
             patch.object(workflow, "_wait_pipeline_free", return_value=True), \
             patch.object(stop_service, "is_stop_requested", return_value=False), \
             patch.object(disk_guard, "ensure_free_space"):
            task = asyncio.create_task(asyncio.to_thread(native_work))
            try:
                for _ in range(200):
                    if entered.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(entered.is_set())
                self.assertTrue(lock.locked())
                with job_tracker._LOCK, self.assertRaises(job_tracker.JobAlreadyRunningError):
                    job_tracker._claim_batch_lock_unlocked("competing-batch")
            finally:
                release.set()
                await task
            await asyncio.sleep(0)
            self.assertFalse(lock.locked())
            with job_tracker._LOCK:
                job_tracker._claim_batch_lock_unlocked("next-batch")
            job_tracker.release_batch("next-batch")

    async def test_failed_stale_stop_cleanup_releases_both_owners(self):
        from backend import workflow_api as workflow
        import job_tracker
        import stop_service
        import sys

        loop = asyncio.get_running_loop()
        lock = asyncio.Lock()

        def native_work():
            with workflow._manual_pipeline_ownership("studio-test", loop):
                self.fail("Uncleared stop flag must prevent native work")

        with patch.dict(sys.modules, {"main": SimpleNamespace(UNIFIED_PIPELINE_LOCK=lock)}), \
             patch.object(workflow, "WORKSPACE", job_tracker.WORKSPACE), \
             patch.object(workflow, "_wait_pipeline_free", return_value=True), \
             patch.object(stop_service, "is_stop_requested", return_value=True), \
             patch.object(stop_service, "clear_stop"):
            with self.assertRaises(workflow.PipelineStateError):
                await asyncio.to_thread(native_work)
        await asyncio.sleep(0)
        self.assertFalse(lock.locked())
        self.assertFalse(job_tracker.BATCH_LOCK_FILE.exists())


class BatchLeaseTests(unittest.TestCase):
    def test_script_entrypoint_uses_serving_application_lock(self):
        from backend import workflow_api as workflow
        import sys

        serving_lock = object()
        serving = SimpleNamespace(__file__=str(workflow._BACKEND_ROOT / "main.py"),
                                  UNIFIED_PIPELINE_LOCK=serving_lock)
        duplicate = SimpleNamespace(UNIFIED_PIPELINE_LOCK=object())
        with patch.dict(sys.modules, {"__main__": serving, "main": duplicate}):
            self.assertIs(workflow._pipeline_lock(), serving_lock)

    def test_two_requested_workers_cannot_reuse_one_same_process_lease(self):
        import job_tracker
        import worker_settings

        with patch.object(worker_settings, "get_worker_settings", return_value={"concurrency": 2}):
            with job_tracker._LOCK:
                job_tracker._claim_batch_lock_unlocked("first")
            try:
                with job_tracker._LOCK, self.assertRaises(job_tracker.JobAlreadyRunningError):
                    job_tracker._claim_batch_lock_unlocked("second")
            finally:
                job_tracker.release_batch("first")


class StudioArtifactLookupTests(unittest.TestCase):
    def test_resolver_finds_manifest_owned_batch_directory(self):
        from backend import workflow_api

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "input" / "clip.mp4"
            source.parent.mkdir()
            source.write_bytes(b"source")
            job_dir = root / ("batch_" + hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:16] + "_clip")
            job_dir.mkdir()
            manifest_dir = root / "bot_system" / "manifests"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "job.manifest.json").write_text(
                json.dumps({
                    "video_path": str(source),
                    "effective_config": {"output_dir": str(job_dir)},
                    "updated_at": time.time(),
                }),
                encoding="utf-8",
            )
            with patch.object(workflow_api, "WORKSPACE", root):
                self.assertEqual(workflow_api.resolve_job_dir("clip"), job_dir.resolve())

    def test_legacy_batch_marker_resolves_but_ambiguous_stems_are_rejected(self):
        from backend import workflow_api

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for hash_part in ("a" * 16, "b" * 16):
                job = root / f"batch_{hash_part}_clip"
                job.mkdir()
                (job / ".autodub-owned").write_text(str(job.resolve()), encoding="utf-8")
                with patch.object(workflow_api, "WORKSPACE", root):
                    if hash_part.startswith("a"):
                        self.assertEqual(workflow_api.resolve_job_dir("clip"), job.resolve())
                    else:
                        with self.assertRaises(workflow_api.HTTPException) as caught:
                            workflow_api.resolve_job_dir("clip")
                        self.assertEqual(caught.exception.status_code, 409)


class ExplicitVoiceTests(unittest.TestCase):
    def test_api_voice_is_frozen_and_used_by_manual_voice_decision(self):
        import v1_orchestrator as orch
        import job_config_service
        import voice_selection
        import ai.v1_auto_voice as auto_voice

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.mp4"
            source.write_bytes(b"fixture")
            routing = {"metadata": {"duration_s": 1}, "resolved_mode": "SHORT", "planned_pipeline": {
                "asr_model": "whisper_turbo", "separation_model": "demucs"}}
            with patch.object(orch, "create_router_snapshot", return_value=routing), \
                 patch("v1_job_policy.apply_job_policy", side_effect=lambda r, *a: r), \
                 patch.object(orch, "get_feature_flags", return_value={}), \
                 patch.object(orch, "resolve_effective_model", return_value=SimpleNamespace(to_dict=lambda: {})), \
                 patch.object(auto_voice, "get_auto_voice_config", return_value={"enabled": True, "dual_voice": True}), \
                 patch.object(voice_selection, "selected", return_value={"id": "global", "source": "capcut", "param": "global"}), \
                 patch.object(job_config_service, "freeze_job_config", side_effect=lambda **k: {"effective_config": k["overrides"]}):
                result = orch.V1Orchestrator(folder).plan_job(source, "probe", overrides={
                    "voice_source": "edge", "voice_param": "vi-VN-NamMinhNeural"})
            config = result["effective_config"]
            chosen = auto_voice.resolve_locked_voice(
                gender="unknown", confidence=0, median_f0=0, first_seg_index=0,
                voice_mode=config["voice_mode"], voice_config=config["voice_config"])
            self.assertEqual(config["voice_mode"], "manual")
            self.assertEqual((chosen["voice_source"], chosen["voice_param"]), ("edge", "vi-VN-NamMinhNeural"))


if __name__ == "__main__":
    unittest.main()
