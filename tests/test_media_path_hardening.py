# -*- coding: utf-8 -*-
"""Kiểm thử chặn path traversal cho các endpoint media & định danh job của Dashboard (Gói 0.2)."""
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
import phase_a_routes
from segment_editor_service import InvalidJobIdentifier, sanitize_job_id


class TestMediaPathHardening(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_dir = self.root / "phoi"
        self.output_dir = self.root / "banve"
        self.workspace = self.root / "workspace"
        self.input_dir.mkdir()
        self.output_dir.mkdir()
        self.workspace.mkdir()

        self.old_workspace = main.WORKSPACE
        self.old_tracker_workspace = job_tracker.WORKSPACE
        self.old_status_file = job_tracker.STATUS_FILE
        self.old_batch_lock_file = job_tracker.BATCH_LOCK_FILE
        self.old_state = dict(job_tracker._CURRENT_STATE)

        main.WORKSPACE = str(self.workspace)
        job_tracker.WORKSPACE = self.workspace
        job_tracker.STATUS_FILE = self.workspace / "job_status.json"
        job_tracker.BATCH_LOCK_FILE = self.workspace / "batch.lock"
        with job_tracker._LOCK:
            job_tracker._CURRENT_STATE.clear()
            job_tracker._CURRENT_STATE.update(job_tracker._DEFAULT_STATE)
            job_tracker._CURRENT_STATE["updated_at"] = time.time()

        self.input_patch = patch.object(main, "get_input_dir", return_value=self.input_dir)
        self.output_patch = patch.object(main, "get_output_dir", return_value=self.output_dir)
        self.input_patch.start()
        self.output_patch.start()
        main.BATCH_TASK = None
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def tearDown(self):
        self.input_patch.stop()
        self.output_patch.stop()
        with job_tracker._LOCK:
            job_tracker._CURRENT_STATE.clear()
            job_tracker._CURRENT_STATE.update(self.old_state)
        main.WORKSPACE = self.old_workspace
        job_tracker.WORKSPACE = self.old_tracker_workspace
        job_tracker.STATUS_FILE = self.old_status_file
        job_tracker.BATCH_LOCK_FILE = self.old_batch_lock_file
        self.temporary.cleanup()

    # --- _safe_preview_name (unit) ---

    def test_safe_preview_name_accepts_normal_ids(self):
        self.assertEqual(main._safe_preview_name("capcut-BV562_streaming"), "capcut-BV562_streaming")

    def test_safe_preview_name_rejects_traversal(self):
        for bad in ("../../etc/passwd", "..\\..\\secret", "a/b", "a\\b", "C:", "C:\\x", ".", "..", "", "   ", "a<b"):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException) as raised:
                    main._safe_preview_name(bad)
                self.assertEqual(raised.exception.status_code, 400)

    # --- voice preview endpoints (route) ---

    def test_voice_preview_video_traversal_is_blocked(self):
        response = self.client.get("/api/voice-preview-video/..%2F..%2Fsecret")
        self.assertIn(response.status_code, (400, 404))

    def test_voice_preview_audio_traversal_is_blocked(self):
        response = self.client.get("/api/voice-preview-audio/..%2F..%2Fsecret")
        self.assertIn(response.status_code, (400, 404))

    def test_voice_preview_normal_id_missing_file_is_404(self):
        response = self.client.get("/api/voice-preview-video/no_such_voice_zz9")
        self.assertEqual(response.status_code, 404)

    # --- /api/download ---

    def test_download_rejects_dotfile_token(self):
        response = self.client.get("/api/download/.dashboard_control_token")
        self.assertEqual(response.status_code, 400)

    def test_download_rejects_traversal(self):
        response = self.client.get("/api/download/..%2Fsecret.mp4")
        self.assertIn(response.status_code, (400, 404))

    def test_download_rejects_non_video_extension(self):
        response = self.client.get("/api/download/notes.txt")
        self.assertEqual(response.status_code, 400)

    def test_download_serves_real_video_in_workspace(self):
        job_dir = self.workspace / "job_abc"
        job_dir.mkdir()
        video = job_dir / "final_clip.mp4"
        video.write_bytes(b"video-bytes")
        response = self.client.get("/api/download/final_clip.mp4")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"video-bytes")

    # --- Phase A bare-name validation ---

    def test_phase_a_safe_bare_name_rejects_paths(self):
        for bad in ("../x.mp4", "a/b.mp4", "a\\b.mp4", "x<y.mp4", 'a"b.mp4', ""):
            with self.subTest(bad=bad):
                with self.assertRaises(HTTPException):
                    phase_a_routes._safe_bare_name(bad)

    def test_phase_a_safe_bare_name_accepts_unicode(self):
        self.assertEqual(
            phase_a_routes._safe_bare_name("telegram_1_花5k组了个.mp4"),
            "telegram_1_花5k组了个.mp4",
        )

    # --- Phase C job id sanitization ---

    def test_sanitize_job_id_rejects_traversal(self):
        for bad in ("../../etc", "a/b", "a\\b", "x<y", "C:\\evil", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidJobIdentifier):
                    sanitize_job_id(bad)

    def test_sanitize_job_id_accepts_normal_names(self):
        self.assertEqual(sanitize_job_id("Dubbed_telegram_abc_视频.mp4"), "telegram_abc_视频")

    def test_phase_c_segments_route_rejects_traversal_job_id(self):
        response = self.client.get("/api/jobs/..%2F..%2Fetc/segments")
        self.assertIn(response.status_code, (400, 404))

    # --- Cache-Control cho HTML chứa token ---

    def test_dashboard_html_is_no_store(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
