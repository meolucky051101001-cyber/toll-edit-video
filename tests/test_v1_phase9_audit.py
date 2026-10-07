import os
import sys
import time
import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import main
import render_history
import social_downloader
import tool_control


class Phase9AuditTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_security_middleware_rejects_missing_token(self):
        # Missing header -> 403
        res = self.client.post("/api/run-batch")
        self.assertEqual(res.status_code, 403)
        self.assertIn("X-Local-Control-Token", res.json()["detail"])

        # Invalid token -> 403
        res_bad = self.client.post(
            "/api/run-batch",
            headers={"X-Local-Control-Token": "bad-token-12345"}
        )
        self.assertEqual(res_bad.status_code, 403)

        # Valid token -> Not 403
        res_good = self.client.post(
            "/api/run-batch",
            headers={"X-Local-Control-Token": main.LOCAL_TOKEN}
        )
        self.assertNotEqual(res_good.status_code, 403)

    def test_security_middleware_rejects_external_host(self):
        # Requests from outside localhost are rejected with 403
        ext_client = TestClient(main.app, client=("192.168.1.100", 54321))
        res = ext_client.get("/")
        self.assertEqual(res.status_code, 403)
        self.assertIn("localhost", res.json()["detail"])

    def test_downloader_timeout_defaults_to_finite(self):
        # When SOCIAL_DOWNLOAD_TIMEOUT_SECONDS is absent or empty, timeout must be finite (120.0), not None
        with patch.dict(os.environ, {}, clear=True):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                with patch("os.listdir", return_value=[]):
                    social_downloader.download_social_video("http://example.com/video", "temp_dir", "test_prefix")
                self.assertTrue(mock_run.called)
                timeout_val = mock_run.call_args[1].get("timeout")
                self.assertIsNotNone(timeout_val)
                self.assertEqual(timeout_val, 120.0)

    def test_render_history_stale_lock_recovery(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / ".render_history.lock"
            # Create a stale lock file older than 60 seconds
            lock_path.write_text("dummy")
            past_time = time.time() - 100.0
            os.utime(str(lock_path), (past_time, past_time))

            # _acquire_lock should recover from stale lock and succeed
            acquired = render_history._acquire_lock(lock_path, timeout=1.0, stale_timeout=60.0)
            self.assertTrue(acquired)
            render_history._release_lock(lock_path)
            self.assertFalse(lock_path.exists())

    def test_unified_pipeline_lock_identity_and_conflict(self):
        # UNIFIED_PIPELINE_LOCK must be shared by API and Batch
        self.assertIs(main.API_PROCESS_LOCK, main.UNIFIED_PIPELINE_LOCK)
        self.assertIs(main.BATCH_TASK_LOCK, main.UNIFIED_PIPELINE_LOCK)

        # When pipeline is active in job_tracker, api_process_video must reject with 409
        with patch("job_tracker.get_status", return_value={"active": True}):
            with patch.object(main, "_validate_input_path", return_value=Path("test.mp4")):
                res = self.client.post(
                    "/api/process_video",
                    data={"video_path": "test.mp4"},
                    headers={"X-Local-Control-Token": main.LOCAL_TOKEN}
                )
                self.assertEqual(res.status_code, 409)
                self.assertIn("Batch", res.json()["detail"])

    def test_tool_control_does_not_kill_foreign_process_on_8090(self):
        mock_conn = MagicMock()
        mock_conn.laddr = MagicMock(port=8090)
        mock_conn.pid = 99999

        mock_proc = MagicMock()
        mock_proc.cmdline.return_value = ["node", "another_app.js"]

        with patch("psutil.net_connections", return_value=[mock_conn]):
            with patch("psutil.Process", return_value=mock_proc):
                with patch("urllib.request.urlopen", side_effect=Exception("not running")):
                    tool_control.ensure_single_controller()
                    # Foreign app must NOT be killed
                    mock_proc.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
