import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))
from smoke_test_v2 import validate_qc_report
from benchmark_v2_performance import ResourceMonitor, parse_manifest_timings


class AcceptanceTests(unittest.TestCase):
    def test_vfr_reader_recovers_from_seek_overshoot_using_pts(self):
        import cv2
        import numpy as np
        from backend.ocr_adaptive import OrderedFrameReader
        class Capture:
            def __init__(self):
                self.index = -1
                self.points = [0, .1, .3, .7, .8]
            def set(self, prop, value):
                self.index = -1 if prop == cv2.CAP_PROP_POS_FRAMES else 3
            def grab(self):
                self.index += 1
                return self.index < len(self.points)
            def get(self, prop):
                return self.points[self.index] * 1000
            def retrieve(self):
                return True, np.full((100, 100, 3), self.index, np.uint8)
        metrics = {"seeks": 0, "grabbed_frames": 0, "decoded_samples": 0}
        reader = OrderedFrameReader(Capture(), 30, 100, 100, lambda: False, metrics, 1)
        frame, _ = reader.read(.3)
        self.assertEqual(int(frame[0, 0, 0]), 2)
        self.assertEqual(metrics["seeks"], 2)
        frame, _ = reader.read(.7)
        self.assertEqual(int(frame[0, 0, 0]), 3)

    def test_vfr_proxy_retains_original_dimensions_and_gpu_encoder(self):
        from backend import ocr_utils as ocr
        cap = Mock()
        import cv2
        cap.isOpened.return_value = True
        cap.get.side_effect = lambda prop: {cv2.CAP_PROP_FPS: 50,
            cv2.CAP_PROP_FRAME_WIDTH: 2160, cv2.CAP_PROP_FRAME_HEIGHT: 3840,
            cv2.CAP_PROP_FRAME_COUNT: 1000}.get(prop, 0)
        probe = Mock(stdout=json.dumps({"streams": [{"r_frame_rate": "300/1", "avg_frame_rate": "50/1"}]}))
        original = ocr.perform_video_ocr
        metrics = {}
        with patch.object(ocr.cv2, "VideoCapture", return_value=cap), patch.object(ocr.subprocess, "run", return_value=probe) as run, patch.object(ocr, "perform_video_ocr", return_value=([], 720, 1280, .8)):
            result = original("fixture.mp4", srt_segments=[Mock()], adaptive=True, metrics=metrics)
        self.assertEqual(result, ([], 2160, 3840, .8))
        command = run.call_args.args[0]
        self.assertIn("h264_nvenc", command)
        self.assertIn("cuda", command)
        self.assertTrue(metrics["ocr_proxy"])
        self.assertTrue(cap.release.called)

    def test_smoke_requires_explicit_gate_and_distinct_checks(self):
        checks = [{"name": name, "status": "pass"} for name in
                  ["pixel_cover_qc", "tts_integrity"] + ["check_" + str(i) for i in range(11)]]
        validate_qc_report({"checks": checks}, True)
        for allowed in (False, None, 1, "true"):
            with self.assertRaises(RuntimeError):
                validate_qc_report({"checks": checks}, allowed)
        with self.assertRaises(RuntimeError):
            validate_qc_report({"checks": checks[:2] * 7}, True)
        for status in ("error", "unknown"):
            with self.assertRaises(RuntimeError):
                validate_qc_report({"checks": checks[:-1] + [{"name": "last", "status": status}]}, True)

    def test_vram_unavailable_is_distinct_from_measured_zero(self):
        monitor = ResourceMonitor()
        with patch("benchmark_v2_performance.subprocess.run", side_effect=OSError):
            self.assertIsNone(monitor._get_vram_mb())
        self.assertEqual(monitor._vram_read_errors, 1)
        with patch("benchmark_v2_performance.subprocess.run", return_value=Mock(returncode=0, stdout="0\n")):
            self.assertEqual(monitor._get_vram_mb(), 0)
        self.assertEqual(monitor._vram_valid_samples, 1)

    def test_warm_status_requires_actual_cache_event(self):
        stage = SimpleNamespace(status=SimpleNamespace(value="completed"), started_at=None, finished_at=None)
        manifest = SimpleNamespace(metadata={}, stages={"ocr": stage})
        stages, _, _ = parse_manifest_timings(manifest, "warm")
        self.assertEqual(stages[0].status, "completed")
        stages, _, _ = parse_manifest_timings(manifest, "warm", live_events={"ocr": "cache_hit"})
        self.assertIn("reused from cache", stages[0].status)

    def test_history_import_and_write_leave_v1_history_untouched(self):
        history = importlib.import_module("backend.render_history")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            v1 = root / ".render_history.json"
            v1.write_text('{"old": 10}', encoding="utf-8")
            with patch.object(history, "HISTORY_FILE", root / ".render_history_v2.json"):
                history.record_render_duration("Dubbed_test.mp4", 12.5)
                self.assertTrue((root / ".render_history_v2.json").is_file())
                self.assertIn("test.mp4", json.loads((root / ".render_history_v2.json").read_text()))
            self.assertEqual(v1.read_text(), '{"old": 10}')

    def test_gemini_failures_have_bounded_attempts_and_no_query_key(self):
        from backend.ai import translation as tr
        with patch.object(tr, "_gemini_unhealthy_until", 0), patch.object(tr, "_gemini_transient_failures", 0), patch.object(tr, "current_model_policy", return_value=SimpleNamespace(gemini_candidates=["one", "two", "three"])), patch.object(tr.requests, "post", side_effect=TimeoutError("private-url")) as post:
            self.assertIsNone(tr.translate_with_gemini(["你好"], api_key="dummy_key"))
            self.assertEqual(post.call_count, 2)
            self.assertFalse(tr.is_gemini_available())
            self.assertNotIn("dummy_key", post.call_args.args[0])
            self.assertEqual(post.call_args.kwargs["timeout"], 20)
            tr.translate_with_gemini(["你好"], api_key="dummy_key")
            self.assertEqual(post.call_count, 2)

    def test_gemini_bad_json_can_try_second_model(self):
        from backend.ai import translation as tr
        bad = Mock(status_code=200)
        bad.json.side_effect = ValueError("bad")
        good = Mock(status_code=200)
        good.json.return_value = {"candidates": [{"content": {"parts": [{"text": '["Xin chào"]'}]}}]}
        with patch.object(tr, "_gemini_unhealthy_until", 0), patch.object(tr, "_gemini_transient_failures", 0), patch.object(tr, "current_model_policy", return_value=SimpleNamespace(gemini_candidates=["one", "two"])), patch.object(tr.requests, "post", side_effect=[bad, good]):
            self.assertEqual(tr.translate_with_gemini(["你好"], api_key="dummy_key"), ["Xin chào"])
            self.assertEqual(tr._gemini_transient_failures, 0)
