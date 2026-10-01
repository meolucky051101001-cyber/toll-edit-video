"""Phase 9.1 Final Gate Verification Tests for Tool V1.0 Final.

Covers:
1. Host Header Validation against DNS Rebinding (reject evil.example:8088, accept 127.0.0.1, localhost, [::1])
2. Concurrency & Unified Pipeline Lock Lifecycle (batch holds lock for entire run, generate_subtitles rejected with 409 if locked)
3. Subprocess Voice Isolation with API Keys & CJK Fail-Closed (generate_dubbing_audio_isolated calls child process with env vars; raises on CJK)
4. Controller Authenticated Graceful Stop (tool_control.py sends X-Local-Control-Token read from .dashboard_control_token)
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure backend directory is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import pytest
import srt
from fastapi.testclient import TestClient


class TestPhase91FinalGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["AUTODUB_WORKSPACE"] = str(Path(tempfile.gettempdir()) / "autodub_p91_test")
        Path(os.environ["AUTODUB_WORKSPACE"]).mkdir(parents=True, exist_ok=True)
        import main
        cls.main = main
        cls.client = TestClient(cls.main.app)

    # ----------------------------------------------------------------------
    # GATE 1: DNS REBINDING & HOST HEADER VALIDATION
    # ----------------------------------------------------------------------
    def test_gate1_valid_hosts_accepted(self):
        """Valid local hosts (localhost, 127.0.0.1, [::1], testserver) must return 200."""
        for host in ["localhost:8088", "127.0.0.1:8088", "localhost", "127.0.0.1", "[::1]:8088", "testserver"]:
            res = self.client.get("/api/status", headers={"Host": host})
            self.assertEqual(res.status_code, 200, f"Host {host} should be accepted")

    def test_gate1_dns_rebinding_rejected(self):
        """DNS rebinding attempts with attacker-controlled host headers must return 403."""
        forbidden_hosts = [
            "evil.example:8088",
            "evil.example",
            "localhost.attacker.com",
            "attacker.com",
            "192.168.1.100:8088",
            "10.0.0.1",
            "randomdomain.xyz"
        ]
        for host in forbidden_hosts:
            res = self.client.get("/api/status", headers={"Host": host})
            self.assertEqual(res.status_code, 403, f"Host {host} must be rejected with 403")
            self.assertIn("Host không hợp lệ", res.json().get("detail", ""))

    # ----------------------------------------------------------------------
    # GATE 2: CONCURRENCY & UNIFIED PIPELINE LOCK LIFECYCLE
    # ----------------------------------------------------------------------
    def test_gate2_generate_subtitles_rejected_when_batch_holds_lock(self):
        """When UNIFIED_PIPELINE_LOCK is locked by batch, generate_subtitles must return 409 Conflict."""
        headers = {
            "Host": "127.0.0.1:8088",
            "X-Local-Control-Token": self.main.DASHBOARD_CONTROL_TOKEN
        }
        loop = asyncio.new_event_loop()
        try:
            async def run_check():
                async with self.main.UNIFIED_PIPELINE_LOCK:
                    res = self.client.post(
                        "/api/generate_subtitles",
                        data={"video_path": "dummy.mp4"},
                        headers=headers
                    )
                    self.assertEqual(res.status_code, 409)
                    self.assertIn("bận", res.json().get("detail", "").lower())
            loop.run_until_complete(run_check())
        finally:
            loop.close()

    def test_gate2_process_video_and_batch_rejected_when_pipeline_locked(self):
        """While pipeline lock is active, run-batch and process-video must return 409 Conflict."""
        headers = {
            "Host": "localhost:8088",
            "X-Local-Control-Token": self.main.DASHBOARD_CONTROL_TOKEN
        }
        loop = asyncio.new_event_loop()
        try:
            async def run_check():
                async with self.main.UNIFIED_PIPELINE_LOCK:
                    r1 = self.client.post("/api/run-batch", headers=headers)
                    self.assertEqual(r1.status_code, 409)

                    r2 = self.client.post("/api/process-video", data={"video_path": "test.mp4"}, headers=headers)
                    self.assertEqual(r2.status_code, 409)
            loop.run_until_complete(run_check())
        finally:
            loop.close()

    def test_gate2_batch_runner_holds_lock_during_execution(self):
        """Verify batch_runner acquires UNIFIED_PIPELINE_LOCK and releases it on completion or exception."""
        lock_held_during_run = False
        async def mock_process_batch(*args, **kwargs):
            nonlocal lock_held_during_run
            lock_held_during_run = self.main.UNIFIED_PIPELINE_LOCK.locked()

        loop = asyncio.new_event_loop()
        try:
            with patch("batch_processor.process_batch_folder", side_effect=mock_process_batch):
                self.main.BATCH_INPUT_FOLDER = "mock_in"
                self.main.BATCH_OUTPUT_FOLDER = "mock_out"
                loop.run_until_complete(self.main.batch_runner())
                self.assertTrue(lock_held_during_run, "UNIFIED_PIPELINE_LOCK must be locked during batch run")
                self.assertFalse(self.main.UNIFIED_PIPELINE_LOCK.locked(), "Lock must be released after batch run")
        finally:
            loop.close()

    # ----------------------------------------------------------------------
    # GATE 3: SUBPROCESS VOICE ISOLATION WITH API KEYS & CJK FAIL-CLOSED
    # ----------------------------------------------------------------------
    def test_gate3_voice_isolated_uses_subprocess_with_api_key(self):
        """generate_dubbing_audio_isolated must NOT bypass subprocess isolation when api_key is provided."""
        from ai import v1_voice_isolated

        segments = [
            srt.Subtitle(index=1, start=srt.timedelta(seconds=0), end=srt.timedelta(seconds=2), content="Xin chào bạn")
        ]

        captured_args = None
        captured_env = None

        def fake_run(cmd, **kwargs):
            nonlocal captured_args, captured_env
            captured_args = cmd
            captured_env = kwargs.get("env", {})
            result_file = Path(cmd[3])
            result_file.write_text(json.dumps(["segment_1.wav"]), encoding="utf-8")

        loop = asyncio.new_event_loop()
        try:
            with patch("batch_control.run", side_effect=fake_run):
                results = loop.run_until_complete(
                    v1_voice_isolated.generate_dubbing_audio_isolated(
                        segments, "mock_output", voice_source="fpt", voice_param="banmai", api_key="secret_fpt_api_token"
                    )
                )
                self.assertEqual(results, ["segment_1.wav"])
                self.assertIsNotNone(captured_args)
                self.assertIn("v1_voice_worker.py", str(captured_args[1]))
                self.assertEqual(captured_env.get("VOICE_API_KEY"), "secret_fpt_api_token")
                self.assertEqual(captured_env.get("FPT_API_KEY"), "secret_fpt_api_token")
                for arg in captured_args:
                    self.assertNotIn("secret_fpt_api_token", str(arg))
        finally:
            loop.close()

    def test_gate3_voice_isolated_cjk_fail_closed(self):
        """If untranslated CJK characters remain in segments, v1_voice_isolated must fail-closed."""
        from ai import v1_voice_isolated

        cjk_segments = [
            srt.Subtitle(index=1, start=srt.timedelta(seconds=0), end=srt.timedelta(seconds=2), content="Hello 你好世界")
        ]

        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                loop.run_until_complete(
                    v1_voice_isolated.generate_dubbing_audio_isolated(cjk_segments, "mock_out")
                )
            self.assertIn("CJK", str(ctx.exception))
            self.assertIn("Fail-Closed", str(ctx.exception))
        finally:
            loop.close()

    # ----------------------------------------------------------------------
    # GATE 4: CONTROLLER AUTHENTICATED GRACEFUL STOP
    # ----------------------------------------------------------------------
    def test_gate4_controller_reads_and_sends_dashboard_token(self):
        """tool_control.stop_and_release must read .dashboard_control_token and pass X-Local-Control-Token."""
        import tool_control

        workspace = Path(os.environ["AUTODUB_WORKSPACE"])
        token_path = workspace / ".dashboard_control_token"
        token_path.write_text("test_secret_runtime_token_12345", encoding="utf-8")

        with patch.dict(tool_control.ROOTS, {"v1": workspace / "dummy_backend"}):
            (workspace / "dummy_backend").mkdir(parents=True, exist_ok=True)
            read_token = tool_control._get_dashboard_token("v1")
            self.assertEqual(read_token, "test_secret_runtime_token_12345")

        captured_request = None
        def fake_urlopen(req, *args, **kwargs):
            nonlocal captured_request
            if isinstance(req, str) and "status" in req:
                resp = MagicMock()
                resp.status = 200
                resp.read.return_value = json.dumps({"active": True}).encode("utf-8")
                resp.__enter__.return_value = resp
                return resp
            if hasattr(req, "get_full_url") and "stop-batch" in req.get_full_url():
                captured_request = req
                resp = MagicMock()
                resp.status = 200
                resp.__enter__.return_value = resp
                return resp
            resp = MagicMock()
            resp.status = 200
            resp.read.return_value = b"{}"
            resp.__enter__.return_value = resp
            return resp

        with patch("tool_control.FLAGS", workspace / "control"), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("psutil.process_iter", return_value=[]), \
             patch("tool_control._get_dashboard_token", return_value="test_secret_runtime_token_12345"):
            tool_control.stop_and_release("v1")

            self.assertIsNotNone(captured_request, "POST /api/stop-batch should have been called")
            self.assertEqual(
                captured_request.headers.get("X-local-control-token"),
                "test_secret_runtime_token_12345",
                "Request must include valid X-Local-Control-Token"
            )

    @classmethod
    def tearDownClass(cls):
        Path(r"C:\tool v1\workspace\control\v1.pause").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
