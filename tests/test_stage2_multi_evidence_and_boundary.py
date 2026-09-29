import unittest
from pathlib import Path
import sys

backend_dir = Path(__file__).resolve().parent.parent / "backend"
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from pipeline_v2.reconcile import (
    classify_text_span,
    measure_vocal_rms,
    reconcile_asr_gaps,
    build_domain_tracks,
    ReconciledSpan,
)
from pipeline_v2.domain import SourceTextTrack


class TestStage2MultiEvidenceAndBoundary(unittest.TestCase):
    def test_short_spans_preserved(self):
        """Spans under 0.55s must not be silently dropped."""
        gap_dets = [
            {
                "time": 10.0,
                "text": "快点",
                "bbox": [0.4, 0.75, 0.6, 0.82],
                "in_subtitle_band": True,
                "prob": 0.95,
            },
            {
                "time": 10.2,
                "text": "快点",
                "bbox": [0.4, 0.75, 0.6, 0.82],
                "in_subtitle_band": True,
                "prob": 0.95,
            },
        ]
        spans, dicts = reconcile_asr_gaps(gap_dets, [], None, video_duration=30.0)
        self.assertGreaterEqual(len(spans), 1)
        self.assertEqual(spans[0].text, "快点")
        # Ensure it has valid duration and wasn't dropped
        self.assertGreater(spans[0].end, spans[0].start)

    def test_packaging_keyword_classification(self):
        """Packaging text at bottom of screen must be classified as packaging_or_watermark."""
        # Packaging keyword "净含量: 500g" at bottom of screen
        cls = classify_text_span(
            text="净含量: 500g",
            in_subtitle_band=True,
            vocal_rms_db=-30.0,
            bbox=[0.3, 0.78, 0.7, 0.83],
            audio_status="ok",
        )
        self.assertEqual(cls, "packaging_or_watermark")

        # "配料表: 水, 白砂糖"
        cls2 = classify_text_span(
            text="配料表: 水, 白砂糖",
            in_subtitle_band=True,
            vocal_rms_db=-25.0,
            bbox=[0.2, 0.80, 0.8, 0.85],
            audio_status="ok",
        )
        self.assertEqual(cls2, "packaging_or_watermark")

    def test_audio_unknown_leads_to_uncertain(self):
        """Audio read failure/unknown status must lead to uncertain, not unvoiced."""
        cls = classify_text_span(
            text="我们快走吧",
            in_subtitle_band=True,
            vocal_rms_db=-100.0,
            bbox=[0.2, 0.78, 0.8, 0.83],
            audio_status="unknown",
        )
        self.assertEqual(cls, "uncertain")

    def test_uncertain_sets_needs_review(self):
        """Uncertain spans must have needs_review=True in SourceTextTrack."""
        span = ReconciledSpan(
            span_id="gap_span_0001",
            first_time=5.0,
            last_time=6.0,
            start=5.0,
            end=6.0,
            text="这是一个测试句子",
            bbox=[0.2, 0.78, 0.8, 0.83],
            classification="uncertain",
            vocal_rms_db=-100.0,
            confidence=0.5,
            sample_count=2,
            in_subtitle_band=True,
        )
        tracks, subs, dubs = build_domain_tracks([span], [])
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].needs_review)
        self.assertIsNotNone(tracks[0].review_reason)


if __name__ == "__main__":
    unittest.main()
