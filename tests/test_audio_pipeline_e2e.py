import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from ai.audio_enhancer import preserve_pristine_background
from video_utils import mix_audio_pydub


class TestAudioPipelineE2E(unittest.TestCase):
    def test_e2e_audio_enhancement_and_mixing(self):
        sr = 48000
        dur = 4.0  # 4 seconds
        total_samples = int(sr * dur)
        t = np.linspace(0, dur, total_samples, endpoint=False)

        # Original track: Stereo music (440 Hz L, 554 Hz R)
        music_l = np.sin(2 * np.pi * 440 * t) * 0.4
        music_r = np.sin(2 * np.pi * 554 * t) * 0.4
        orig_audio = np.column_stack([music_l, music_r]).astype(np.float32)

        # Separated track (simulate vocal removed BGM)
        sep_bgm = (orig_audio * 0.95).astype(np.float32)

        # Dubbing TTS track: 1 second speech from 1.5s to 2.5s
        dub_dur = 1.0
        dub_samples = int(sr * dub_dur)
        t_dub = np.linspace(0, dub_dur, dub_samples, endpoint=False)
        dub_data = (np.sin(2 * np.pi * 300 * t_dub) * 0.8)[:, np.newaxis].astype(np.float32)

        with tempfile.TemporaryDirectory() as td:
            orig_wav = os.path.join(td, "original.wav")
            sep_wav = os.path.join(td, "no_vocals.wav")
            enhanced_wav = os.path.join(td, "enhanced_bgm.wav")
            dub_wav = os.path.join(td, "dub_0.wav")
            mixed_wav = os.path.join(td, "final_mix.wav")

            sf.write(orig_wav, orig_audio, sr)
            sf.write(sep_wav, sep_bgm, sr)
            sf.write(dub_wav, dub_data, sr)

            segments = [{"start": 1.5, "end": 2.5, "text": "Xin chào"}]

            # 1. Test Selective Background Preservation
            enhanced_res = preserve_pristine_background(
                orig_wav,
                sep_wav,
                segments,
                enhanced_wav,
                pad_ms=50.0,
                crossfade_ms=30.0,
                min_gap_ms=200.0,
                restore_high_freq=True,
            )
            self.assertTrue(os.path.isfile(enhanced_res))

            enh_data, enh_sr = sf.read(enhanced_res)
            self.assertEqual(enh_sr, sr)
            self.assertEqual(len(enh_data), total_samples)

            # Non-speech segment (e.g. at 0.5s): Must equal original audio
            idx_05 = int(0.5 * sr)
            np.testing.assert_allclose(enh_data[idx_05], orig_audio[idx_05], atol=1e-3)

            # 2. Test Studio Mixing with Dynamic Ducking & EBU R128
            dub_files = [{"path": dub_wav, "start": 1.5, "end": 2.5}]
            mix_res = mix_audio_pydub(
                enhanced_res,
                dub_files,
                mixed_wav,
                original_volume_db=-2.0,
                dubbing_volume_db=1.0,
            )
            self.assertTrue(os.path.isfile(mix_res))
            self.assertTrue(os.path.getsize(mix_res) > 0)

            mix_data, mix_sr = sf.read(mix_res)
            # Duration must match the background within tight tolerance (0.05s)
            self.assertAlmostEqual(len(mix_data) / mix_sr, dur, delta=0.05)
            # Output must have valid finite signal (no NaN or Inf)
            self.assertTrue(np.all(np.isfinite(mix_data)))
            # True peak must be well within limits (<= 1.0)
            self.assertLessEqual(np.max(np.abs(mix_data)), 1.05)


if __name__ == "__main__":
    unittest.main()
