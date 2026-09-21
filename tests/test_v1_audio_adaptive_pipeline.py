"""
Unit tests for Tool V1 Adaptive Audio Pipeline:
1. Adaptive Auto-Ducking (RMS-based, smooth transitions, anti-pumping)
2. True Peak Limiter / Anti-clipping (Peak <= -1.0 dBFS)
3. Gap Detection & Conditional ASR filtering
4. OCR Subtitle Fallback (recovering missing speech in Subtitle Band)
"""

import math
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import srt
from pydub import AudioSegment
from pydub.generators import Sine

from backend.v1_audio_mixer import (
    AdaptiveDuckingSettings,
    apply_adaptive_ducking,
    apply_peak_limiter,
    calculate_adaptive_duck_gain,
    merge_ducking_intervals,
    mix_adaptive_audio,
)
from backend.ai.v1_conditional_asr import (
    detect_suspicious_gaps,
    filter_gaps_with_energy,
    is_chinese_text,
    is_hallucination,
)
from backend.ocr_utils import recover_missing_subtitles_from_ocr
from backend.ocr_subtitle_locator import SubtitleBandSelection


class TestV1AdaptiveAudioPipeline(unittest.TestCase):

    def test_calculate_adaptive_duck_gain(self):
        settings = AdaptiveDuckingSettings(
            soft_duck_db=-5.0,
            moderate_duck_db=-9.0,
            loud_duck_db=-13.0,
            soft_bgm_threshold_dbfs=-25.0,
            loud_bgm_threshold_dbfs=-15.0,
        )
        # Soft BGM -> -5 dB
        self.assertAlmostEqual(calculate_adaptive_duck_gain(-30.0, settings), -5.0, places=1)
        # Loud BGM -> -13 dB
        self.assertAlmostEqual(calculate_adaptive_duck_gain(-10.0, settings), -13.0, places=1)
        # Moderate BGM (-20 dBFS) -> midpoint: -9 dB
        self.assertAlmostEqual(calculate_adaptive_duck_gain(-20.0, settings), -9.0, places=1)
        # Silent BGM -> 0 dB (no ducking needed)
        self.assertEqual(calculate_adaptive_duck_gain(-70.0, settings), 0.0)

    def test_merge_ducking_intervals_anti_pumping(self):
        # Two dubs close to each other (pause = 200ms < gap_merge_ms 400ms)
        dubs = [
            {"start": 1.0, "actual_audio_duration": 1.0},  # 1.0s to 2.0s
            {"start": 2.2, "actual_audio_duration": 1.0},  # 2.2s to 3.2s
        ]
        # Should be merged into a single smooth ducking envelope
        merged = merge_ducking_intervals(dubs, total_duration_ms=5000, attack_ms=150, release_ms=350, gap_merge_ms=400)
        self.assertEqual(len(merged), 1)
        # Interval starts before 1.0s and ends after 3.2s
        self.assertTrue(merged[0][0] < 1000)
        self.assertTrue(merged[0][1] > 3200)

        # Two dubs far apart (pause = 2.0s > gap_merge_ms 400ms)
        dubs_far = [
            {"start": 1.0, "actual_audio_duration": 1.0},
            {"start": 4.0, "actual_audio_duration": 1.0},
        ]
        merged_far = merge_ducking_intervals(dubs_far, total_duration_ms=8000, attack_ms=150, release_ms=350, gap_merge_ms=400)
        self.assertEqual(len(merged_far), 2)

    def test_peak_limiter_prevents_clipping(self):
        # Generate loud audio near 0 dBFS
        loud_audio = Sine(440).to_audio_segment(duration=500, volume=0.0)
        # Boost volume to +6dB (would clip without limiter)
        boosted = loud_audio + 6.0
        self.assertTrue(boosted.max_dBFS >= -0.1)

        limited = apply_peak_limiter(boosted, max_peak_dbfs=-1.0)
        self.assertLessEqual(limited.max_dBFS, -0.99)

    def test_mix_adaptive_audio_full_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            bgm_path = os.path.join(tmpdir, "bgm.wav")
            dub1_path = os.path.join(tmpdir, "dub1.wav")
            out_path = os.path.join(tmpdir, "mixed.wav")

            # 4 seconds BGM
            bgm = Sine(220).to_audio_segment(duration=4000, volume=-10.0)
            bgm.export(bgm_path, format="wav")

            # 1 second voice at t=1.0s
            dub1 = Sine(880).to_audio_segment(duration=1000, volume=-5.0)
            dub1.export(dub1_path, format="wav")

            dubs = [{"path": dub1_path, "start": 1.0, "actual_audio_duration": 1.0}]

            result = mix_adaptive_audio(
                bgm_path=bgm_path,
                dubbing_audio_files=dubs,
                output_path=out_path,
                base_bgm_gain_db=-2.0,
                base_voice_gain_db=1.0,
            )
            self.assertTrue(os.path.exists(result))
            mixed = AudioSegment.from_file(result)
            self.assertAlmostEqual(len(mixed), 4000, delta=100)
            self.assertLessEqual(mixed.max_dBFS, -1.0)

    def test_gap_detection_and_energy_filtering(self):
        # Subtitles covering 1.0s-2.0s and 6.0s-7.0s in a 10s video
        subs = [
            srt.Subtitle(index=1, start=timedelta(seconds=1.0), end=timedelta(seconds=2.0), content="第一句"),
            srt.Subtitle(index=2, start=timedelta(seconds=6.0), end=timedelta(seconds=7.0), content="第二句"),
        ]
        gaps = detect_suspicious_gaps(subs, total_duration_seconds=10.0, min_gap_seconds=2.5)
        # Gap between 2.0s and 6.0s (duration = 4.0s >= 2.5s)
        # Gap at the end: 7.0s to 10.0s (duration = 3.0s >= 2.0s)
        self.assertTrue(any(g[0] == 2.0 and g[1] == 6.0 for g in gaps))

        # Test energy filter with real audio
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "test.wav")
            # Create audio with sound between 2.0s and 6.0s, but silent after 7.0s
            part1 = AudioSegment.silent(duration=2000)
            part2 = Sine(440).to_audio_segment(duration=4000, volume=-15.0)
            part3 = AudioSegment.silent(duration=4000)
            full = part1 + part2 + part3
            full.export(audio_path, format="wav")

            verified = filter_gaps_with_energy(audio_path, gaps, min_energy_dbfs=-35.0)
            # Gap [2.0, 6.0] has tone (-15 dBFS >= -35 dBFS), so it MUST be kept
            self.assertTrue(any(g[0] == 2.0 and g[1] == 6.0 for g in verified))
            # Gap [7.0, 10.0] is pure silence (-inf < -35 dBFS), so it MUST be filtered out
            self.assertFalse(any(g[0] == 7.0 and g[1] == 10.0 for g in verified))

    def test_chinese_text_and_hallucination_detection(self):
        self.assertTrue(is_chinese_text("今天天气真好"))
        self.assertFalse(is_chinese_text("Hello world 123"))
        self.assertTrue(is_hallucination("啊啊啊啊啊啊啊"))
        self.assertTrue(is_hallucination("谢谢观看 请不吝点赞"))
        self.assertFalse(is_hallucination("今天给大家分享一款超好用的收纳盒"))

    def test_recover_missing_subtitles_from_ocr(self):
        # ASR only recognized 0.0s - 2.0s
        subs = [
            srt.Subtitle(index=1, start=timedelta(seconds=0.0), end=timedelta(seconds=2.0), content="这是第一句话"),
        ]
        # Simulated Chinese Subtitle Band at Y: 0.80 - 0.85
        band = SubtitleBandSelection(
            top=0.80,
            bottom=0.85,
            mode="asr_match",
            support=10,
            selected_by_segment={},
            candidate_count=20,
        )
        # OCR blocks:
        # 1. Inside band, at 4.0s and 5.0s (missed by ASR!)
        # 2. Outside band (e.g. logo at Y: 0.10)
        all_blocks = [
            {"text": "第二句丢失的台词", "y_pct": 0.82, "sample_time": 4.0, "prob": 0.90},
            {"text": "第二句丢失的台词", "y_pct": 0.82, "sample_time": 5.0, "prob": 0.92},
            {"text": "顶部广告水印", "y_pct": 0.10, "sample_time": 4.5, "prob": 0.95},
        ]

        recovered = recover_missing_subtitles_from_ocr(all_blocks, subs, band, duration=10.0)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["text"], "第二句丢失的台词")
        self.assertTrue(recovered[0]["start"] <= 4.0)
        self.assertTrue(recovered[0]["end"] >= 5.0)


if __name__ == "__main__":
    unittest.main()
