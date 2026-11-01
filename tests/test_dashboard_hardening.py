import json
import asyncio
import os
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
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def test_missing_token_returns_403(self):
        unauth_client = TestClient(main.app)
        response = unauth_client.post("/api/run-batch")
        self.assertEqual(response.status_code, 403)
        self.assertIn("X-Local-Control-Token", response.json()["detail"])

    def test_script_route_requires_token(self):
        unauth_client = TestClient(main.app)
        response = unauth_client.post("/api/script/preview-tts", json={})
        self.assertEqual(response.status_code, 403)
        authorized = self.client.post("/api/script/preview-tts", json={})
        self.assertEqual(authorized.status_code, 422)

    def test_thumbnail_route_requires_token(self):
        unauth_client = TestClient(main.app)
        response = unauth_client.post(
            "/api/workflow/thumbnail/capture", json={"stem": "sample"}
        )
        self.assertEqual(response.status_code, 403)
        authorized = self.client.post(
            "/api/workflow/thumbnail/capture", json={"stem": ""}
        )
        self.assertEqual(authorized.status_code, 400)

    def test_auth_token_endpoint_removed(self):
        response = TestClient(main.app).get("/api/auth-token")
        self.assertEqual(response.status_code, 404)

    def test_script_studio_page_receives_control_token(self):
        response = TestClient(main.app).get("/kich-ban")
        self.assertEqual(response.status_code, 200)
        self.assertIn(main.LOCAL_TOKEN, response.text)
        self.assertNotIn("__REPLACE_TOKEN__", response.text)

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

    def test_release_retries_transient_permission_error(self):
        job_tracker.start_batch(1)
        target = job_tracker.BATCH_LOCK_FILE
        real_unlink = Path.unlink
        calls = []

        def flaky_unlink(self, *args, **kwargs):
            if self == target:
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError(13, "busy")
            return real_unlink(self, *args, **kwargs)

        with patch.object(Path, "unlink", flaky_unlink):
            job_tracker.finish_batch()
        self.assertFalse(target.exists())
        self.assertEqual(len(calls), 2)

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

    def test_input_folder_rejects_downloader_save_dir(self):
        download_dir = self.root / "vid_chua_edit"
        download_dir.mkdir()
        with patch.dict(os.environ, {"SAVE_DIRECTORY": str(download_dir)}):
            response = self.client.post(
                "/api/input-folder",
                json={"path": str(download_dir)},
                headers={"X-Dashboard-Input": "1"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("chỉ tải", response.json()["detail"])

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


class TestDownloaderFolderGuard(unittest.TestCase):
    def test_saved_input_dir_skips_downloader_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            download_dir = root / "vid_chua_edit"
            fallback_dir = root / "phoi"
            download_dir.mkdir()
            fallback_dir.mkdir()
            (root / "dashboard_input.json").write_text(
                json.dumps({"path": str(download_dir)}), encoding="utf-8"
            )
            env = {
                "SAVE_DIRECTORY": str(download_dir),
                "AUTODUB_INPUT_DIR": str(fallback_dir),
            }
            with patch.object(main, "WORKSPACE", str(root)), patch.dict(os.environ, env):
                self.assertEqual(main.get_input_dir(), fallback_dir)

    def test_batch_refuses_downloader_folder(self):
        import batch_processor

        with tempfile.TemporaryDirectory() as temporary:
            download_dir = Path(temporary) / "vid_chua_edit"
            output_dir = Path(temporary) / "out"
            download_dir.mkdir()
            output_dir.mkdir()
            (download_dir / "clip.mp4").write_bytes(b"input")
            messages = []

            async def collect(message):
                messages.append(message)

            with patch.dict(os.environ, {"SAVE_DIRECTORY": str(download_dir)}):
                asyncio.run(batch_processor.process_batch_folder(
                    str(download_dir), str(output_dir), collect))
            self.assertTrue(any("chỉ tải" in message for message in messages))
            self.assertTrue((download_dir / "clip.mp4").exists())

    def test_downloader_folder_helper_matches_default_and_env(self):
        from config.paths import is_downloader_save_dir

        with tempfile.TemporaryDirectory() as temporary:
            custom = Path(temporary).resolve()
            with patch.dict(os.environ, {"SAVE_DIRECTORY": str(custom)}):
                self.assertTrue(is_downloader_save_dir(custom))
        self.assertTrue(is_downloader_save_dir(r"D:\vid chưa edit"))
        self.assertFalse(is_downloader_save_dir(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()
