import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.ai.translation import _subdivide_and_retry
from backend.pipeline_v2.timing import probe_audio_duration, fit_audio_to_window
import backend.ai.voice_cloning as voice_cloning


class CodeReviewFixesTests(unittest.TestCase):
    def test_rvc_semaphore_single_instance(self):
        """Kiểm tra rvc_semaphore chỉ có duy nhất 1 instance ở module-level."""
        self.assertTrue(hasattr(voice_cloning, "rvc_semaphore"))
        self.assertIsInstance(voice_cloning.rvc_semaphore, asyncio.Semaphore)

    def test_subdivide_and_retry_success(self):
        """Kiểm tra helper chia nhỏ thích ứng (divide-and-conquer) ghép nối thành công."""
        texts = ["câu 1", "câu 2", "câu 3", "câu 4"]

        def mock_translate(batch, **kwargs):
            if len(batch) == 2:
                return [f"dịch_{t}" for t in batch]
            return None

        result = _subdivide_and_retry(
            mock_translate,
            texts,
            target_lang="vi",
            api_key="test_key",
            prior_context=None,
            model="gemini-3.8-flash",
            provider_label="Gemini",
            context_start_seconds=1.0,
            context_end_seconds=10.0,
            duration_budgets=[2.0, 3.0, 1.5, 4.0],
        )
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 4)
        self.assertEqual(result, ["dịch_câu 1", "dịch_câu 2", "dịch_câu 3", "dịch_câu 4"])

    def test_subdivide_and_retry_temporal_context_split(self):
        """Kiểm tra context_start_seconds ở nửa trái và context_end_seconds ở nửa phải."""
        recorded_calls = []

        def mock_translate(batch, **kwargs):
            recorded_calls.append(kwargs)
            return [f"d_{t}" for t in batch]

        _subdivide_and_retry(
            mock_translate,
            ["a", "b", "c", "d"],
            target_lang="vi",
            api_key="k",
            prior_context=None,
            model="m",
            context_start_seconds=0.0,
            context_end_seconds=5.0,
        )
        self.assertEqual(len(recorded_calls), 2)
        # Nửa trái giữ context_start_seconds, đã bỏ context_end_seconds
        self.assertEqual(recorded_calls[0].get("context_start_seconds"), 0.0)
        self.assertNotIn("context_end_seconds", recorded_calls[0])
        # Nửa phải giữ context_end_seconds, đã bỏ context_start_seconds
        self.assertEqual(recorded_calls[1].get("context_end_seconds"), 5.0)
        self.assertNotIn("context_start_seconds", recorded_calls[1])

    @patch("backend.pipeline_v2.timing.subprocess.run")
    def test_probe_audio_duration_timeout_raises_runtime_error(self, mock_run):
        """Kiểm tra probe_audio_duration bắt TimeoutExpired và raise RuntimeError rõ nghĩa."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ffprobe"], timeout=30)
        with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tf:
            tf_path = tf.name
        try:
            with self.assertRaises(RuntimeError) as ctx:
                probe_audio_duration(tf_path)
            self.assertIn("timed out after 30s", str(ctx.exception))
        finally:
            Path(tf_path).unlink(missing_ok=True)

    @patch("backend.pipeline_v2.timing.probe_audio_duration", return_value=10.0)
    @patch("backend.pipeline_v2.timing.subprocess.run")
    def test_fit_audio_to_window_timeout_raises_runtime_error(self, mock_run, mock_probe):
        """Kiểm tra fit_audio_to_window bắt TimeoutExpired và raise RuntimeError rõ nghĩa."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=120)
        with self.assertRaises(RuntimeError) as ctx:
            fit_audio_to_window("in.wav", "out.wav", target_seconds=5.0)
        self.assertIn("timed out after 120s", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
