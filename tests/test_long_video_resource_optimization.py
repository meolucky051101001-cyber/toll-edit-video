import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from ai.audio_enhancer import (
    preserve_pristine_background,
    preserve_pristine_background_chunked,
)
from pipeline_v2.timing import GeminiTimingRewriter, RewriteRequest, probe_audio_duration
from video_utils import process_video


class TestLongVideoResourceOptimization(unittest.TestCase):
    def test_probe_audio_duration_uses_fast_soundfile_path(self):
        """Verify probe_audio_duration uses soundfile in-process without spawning subprocess."""
        with tempfile.TemporaryDirectory() as td:
            wav_path = Path(td) / "test.wav"
            sr = 48000
            data = np.zeros((sr * 2, 2), dtype=np.float32)  # 2.0s
            sf.write(str(wav_path), data, sr)

            with mock.patch("subprocess.run") as mock_subproc:
                dur = probe_audio_duration(wav_path)
                self.assertAlmostEqual(dur, 2.0, places=2)
                mock_subproc.assert_not_called()

    def test_probe_audio_duration_falls_back_to_ffprobe_on_unknown(self):
        """Verify probe_audio_duration safely falls back to ffprobe when soundfile cannot read."""
        with tempfile.TemporaryDirectory() as td:
            dummy_path = Path(td) / "test.mkv"
            dummy_path.write_bytes(b"dummy mkv data")

            mock_res = mock.Mock()
            mock_res.returncode = 0
            mock_res.stdout = "15.500000\n"

            with mock.patch("subprocess.run", return_value=mock_res) as mock_subproc:
                dur = probe_audio_duration(dummy_path)
                self.assertAlmostEqual(dur, 15.5, places=2)
                mock_subproc.assert_called_once()

    def test_preserve_pristine_background_chunked_streaming(self):
        """Verify chunked streaming preserves audio fidelity with continuous SOS filtering and low memory."""
        sr = 48000
        dur = 6.0
        samples = int(sr * dur)
        t = np.linspace(0, dur, samples, endpoint=False)

        # 440 Hz sine wave background, 880 Hz separated
        orig = np.sin(2 * np.pi * 440 * t)[:, np.newaxis]
        sep = np.sin(2 * np.pi * 880 * t)[:, np.newaxis]

        with tempfile.TemporaryDirectory() as td:
            orig_path = os.path.join(td, "orig.wav")
            sep_path = os.path.join(td, "sep.wav")
            out_path = os.path.join(td, "enhanced_chunked.wav")

            sf.write(orig_path, orig, sr)
            sf.write(sep_path, sep, sr)

            # Speech occurs between 2.0s and 4.0s
            segments = [{"start": 2.0, "end": 4.0}]

            # Force 1.5s chunk processing across a 6.0s file (4 chunk transitions)
            res_path = preserve_pristine_background(
                orig_path,
                sep_path,
                segments,
                out_path,
                pad_ms=50.0,
                crossfade_ms=20.0,
                min_gap_ms=200.0,
                restore_high_freq=True,
                chunk_seconds=1.5,
            )

            self.assertTrue(os.path.isfile(res_path))
            out_data, out_sr = sf.read(res_path)
            self.assertEqual(out_sr, sr)
            self.assertEqual(len(out_data), samples)
            if out_data.ndim == 1:
                out_data = out_data[:, np.newaxis]

            # At t = 0.5s (non-speech): matches original
            sample_05 = int(0.5 * sr)
            self.assertAlmostEqual(float(out_data[sample_05, 0]), float(orig[sample_05, 0]), places=3)

            # At t = 3.0s (speech): matches separated
            sample_30 = int(3.0 * sr)
            self.assertAlmostEqual(float(out_data[sample_30, 0]), float(sep[sample_30, 0]), places=1)

    def test_gemini_timing_rewriter_uses_headers_without_key_in_url(self):
        """Verify Gemini timing rewriter passes x-goog-api-key header and keeps key out of URL."""
        api_key = "secret_gemini_timing_key_123"
        rewriter = GeminiTimingRewriter(api_key=api_key, models=["gemini-3.8-flash"])
        req = RewriteRequest(
            segment_index=1,
            text="Câu này hơi dài cần viết lại",
            max_characters=15,
            target_seconds=1.0,
            source_segment_id=1,
        )

        mock_resp = mock.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": '[{"id": 1, "text": "Câu ngắn hơn"}]'}
                        ]
                    }
                }
            ]
        }

        with mock.patch("backend.pipeline_v2.timing._check_gemini_available", return_value=True):
            with mock.patch("requests.post", return_value=mock_resp) as mock_post:
                res = rewriter([req])
                self.assertEqual(res, {1: "Câu ngắn hơn"})
                self.assertEqual(mock_post.call_count, 1)

                called_url = mock_post.call_args[0][0]
                called_headers = mock_post.call_args[1].get("headers", {})

                # Ensure key is NOT in query string of the URL
                self.assertNotIn("?key=", called_url)
                self.assertNotIn(api_key, called_url)

                # Ensure key is in header
                self.assertEqual(called_headers.get("x-goog-api-key"), api_key)

    def test_render_fallback_to_libx264_on_nvenc_failure(self):
        """Verify process_video falls back to libx264 when h264_nvenc fails."""
        with tempfile.TemporaryDirectory() as td:
            video_path = os.path.join(td, "in.mp4")
            sub_path = os.path.join(td, "in.ass")
            audio_path = os.path.join(td, "in.wav")
            out_path = os.path.join(td, "out.mp4")

            # Create dummy input files
            with open(video_path, "wb") as f:
                f.write(b"0" * 1024)
            with open(sub_path, "w", encoding="utf-8") as f:
                f.write("[Script Info]\nTitle: Test\n[Events]\nFormat: Layer, Start, End, Text\n")
            with open(audio_path, "wb") as f:
                f.write(b"0" * 1024)

            # Mock cv2.VideoCapture to return valid dimensions
            mock_cap = mock.Mock()
            mock_cap.get.side_effect = lambda prop: 1920 if prop == 3 else 1080

            # Mock build_canvas_render_graph and subprocess.run
            def fake_subprocess_run(cmd, *args, **kwargs):
                cmd_str = " ".join(cmd)
                mock_proc = mock.Mock()
                if "h264_nvenc" in cmd_str:
                    # Simulate NVENC error (e.g. session exhaustion)
                    mock_proc.returncode = 1
                    mock_proc.stderr = "Error: Out of NVENC encoder sessions / CUDA failed"
                elif "libx264" in cmd_str:
                    # Simulate CPU success: create output file
                    with open(out_path, "wb") as f:
                        f.write(b"x" * 20000)
                    mock_proc.returncode = 0
                    mock_proc.stderr = ""
                else:
                    mock_proc.returncode = 1
                    mock_proc.stderr = "Unknown encoder"
                return mock_proc

            with mock.patch("cv2.VideoCapture", return_value=mock_cap):
                with mock.patch("video_utils.build_canvas_render_graph", return_value=([], [], "0:v")):
                    with mock.patch("subprocess.run", side_effect=fake_subprocess_run) as mock_subproc:
                        ok = process_video(
                            video_path=video_path,
                            srt_path=sub_path,
                            mixed_audio_path=audio_path,
                            output_video_path=out_path,
                        )
                        self.assertTrue(ok)
                        # Must have tried h264_nvenc first, then fallen back to libx264
                        self.assertGreaterEqual(mock_subproc.call_count, 2)
                        last_cmd = mock_subproc.call_args[0][0]
                        self.assertIn("libx264", last_cmd)
                        self.assertNotIn("-hwaccel", last_cmd)


if __name__ == "__main__":
    unittest.main()
