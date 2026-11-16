"""Phase 9.1 Final Gate Verification Tests for Tool V1.0 Final.

Covers:
1. Host Header Validation against DNS Rebinding (reject evil.example:8088, accept 127.0.0.1, localhost, [::1])
2. Concurrency & Unified Pipeline Lock Lifecycle (batch holds lock for entire run, generate_subtitles rejected with 409 if locked)
3. Subprocess Voice Isolation with API Keys & CJK Fail-Closed (generate_dubbing_audio_isolated calls child process with env vars; raises on CJK)
4. Controller uses verified scoped shutdown and refuses to interrupt an active job
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
        cls.workspace_dir = tempfile.TemporaryDirectory(prefix="autodub_p91_test_")
        cls.previous_workspace = os.environ.get("AUTODUB_WORKSPACE")
        os.environ["AUTODUB_WORKSPACE"] = cls.workspace_dir.name
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
    def test_gate4_controller_refuses_busy_without_http_stop(self):
        """UI stop respects the shared busy gate and never force-stops an active job."""
        import tool_control
        from v1_shutdown import ShutdownResult, EXIT_BUSY

        result = ShutdownResult(EXIT_BUSY, "processing", "stop", False,
                                busy_reasons=["dashboard_job_active:fixture"])
        with patch.object(tool_control.shutdown, "execute_shutdown", return_value=result) as stop, \
             patch.object(tool_control.urllib.request, "urlopen") as request:
            with self.assertRaises(tool_control.ControlShutdownError):
                tool_control.stop_and_release("v1")
        self.assertEqual(stop.call_args.kwargs["scope"], "processing")
        self.assertFalse(stop.call_args.kwargs["force"])
        request.assert_not_called()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        if cls.previous_workspace is None:
            os.environ.pop("AUTODUB_WORKSPACE", None)
        else:
            os.environ["AUTODUB_WORKSPACE"] = cls.previous_workspace
        cls.workspace_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
