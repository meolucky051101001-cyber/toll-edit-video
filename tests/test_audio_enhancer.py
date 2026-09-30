import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from ai.audio_enhancer import (
    build_speech_mask,
    extract_high_frequencies,
    preserve_pristine_background,
    resample_audio,
)


class TestAudioEnhancer(unittest.TestCase):
    def test_build_speech_mask(self):
        sr = 48000
        total_samples = 48000 * 3  # 3 seconds
        segments = [{"start": 1.0, "end": 2.0}]

        mask = build_speech_mask(
            total_samples,
            sr,
            segments,
            pad_ms=100.0,
            crossfade_ms=40.0,
            min_gap_ms=400.0,
        )
        self.assertEqual(len(mask), total_samples)
        # At start (0.1s), mask should be 0.0 (non-speech)
        self.assertEqual(mask[int(0.1 * sr)], 0.0)
        # At center of speech (1.5s), mask should be 1.0
        self.assertEqual(mask[int(1.5 * sr)], 1.0)
        # At end (2.8s), mask should be 0.0
        self.assertEqual(mask[int(2.8 * sr)], 0.0)
        # Check smooth gradient around transition
        start_transition = mask[int(0.85 * sr) : int(0.95 * sr)]
        self.assertTrue(np.all(np.diff(start_transition) >= 0.0))

    def test_preserve_pristine_background(self):
        sr = 48000
        dur = 3.0
        samples = int(sr * dur)
        t = np.linspace(0, dur, samples, endpoint=False)

        # Original audio: 440 Hz sine wave (representing pristine background music)
        orig = np.sin(2 * np.pi * 440 * t)[:, np.newaxis]
        # Separated audio: 880 Hz sine wave
        sep = np.sin(2 * np.pi * 880 * t)[:, np.newaxis]

        with tempfile.TemporaryDirectory() as td:
            orig_path = os.path.join(td, "orig.wav")
            sep_path = os.path.join(td, "sep.wav")
            out_path = os.path.join(td, "enhanced.wav")

            sf.write(orig_path, orig, sr)
            sf.write(sep_path, sep, sr)

            # Speech occurs between 1.0s and 2.0s
            segments = [{"start": 1.0, "end": 2.0}]

            res_path = preserve_pristine_background(
                orig_path,
                sep_path,
                segments,
                out_path,
                pad_ms=50.0,
                crossfade_ms=20.0,
                min_gap_ms=200.0,
                restore_high_freq=True,
            )

            self.assertTrue(os.path.isfile(res_path))
            out_data, out_sr = sf.read(res_path)
            self.assertEqual(out_sr, sr)
            self.assertEqual(len(out_data), samples)
            if out_data.ndim == 1:
                out_data = out_data[:, np.newaxis]

            # At t = 0.2s (well outside speech): should match orig EXACTLY
            sample_02 = int(0.2 * sr)
            self.assertAlmostEqual(float(out_data[sample_02, 0]), float(orig[sample_02, 0]), places=3)

            # At t = 1.5s (inside speech): should be predominantly sep (880 Hz)
            sample_15 = int(1.5 * sr)
            self.assertAlmostEqual(float(out_data[sample_15, 0]), float(sep[sample_15, 0]), places=1)

    def test_empty_segments_fallback(self):
        sr = 44100
        orig = np.ones((44100, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory() as td:
            orig_path = os.path.join(td, "orig.wav")
            sep_path = os.path.join(td, "sep.wav")
            out_path = os.path.join(td, "out.wav")
            sf.write(orig_path, orig, sr)
            sf.write(sep_path, orig * 0.5, sr)

            res = preserve_pristine_background(orig_path, sep_path, [], out_path)
            out_data, out_sr = sf.read(res)
            self.assertEqual(out_sr, sr)
            np.testing.assert_allclose(out_data, orig, atol=1e-4)

    def test_sample_rate_and_channel_mismatch(self):
        orig_sr = 48000
        sep_sr = 44100
        dur = 2.0
        t_orig = np.linspace(0, dur, int(orig_sr * dur), endpoint=False)
        t_sep = np.linspace(0, dur, int(sep_sr * dur), endpoint=False)

        # Stereo original, Mono separated
        orig = np.column_stack([np.sin(2 * np.pi * 440 * t_orig), np.cos(2 * np.pi * 440 * t_orig)])
        sep = np.sin(2 * np.pi * 880 * t_sep)[:, np.newaxis]

        with tempfile.TemporaryDirectory() as td:
            orig_path = os.path.join(td, "orig.wav")
            sep_path = os.path.join(td, "sep.wav")
            out_path = os.path.join(td, "out.wav")
            sf.write(orig_path, orig, orig_sr)
            sf.write(sep_path, sep, sep_sr)

            segments = [{"start": 0.5, "end": 1.5}]
            res = preserve_pristine_background(orig_path, sep_path, segments, out_path)
            out_data, out_sr = sf.read(res)
            self.assertEqual(out_sr, orig_sr)
            self.assertEqual(out_data.shape[1], 2)
            self.assertEqual(len(out_data), len(orig))


if __name__ == "__main__":
    unittest.main()
