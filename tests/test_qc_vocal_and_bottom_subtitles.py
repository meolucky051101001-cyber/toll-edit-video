"""Unit tests for QC vocal audibility stem checking, bottom-edge OCR subtitle coverage,
and rim leak detection.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

_backend_dir = str(Path(__file__).resolve().parents[1] / "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

import numpy as np
import soundfile as sf

from backend.pipeline_v2.qc import (
    QCSettings,
    _check_mixed_audio_audibility,
    run_report_only_qc,
)
from backend.ocr_subtitle_locator import _geometry_score
from backend.ocr_utils import _has_chinese_overlap
from backend.pipeline_v2.cover_qc import inspect_frame_pixel_coverage, check_watermark_collision


class QcVocalStemAudibilityTests(unittest.TestCase):
    def test_loud_bgm_with_silent_vocal_stem_fails_audibility_check(self):
        """When mixed audio is loud (-18 dBFS) but the vocal stem is silent (-60 dBFS),

        the QC check must flag the segment as silent_vocal and return an error status.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sr = 16000
            duration = 1.0
            t = np.linspace(0, duration, int(sr * duration), endpoint=False)

            # Mixed audio: loud sine wave (~ -18 dBFS, amplitude ~ 0.125)
            mixed_data = 0.125 * np.sin(2 * np.pi * 440 * t)
            mixed_file = tmp_path / "mixed.wav"
            sf.write(str(mixed_file), mixed_data, sr)

            # Vocal stem: virtually silent (~ -60 dBFS, amplitude ~ 0.001)
            vocal_data = 0.001 * np.sin(2 * np.pi * 440 * t)
            vocal_file = tmp_path / "vocal_seg1.wav"
            sf.write(str(vocal_file), vocal_data, sr)

            segments = [
                {
                    "index": 1,
                    "id": 1,
                    "start": 0.0,
                    "end": 1.0,
                    "content": "Lời thoại kiểm tra",
                    "audio_path": str(vocal_file),
                }
            ]

            metrics, checks = _check_mixed_audio_audibility(mixed_file, segments)

            self.assertIn(1, metrics.get("silent_vocal_ids", []))
            self.assertIn(1, metrics.get("inaudible_segment_ids", []))
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0].status, "error")
            self.assertIn("missing vocal stem", checks[0].message)

    def test_audible_vocal_stem_passes_audibility_check(self):
        """When vocal stem has normal audible level (-14 dBFS), audibility check passes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            sr = 16000
            duration = 1.0
            t = np.linspace(0, duration, int(sr * duration), endpoint=False)

            # Both mixed and vocal are audible (amplitude ~ 0.2)
            audible_data = 0.2 * np.sin(2 * np.pi * 440 * t)
            mixed_file = tmp_path / "mixed.wav"
            vocal_file = tmp_path / "vocal_seg1.wav"
            sf.write(str(mixed_file), audible_data, sr)
            sf.write(str(vocal_file), audible_data, sr)

            segments = [
                {
                    "index": 1,
                    "id": 1,
                    "start": 0.0,
                    "end": 1.0,
                    "content": "Lời thoại kiểm tra",
                    "audio_path": str(vocal_file),
                }
            ]

            metrics, checks = _check_mixed_audio_audibility(mixed_file, segments)

            self.assertEqual(len(metrics.get("silent_vocal_ids", [])), 0)
            self.assertEqual(len(metrics.get("inaudible_segment_ids", [])), 0)
            self.assertEqual(len(checks), 1)
            self.assertEqual(checks[0].status, "pass")


class BottomEdgeSubtitleAndRimQcTests(unittest.TestCase):
    def test_geometry_score_accepts_subtitles_near_bottom_edge(self):
        """Subtitles between 93% and 98% height must receive a valid score (not rejected)."""
        # Box from y=0.92 to y=0.975 (Douyin subtitle range)
        block_bottom = {
            "x_pct": 0.1,
            "max_x_pct": 0.9,
            "y_pct": 0.92,
            "max_y_pct": 0.975,
            "prob": 0.9,
        }
        score_bottom = _geometry_score(block_bottom, 1080, 1920)
        self.assertIsNotNone(score_bottom)
        self.assertGreater(score_bottom, 0.0)

        # Extremely out of bounds (> 0.995) should be rejected
        block_invalid = {
            "x_pct": 0.1,
            "max_x_pct": 0.9,
            "y_pct": 0.996,
            "max_y_pct": 1.02,
            "prob": 0.9,
        }
        score_invalid = _geometry_score(block_invalid, 1080, 1920)
        self.assertIsNone(score_invalid)

    def test_rim_leak_detected_when_text_extends_outside_cover_margin(self):
        """Pixel cover QC detects rim leaks if high-contrast edges appear just outside the cover box."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "frame.png"
            # 1080x1920 image (H=1920, W=1080)
            frame = np.full((1920, 1080, 3), 30, dtype=np.uint8)

            # Draw a white cover box at [1700, 1800], [200, 880]
            frame[1700:1801, 200:881] = 255

            # Place high contrast Chinese text just below the cover box (y=1805 to 1812)
            frame[1805:1812, 300:700] = 240  # bright text leaking out on dark background

            from PIL import Image
            Image.fromarray(frame).save(img_path)

            covers = [(0.0, 1.0, 200, 1700, 880, 1800)]
            # Expected region extending past cover down to 1815
            expected_regions = [{
                "segment_id": 1,
                "x_pct": 200 / 1080,
                "y_pct": 1700 / 1920,
                "max_x_pct": 880 / 1080,
                "max_y_pct": 1815 / 1920,
            }]

            res = inspect_frame_pixel_coverage(
                img_path,
                covers,
                canvas_w=1080,
                canvas_h=1920,
                timestamp=0.5,
                expected_regions=expected_regions,
            )
            self.assertFalse(res.get("all_boxes_filled", True))
            self.assertTrue(res.get("overflow_detected", False))
            self.assertTrue(any(c.get("rim_leak_detected") for c in res.get("expected_region_checks", [])))

    def test_chinese_overlap_filter_rejects_unrelated_ocr_candidate(self):
        """_has_chinese_overlap rejects OCR candidates that do not share Chinese characters with segment."""
        # Mismatched candidates (like câu 29 or câu 100 error)
        candidate = "今天天气真好"
        segment_orig = "这道题怎么做"
        self.assertFalse(_has_chinese_overlap(candidate, segment_orig))

        # Partial match with shared characters
        candidate_match = "这道题目很难"
        self.assertTrue(_has_chinese_overlap(candidate_match, segment_orig))


