import os
import sys
import unittest
import tempfile
import copy
from unittest.mock import patch
from pathlib import Path

# Add backend directory to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import job_tracker
import shared_state
from main import app
from fastapi.testclient import TestClient


class TestDashboardAndTracker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, value in {
            'WORKSPACE': root,
            'STATUS_FILE': root / 'job_status.json',
            'BATCH_LOCK_FILE': root / 'batch.lock',
            '_CURRENT_STATE': copy.deepcopy(job_tracker._DEFAULT_STATE),
        }.items():
            replacement = patch.object(job_tracker, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        # Reset state before each test
        job_tracker.finish_batch()
        shared_state.stop_requested = False
        self.client = TestClient(app)

    def test_job_tracker_lifecycle(self):
        # 1. Check initial idle state
        status = job_tracker.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["status"], "idle")

        # 2. Start batch
        job_tracker.start_batch(total_videos=3, input_dir="D:\\test_phoi", output_dir="D:\\test_banve")
        status = job_tracker.get_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["queue_total"], 3)

        # 3. Start a video
        job_tracker.start_video("test_clip.mp4", index=1, total=3)
        status = job_tracker.get_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["video_name"], "test_clip.mp4")
        self.assertEqual(status["percent"], 5)

        # 4. Update steps
        job_tracker.update_step(1, "Trích xuất âm thanh", percent=10)
        status = job_tracker.get_status()
        self.assertEqual(status["percent"], 10)
        self.assertEqual(status["step"], 1)

        job_tracker.update_step(6, "NVENC Render", percent=95)
        status = job_tracker.get_status()
        self.assertEqual(status["percent"], 95)
        self.assertEqual(status["status"], "rendering")

        # 5. Finish video
        job_tracker.finish_video("test_clip.mp4", "D:\\test_banve\\Dubbed_test_clip.mp4", duration_seconds=15)
        status = job_tracker.get_status()
        self.assertEqual(status["percent"], 100)
        self.assertIsNotNone(status["last_completed"])
        self.assertEqual(status["last_completed"]["video_name"], "test_clip.mp4")

        # 6. Finish batch
        job_tracker.finish_batch()
        status = job_tracker.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["status"], "idle")

    def test_job_tracker_error_and_stop(self):
        job_tracker.start_video("error_clip.mp4", index=1, total=1)
        job_tracker.set_error("error_clip.mp4", "FFmpeg failure")
        status = job_tracker.get_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["last_error"], "FFmpeg failure")

        job_tracker.request_stop()
        status = job_tracker.get_status()
        self.assertEqual(status["status"], "stopping")

    def test_api_status_endpoint(self):
        res = self.client.get("/api/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("active", data)
        self.assertIn("percent", data)
        self.assertIn("status", data)

    def test_api_phoi_endpoint(self):
        res = self.client.get("/api/phoi")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("files", data)
        self.assertIn("total_count", data)

    def test_api_banve_endpoint(self):
        res = self.client.get("/api/banve")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("files", data)
        self.assertIn("total_count", data)

    def test_dashboard_html_served(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        self.assertIn("Tool V1", res.text)
        self.assertIn("D:\\video phôi", res.text)


if __name__ == "__main__":
    unittest.main()
