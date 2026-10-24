import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from backend import phase_d_routes as routes
from backend.recovery_service import RecoveryService, _ACTIVE_PLAN_TOKENS, compute_file_fingerprint


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


class RecoveryArtifactTests(unittest.TestCase):
    def test_render_recovery_preserves_valid_mix(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            source.write_bytes(b"input video")
            job = root / "workspace/source"
            job.mkdir(parents=True)
            (job / "mixed.wav").write_bytes(b"valid mix")
            (job / "final_source.mp4").write_bytes(b"invalid render")
            service = RecoveryService(str(root / "workspace"))
            token = "audit-render-token"
            _ACTIVE_PLAN_TOKENS[token] = {
                "job_id": "source", "created_at": __import__("time").time(),
                "plan": {"source_file": {"fingerprint": compute_file_fingerprint(source)},
                         "summary": {"first_rerun_stage": "render", "reusable_stages": ["mix"]}},
            }
            try:
                with patch.object(service, "find_source_video", return_value=source):
                    result = service.execute_resume_action("source", "recover", plan_token=token)
                self.assertTrue((job / "mixed.wav").is_file())
                self.assertFalse((job / "final_source.mp4").exists())
                self.assertEqual(result["work_dir"], str(job.resolve()))
            finally:
                _ACTIVE_PLAN_TOKENS.pop(token, None)