class WatermarkQcTests(unittest.TestCase):
    def test_watermark_reports_info_when_no_box_defined(self):
        """When watermark_box is None, check_watermark_collision returns no collision."""
        covers = [(0.0, 1.0, 200, 1700, 880, 1800)]
        res = check_watermark_collision(covers, 1080, 1920, watermark_box=None)
        self.assertFalse(res["has_collision"])
        self.assertEqual(res["collision_count"], 0)
        self.assertEqual(res["checked_covers"], 1)

    def test_watermark_collision_detected_when_cover_overlaps_watermark_box(self):
        """When a cover overlaps the watermark box in the bottom right, collision is detected."""
        covers = [(0.0, 1.0, 700, 1750, 1050, 1850)]
        # watermark box at bottom right: x1=800, y1=1700, x2=1050, y2=1900
        wm_box = (800, 1700, 1050, 1900)
        res = check_watermark_collision(covers, 1080, 1920, watermark_box=wm_box)
        self.assertTrue(res["has_collision"])
        self.assertEqual(res["collision_count"], 1)


class ReconciliationTests(unittest.TestCase):
    def test_reconcile_clusters_interleaved_samples_and_classifies_dialogue_vs_logo(self):
        """Interleaved watermark samples must not break subtitle clustering, and classes must separate."""
        from backend.pipeline_v2.reconcile import (
            cluster_gap_detections,
            classify_text_span,
            build_segments_from_spans,
            ReconciledSpan,
        )

        raw_samples = [
            {"time": 148.32, "text": "老妈带他们把硝石全倒进冰水里搅匀", "bbox": [0.21, 0.90, 0.78, 0.96], "in_subtitle_band": True},
            {"time": 149.00, "text": "@鹿茸茸", "bbox": [0.38, 0.49, 0.57, 0.59], "in_subtitle_band": False},
            {"time": 150.32, "text": "老妈带他们把硝石全倒进冰水里搅匀", "bbox": [0.21, 0.90, 0.78, 0.96], "in_subtitle_band": True},
            {"time": 151.32, "text": "老妈带他们把硝石全倒进冰水里搅匀", "bbox": [0.21, 0.90, 0.78, 0.96], "in_subtitle_band": True},
        ]
        clusters = cluster_gap_detections(raw_samples)
        self.assertEqual(len(clusters), 2)

        # Dialogue cluster merged across the watermark interruption
        dialogue_cluster = next(c for c in clusters if c["in_subtitle_band"])
        self.assertEqual(len(dialogue_cluster["samples"]), 3)
        self.assertAlmostEqual(dialogue_cluster["first_time"], 148.32, places=2)
        self.assertAlmostEqual(dialogue_cluster["last_time"], 151.32, places=2)

        # Classification check
        d_class = classify_text_span(dialogue_cluster["text"], True, -16.5, dialogue_cluster["bbox"])
        self.assertEqual(d_class, "spoken_dialogue")

        logo_cluster = next(c for c in clusters if not c["in_subtitle_band"])
        l_class = classify_text_span(logo_cluster["text"], False, -20.0, logo_cluster["bbox"])
        self.assertIn(l_class, ("scene_packaging_or_logo", "packaging_or_watermark"))

    def test_reconcile_recovers_six_user_issue_dialogue_segments(self):
        """Simulate the 6 user-reported timestamps and verify all recover as spoken_dialogue RuntimeSegments."""
        from backend.pipeline_v2.reconcile import (
            reconcile_asr_gaps,
            build_segments_from_spans,
            ReconciledSpan,
        )

        # 6 timestamps from user report with corresponding text and vocal levels
        issue_cases = [
            (117.40, "敌袭全员醒来", -16.8),
            (144.32, "大毛二毛三毛去储藏屋搬硝石粉", -15.7),
            (150.32, "老妈带他们把硝石全倒进冰水里搅匀", -17.3),
            (321.17, "一群恐怖虫子来了", -13.3),
            (636.98, "来尝尝咱们自酿的大山特供糯米酿", -16.5),
            (1087.66, "好烫好辣", -12.3),
        ]

        spans = []
        for idx, (t, text, rms) in enumerate(issue_cases, 1):
            spans.append(
                ReconciledSpan(
                    span_id=f"gap_span_{idx:04d}",
                    first_time=t,
                    last_time=t + 1.5,
                    start=t - 0.1,
                    end=t + 2.0,
                    text=text,
                    bbox=[0.25, 0.90, 0.75, 0.97],
                    classification="spoken_dialogue",
                    vocal_rms_db=rms,
                    confidence=0.95,
                    sample_count=2,
                    in_subtitle_band=True,
                    translated_vietnamese=f"Dịch {text}",
                )
            )

        new_segments = build_segments_from_spans(spans)
        self.assertEqual(len(new_segments), 6)
        for seg in new_segments:
            self.assertTrue(getattr(seg, "is_gap_reconciled", False))
            self.assertTrue(seg.is_subtitle)
            self.assertTrue(seg.in_subtitle_band)
            self.assertGreater(len(seg.content), 0)
            self.assertIsNotNone(seg.best_block)


if __name__ == "__main__":
    unittest.main()

