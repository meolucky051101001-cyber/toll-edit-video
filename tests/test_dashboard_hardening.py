import json
import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import job_tracker
import main
from pipeline_v2.atomic_io import atomic_write_json


class TestDashboardHardening(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_dir = self.root / "phoi"
        self.output_dir = self.root / "banve"
        self.input_dir.mkdir()
        self.output_dir.mkdir()

        self.old_workspace = job_tracker.WORKSPACE
        self.old_status_file = job_tracker.STATUS_FILE
        self.old_batch_lock_file = job_tracker.BATCH_LOCK_FILE
        self.old_state = dict(job_tracker._CURRENT_STATE)

        job_tracker.WORKSPACE = self.root / "workspace"
        job_tracker.STATUS_FILE = job_tracker.WORKSPACE / "job_status.json"
        job_tracker.BATCH_LOCK_FILE = job_tracker.WORKSPACE / "batch.lock"
        with job_tracker._LOCK:
            job_tracker._CURRENT_STATE.clear()
            job_tracker._CURRENT_STATE.update(job_tracker._DEFAULT_STATE)
            job_tracker._CURRENT_STATE["updated_at"] = time.time()

        self.input_patch = patch.object(
            main, "get_input_dir", return_value=self.input_dir
        )
        self.output_patch = patch.object(
            main, "get_output_dir", return_value=self.output_dir
        )
        self.input_patch.start()
        self.output_patch.start()
        main.BATCH_TASK = None
        self.client = TestClient(main.app)

    def tearDown(self):
        self.input_patch.stop()
        self.output_patch.stop()
        with job_tracker._LOCK:
            job_tracker._CURRENT_STATE.clear()
            job_tracker._CURRENT_STATE.update(self.old_state)
        job_tracker.WORKSPACE = self.old_workspace
        job_tracker.STATUS_FILE = self.old_status_file
        job_tracker.BATCH_LOCK_FILE = self.old_batch_lock_file
        self.temporary.cleanup()

    def test_tracker_reads_newer_state_from_another_process(self):
        job_tracker.start_batch(2, str(self.input_dir), str(self.output_dir))
        disk_state = job_tracker.get_status()
        disk_state.update(
            {
                "active": True,
                "percent": 55,
                "step": 3.5,
                "updated_at": disk_state["updated_at"] + 10,
            }
        )
        atomic_write_json(job_tracker.STATUS_FILE, disk_state)

        status = job_tracker.get_status()

        self.assertEqual(status["percent"], 55)
        self.assertEqual(status["step"], 3.5)

    def test_cross_process_lock_rejects_second_batch(self):
        job_tracker.start_batch(1)
        with self.assertRaises(job_tracker.JobAlreadyRunningError):
            job_tracker.start_batch(1)

    def test_mark_stopped_releases_batch_lock(self):
        job_tracker.start_batch(1)
        self.assertTrue(job_tracker.BATCH_LOCK_FILE.exists())

        job_tracker.mark_stopped()

        self.assertFalse(job_tracker.BATCH_LOCK_FILE.exists())
        self.assertEqual(job_tracker.get_status()["status"], "stopped")

    def test_invalid_folder_is_rejected(self):
        response = self.client.post(
            "/api/open-folder", data={"folder": "not-allowed"}
        )
        self.assertEqual(response.status_code, 400)

    def test_open_folder_uses_windows_shell(self):
        with patch.object(main.os, "startfile") as launch:
            response = self.client.post("/api/open-folder", data={"folder": "banve"})
        self.assertEqual(response.status_code, 200)
        launch.assert_called_once_with(str(self.output_dir.resolve()), "open", "", None, 1)

    def test_open_folder_failure_is_visible(self):
        with patch.object(main.os, "startfile", side_effect=OSError("shell failed")):
            response = self.client.post("/api/open-folder", data={"folder": "banve"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("shell failed", response.json()["message"])

    def test_path_traversal_is_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            main._safe_media_path("phoi", "..\\secret.mp4")
        self.assertEqual(raised.exception.status_code, 400)

    def test_valid_video_can_be_streamed(self):
        video = self.input_dir / "clip.mp4"
        video.write_bytes(b"not-a-real-video-but-safe-for-route-test")

        response = self.client.get("/api/stream/phoi/clip.mp4")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, video.read_bytes())

    def test_run_batch_reports_conflict_with_job_id(self):
        job_id = job_tracker.start_batch(1)

        response = self.client.post("/api/run-batch")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["job_id"], job_id)

    def test_dead_owner_is_reported_interrupted(self):
        job_tracker.start_batch(1)
        with patch.object(job_tracker, "_pid_is_running", return_value=False):
            status = job_tracker.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["status"], "interrupted")

    def test_stop_survives_a_progress_write(self):
        job_tracker.start_batch(1)
        job_tracker.request_stop()
        job_tracker._CURRENT_STATE["stop_requested"] = False
        job_tracker.update_step(3, "transcribe")
        self.assertTrue(job_tracker.is_stop_requested())

    def test_callback_failure_releases_lock(self):
        import batch_processor
        async def broken_callback(message):
            raise RuntimeError("callback failed")
        (self.input_dir / "clip.mp4").write_bytes(b"input")
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            asyncio.run(batch_processor.process_batch_folder(
                str(self.input_dir), str(self.output_dir), broken_callback))
        self.assertFalse(job_tracker.BATCH_LOCK_FILE.exists())
        self.assertEqual(job_tracker.get_status()["status"], "error")

    def test_empty_reserved_batch_finishes(self):
        import batch_processor
        job_id = job_tracker.start_batch(0)
        asyncio.run(batch_processor.process_batch_folder(
            str(self.input_dir), str(self.output_dir), job_id=job_id))
        self.assertFalse(job_tracker.BATCH_LOCK_FILE.exists())
        self.assertFalse(job_tracker.get_status()["active"])


if __name__ == "__main__":
    unittest.main()
