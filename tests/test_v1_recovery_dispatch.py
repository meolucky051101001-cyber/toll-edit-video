import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend import phase_d_routes as routes
from backend.recovery_service import RecoveryService


class RecoveryDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        lock = asyncio.Lock()
        async def runner(*args, **kwargs):
            self.calls.append((args, kwargs))
            lock.release()
        self.runtime = SimpleNamespace(
            UNIFIED_PIPELINE_LOCK=lock, BATCH_TASK=None, single_video_runner=runner,
            get_input_dir=lambda: Path("input"), get_output_dir=lambda: Path("output"),
            job_tracker=Mock(), shared_state=SimpleNamespace(stop_requested=True),
        )
        self.runtime.job_tracker.get_status.return_value = {"active": False}
        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pipeline_runtime=self.runtime)))

    async def test_one_request_dispatches_only_the_requested_video(self):
        result = {"source_video": "source.mp4", "work_dir": "artifacts", "frozen_config": {"qc_policy": "BLOCK"}}
        with patch.object(routes._service, "execute_resume_action", return_value=result) as action, \
             patch("subprocess.Popen") as popen:
            response = await routes.execute_job_resume("source", routes.ResumeRequest(mode="recover", plan_token="token"), self.request)
            await self.runtime.BATCH_TASK
        self.assertTrue(json.loads(response.body)["dispatched"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][0][0], "source.mp4")
        self.assertEqual(self.calls[0][1]["config_overrides"], {"qc_policy": "BLOCK"})
        self.assertTrue(self.calls[0][1]["pipeline_lock_held"])
        self.assertFalse(self.runtime.UNIFIED_PIPELINE_LOCK.locked())
        action.assert_called_once()
        popen.assert_not_called()

    async def test_busy_rejection_precedes_cleanup(self):
        self.runtime.job_tracker.get_status.return_value = {"active": True}
        with patch.object(routes._service, "execute_resume_action") as action:
            with self.assertRaises(HTTPException) as error:
                await routes.execute_job_resume("source", routes.ResumeRequest(mode="restart"), self.request)
        self.assertEqual(error.exception.status_code, 409)
        action.assert_not_called()

    async def test_bad_plan_releases_reservation_and_lock(self):
        with patch.object(routes._service, "execute_resume_action", side_effect=ValueError("bad plan")):
            with self.assertRaises(HTTPException) as error:
                await routes.execute_job_resume("source", routes.ResumeRequest(mode="recover"), self.request)
        self.assertEqual(error.exception.status_code, 400)
        self.assertFalse(self.runtime.UNIFIED_PIPELINE_LOCK.locked())
        self.runtime.job_tracker.release_batch.assert_called_once()
        self.assertIsNone(self.runtime.BATCH_TASK)

    async def test_restart_is_passed_to_the_runner(self):
        with patch.object(routes._service, "execute_resume_action", return_value={"source_video": "source.mp4"}):
            await routes.execute_job_resume("source", routes.ResumeRequest(mode="restart"), self.request)
            await self.runtime.BATCH_TASK
        self.assertEqual(self.calls[0][1]["recovery_mode"], "restart")


class RecoveryRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_failure_releases_reserved_slot_and_lock(self):
        import builtins
        import main as api
        lock = asyncio.Lock()
        await lock.acquire()
        tracker = Mock()
        tracker.current_execution = api.job_tracker.current_execution
        original_import = builtins.__import__

        def fail_pipeline_import(name, *args, **kwargs):
            if name == "batch_processor":
                raise ImportError("render dependency unavailable")
            return original_import(name, *args, **kwargs)

        with patch.object(api, "UNIFIED_PIPELINE_LOCK", lock), \
             patch.object(api, "job_tracker", tracker), \
             patch("builtins.__import__", side_effect=fail_pipeline_import):
            await api.single_video_runner("source.mp4", "output", "reserved", pipeline_lock_held=True)
        self.assertFalse(lock.locked())
        tracker.fail_batch.assert_called_once_with("render dependency unavailable", job_id="reserved")
        tracker.release_batch.assert_called_once_with("reserved")
        self.assertIsNone(tracker.current_execution.get())

    async def test_cancellation_records_stopped_before_releasing_slot(self):
        import sys
        import main as api
        lock = asyncio.Lock()
        await lock.acquire()
        tracker = Mock()
        tracker.current_execution = api.job_tracker.current_execution

        async def canceled(*args, **kwargs):
            self.assertEqual(tracker.current_execution.get(), "reserved")
            raise asyncio.CancelledError

        fake_processor = SimpleNamespace(process_single_local_video=canceled)
        with patch.object(api, "UNIFIED_PIPELINE_LOCK", lock), \
             patch.object(api, "job_tracker", tracker), \
             patch.dict(sys.modules, {"batch_processor": fake_processor}):
            with self.assertRaises(asyncio.CancelledError):
                await api.single_video_runner("source.mp4", "output", "reserved", pipeline_lock_held=True)
        self.assertFalse(lock.locked())
        tracker.mark_stopped.assert_called_once()
        tracker.release_batch.assert_called_once_with("reserved")
        self.assertIsNone(tracker.current_execution.get())
        calls = [call[0] for call in tracker.mock_calls]
        self.assertLess(calls.index("mark_stopped"), calls.index("release_batch"))


class RecoveryArtifactTests(unittest.TestCase):
    def test_changed_manifest_invalidates_plan_without_touching_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"input video")
            job = root / "workspace/source"
            job.mkdir(parents=True)
            (job / "mixed.wav").write_bytes(b"valid mix")
            (job / "final_source.mp4").write_bytes(b"invalid render")
            service = RecoveryService(str(root / "workspace"))
            manifest = service.manifests.get_or_create_manifest("source-id", source, effective_config={"output_dir": str(job)})
            plan = service.build_resume_plan("source-id", root, root / "output")
            manifest.status = "failed"
            service.manifests.save_manifest(manifest)
            with self.assertRaisesRegex(RuntimeError, "đã thay đổi"):
                service.execute_resume_action("source-id", "restart", plan["plan_token"],
                                              input_dir=root, output_dir=root / "output")
            self.assertEqual((job / "mixed.wav").read_bytes(), b"valid mix")
            self.assertEqual((job / "final_source.mp4").read_bytes(), b"invalid render")
