"""Comprehensive tests for the expected cover timeline and production QC path.

Phase 1 (Đợt 1, 1.1 & 1.2) Test Suite:
1. Genuine subtitle in upper screen (y_pct < 0.45) is included.
2. Packaging / background text in lower screen (y_pct > 0.45) is excluded.
3. OCR starting earlier and ending later than ASR preserves full OCR bounds.
4. Segment without OCR geometry emits no fake cover event.
5. Multiple tracking blocks separated by >1.0s gap split into separate visual events.
6. Overlapping and nested OCR blocks merge into a single continuous visual event [cluster_end = max(end)].
7. Adjacent subtitles within 1.0s bridge continuously.
8. Adjacent subtitles with vertical shift set position_changed flag.
9. Clamping at video duration.
10. Deterministic, order-invariant handling of simultaneous events (both hold to 5s regardless of list order).
11. Long timeline scalability (>30 segments).
12. Real production plan_diagnostic_samples guarantees <= 30 frames inclusive of baseline.
13. Integration test: run_report_only_qc strictly caps FFmpeg extraction calls <= 30.
14. Classification metadata serialization roundtrip (GeometryBlock and RuntimeSegment).
15. Real OCR output flow: OCRBlock assigns metadata, preserved across merge and JSON, ingested by timeline.
16. Failure detection: missing cover in ASS.
17. Failure detection: late cover onset.
18. Failure detection: early cover exit.
19. Insufficient hold (0.5s instead of 1.0s) fails closed.
20. Unbridged gap (100ms) between adjacent subtitles fails closed.
"""

from datetime import timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

_backend_dir = str(Path(__file__).resolve().parents[1] / "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

import numpy as np
from PIL import Image

from backend.ocr_utils import OCRBlock
from backend.pipeline_v2.content import merge_ocr_geometry
from backend.pipeline_v2.cover_qc import (
    build_expected_cover_timeline,
    ExpectedCoverEvent,
)
from backend.pipeline_v2.qc import run_report_only_qc, QCSettings, plan_diagnostic_samples
from backend.pipeline_v2.segments import (
    GeometryBlock,
    RuntimeSegment,
    segment_to_dict,
    segment_from_dict,
    segments_to_dicts,
    segments_from_dicts,
)


def _create_synthetic_frame(file_path: Path, width: int = 1080, height: int = 1920, white_box: tuple = None):
    """Generate real frame image on disk to ensure real pixel QC executes without bypass."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    if white_box:
        x1, y1, x2, y2 = [int(v) for v in white_box]
        x1 = max(0, min(width - 1, x1))
        x2 = max(0, min(width, x2))
        y1 = max(0, min(height - 1, y1))
        y2 = max(0, min(height, y2))
        if x2 > x1 and y2 > y1:
            arr[y1:y2, x1:x2] = [255, 255, 255]
    img = Image.fromarray(arr)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(file_path, format="PNG")


def _create_synthetic_batch(command, width: int = 1080, height: int = 1920):
    """Materialize all sequence outputs requested by one FFmpeg invocation."""
    if "-frames:v" not in command:
        return []
    pattern = Path(command[-1])
    count = int(command[command.index("-frames:v") + 1])
    paths = []
    for index in range(count):
        path = Path(str(pattern).replace("%06d", "{:06d}".format(index)))
        _create_synthetic_frame(path, width=width, height=height)
        paths.append(path)
    return paths


class TestExpectedCoverTimeline(unittest.TestCase):
    """Unit tests verifying build_expected_cover_timeline contract independent of ASS."""

    def test_expected_timeline_includes_genuine_subtitle_in_upper_screen(self):
        """1. Genuine subtitle in upper half (y_pct = 0.18, is_subtitle=True) must NOT be excluded."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Upper genuine subtitle",
                "y_pct": 0.18,
                "is_subtitle": True,
                "tracking_blocks": [
                    {"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.16, "max_y_pct": 0.22, "is_subtitle": True}
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].segment_id, 1)
        self.assertAlmostEqual(timeline[0].y_pct, 0.16, places=2)
        self.assertEqual(timeline[0].src_start, 1.0)
        self.assertEqual(timeline[0].src_end, 3.0)

    def test_expected_timeline_excludes_packaging_in_lower_screen(self):
        """2. Packaging text in lower half (y_pct = 0.85, is_subtitle=False) must be excluded and NOT fall back."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Lower screen product ingredient label",
                "y_pct": 0.85,
                "is_subtitle": False,
                "tracking_blocks": [
                    {"start": 1.0, "end": 3.0, "x_pct": 0.1, "max_x_pct": 0.4, "y_pct": 0.82, "max_y_pct": 0.88, "is_subtitle": False}
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 0, "Non-subtitle packaging text must be completely excluded")

    def test_expected_timeline_preserves_full_ocr_bounds_extending_beyond_asr(self):
        """3. OCR starting earlier (0.5s) and ending later (3.5s) than ASR (1.0-3.0s) must preserve full OCR range."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Speech with wider visual presence",
                "y_pct": 0.85,
                "tracking_blocks": [
                    {"start": 0.5, "end": 3.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].src_start, 0.5, "src_start must reflect full OCR presence start")
        self.assertEqual(timeline[0].src_end, 3.5, "src_end must reflect full OCR presence end")
        self.assertEqual(timeline[0].expected_start, 0.5)
        self.assertEqual(timeline[0].expected_end, 4.5, "expected_end must hold 1.0s after OCR disappears (3.5 + 1.0 = 4.5)")

    def test_expected_timeline_emits_no_fake_cover_when_geometry_missing(self):
        """4. Audio segment with no visual OCR geometry emits NO fake cover event (unverified)."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Voiceover without Chinese subtitles on screen",
                "tracking_blocks": [],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 0, "No cover event should be synthesized without visual evidence")

    def test_expected_timeline_splits_blocks_with_large_gap(self):
        """5. Tracking blocks inside the same segment separated by >1.0s gap split into separate visual events."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 7.0,
                "text": "Two distinct appearances in one sentence",
                "tracking_blocks": [
                    {"start": 1.0, "end": 2.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88},
                    {"start": 4.5, "end": 6.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88},
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2, "Blocks separated by 2.0s gap (>1.0s) must split into 2 visual events")
        self.assertEqual(timeline[0].src_start, 1.0)
        self.assertEqual(timeline[0].src_end, 2.5)
        self.assertEqual(timeline[0].expected_end, 3.5, "First block must hold 1.0s after 2.5s")
        self.assertEqual(timeline[1].src_start, 4.5)
        self.assertEqual(timeline[1].src_end, 6.5)

    def test_expected_timeline_merges_overlapping_and_nested_blocks(self):
        """6. Overlapping and nested OCR blocks (1-5s, 2-3s, 4.5-6s) merge into a single continuous visual event (1-6s)."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 6.0,
                "text": "Overlapping and nested words",
                "tracking_blocks": [
                    {"start": 1.0, "end": 5.0, "x_pct": 0.2, "max_x_pct": 0.5, "y_pct": 0.82, "max_y_pct": 0.88},
                    {"start": 2.0, "end": 3.0, "x_pct": 0.5, "max_x_pct": 0.7, "y_pct": 0.82, "max_y_pct": 0.88},
                    {"start": 4.5, "end": 6.0, "x_pct": 0.7, "max_x_pct": 0.9, "y_pct": 0.82, "max_y_pct": 0.88},
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1, "Continuously overlapping/nested blocks must merge into 1 visual event")
        self.assertEqual(timeline[0].src_start, 1.0, "c_start must be min(start) = 1.0")
        self.assertEqual(timeline[0].src_end, 6.0, "c_end must be max(end) = 6.0")
        self.assertEqual(timeline[0].expected_start, 1.0)
        self.assertEqual(timeline[0].expected_end, 7.0, "Holds 1.0s to 7.0s")

    def test_expected_timeline_bridges_continuous_when_gap_within_1s(self):
        """7. Adjacent subtitles with gap <= 1.0s must bridge continuously without closing the cover."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 2.5,
                "text": "Sub 1",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 2.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
            {
                "id": 2,
                "start": 3.0,
                "end": 4.5,
                "text": "Sub 2",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 3.0, "end": 4.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2)
        self.assertTrue(timeline[0].bridged_to_next)
        self.assertEqual(timeline[0].expected_end, 3.0, "Sub 1 cover must bridge continuously to Sub 2 start at 3.0s")
        self.assertFalse(timeline[1].bridged_to_next)
        self.assertEqual(timeline[1].expected_end, 5.5, "Sub 2 cover must hold 1.0s after ending (4.5 + 1.0 = 5.5)")

    def test_expected_timeline_detects_position_change_for_shifted_subtitles(self):
        """8. Adjacent bridged subtitles with differing vertical positions must mark position_changed."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 2.5,
                "text": "Lower sub",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 2.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
            {
                "id": 2,
                "start": 3.0,
                "end": 4.5,
                "text": "Upper sub",
                "y_pct": 0.20,
                "tracking_blocks": [{"start": 3.0, "end": 4.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.18, "max_y_pct": 0.24}],
            },
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2)
        self.assertTrue(timeline[0].position_changed, "Vertical shift > 0.04 must be marked position_changed")

    def test_expected_timeline_clamps_to_video_duration(self):
        """9. Cover timeline events near video end must be clamped strictly to video_duration."""
        segments = [
            {
                "id": 1,
                "start": 8.5,
                "end": 9.8,
                "text": "Ending sub",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 8.5, "end": 9.8, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].expected_end, 10.0, "expected_end must clamp to video_duration (10.0), not 10.8")

    def test_expected_timeline_simultaneous_events_order_invariant(self):
        """10. Simultaneous timestamp events must produce deterministic, order-invariant results where both hold 1.0s."""
        seg_a = {
            "id": 1,
            "start": 2.0,
            "end": 4.0,
            "text": "Simultaneous sub A",
            "y_pct": 0.82,
            "tracking_blocks": [{"start": 2.0, "end": 4.0, "x_pct": 0.1, "max_x_pct": 0.5, "y_pct": 0.82, "max_y_pct": 0.86}],
        }
        seg_b = {
            "id": 2,
            "start": 2.0,
            "end": 4.0,
            "text": "Simultaneous sub B",
            "y_pct": 0.88,
            "tracking_blocks": [{"start": 2.0, "end": 4.0, "x_pct": 0.5, "max_x_pct": 0.9, "y_pct": 0.88, "max_y_pct": 0.94}],
        }

        # Test both permutations: [A, B] and [B, A]
        timeline_ab = build_expected_cover_timeline([seg_a, seg_b], video_duration=10.0, fps=30.0)
        timeline_ba = build_expected_cover_timeline([seg_b, seg_a], video_duration=10.0, fps=30.0)

        self.assertEqual(timeline_ab, timeline_ba, "Timeline must be 100% identical regardless of input list order")
        self.assertEqual(len(timeline_ab), 2)
        for ev in timeline_ab:
            self.assertEqual(ev.src_start, 2.0)
            self.assertEqual(ev.src_end, 4.0)
            self.assertEqual(ev.expected_start, 2.0)
            self.assertEqual(ev.expected_end, 5.0, "Both simultaneous events must hold 1.0s to 5.0s")
            self.assertAlmostEqual(ev.hold_seconds, 1.0, places=2)
            self.assertFalse(ev.bridged_to_next)

    def test_expected_timeline_supports_over_30_segments(self):
        """11. Pipeline must handle long videos with >30 segments without dropping events."""
        segments = []
        for i in range(40):
            segments.append({
                "id": i + 1,
                "start": i * 2.0,
                "end": i * 2.0 + 1.2,
                "text": f"Sentence {i + 1}",
                "y_pct": 0.85,
                "tracking_blocks": [{
                    "start": i * 2.0,
                    "end": i * 2.0 + 1.2,
                    "x_pct": 0.2,
                    "max_x_pct": 0.8,
                    "y_pct": 0.82,
                    "max_y_pct": 0.88,
                }],
            })
        timeline = build_expected_cover_timeline(segments, video_duration=100.0, fps=30.0)
        self.assertEqual(len(timeline), 40)
        for i in range(len(timeline) - 1):
            self.assertLessEqual(timeline[i].expected_start, timeline[i + 1].expected_start)

    def test_timeline_sampling_budget_guarantees_budget_cap_and_coverage(self):
        """12. Real production plan_diagnostic_samples strictly caps total frames <= 30 inclusive of baseline."""
        duration = 100.0
        # Create 50 candidates (far exceeding the 30-frame budget)
        candidates = [(f"transition_{i}", round(1.0 + i * 1.8, 2)) for i in range(40)]
        candidates.extend([(f"weak_ocr_{i}", round(5.0 + i * 10.0, 2)) for i in range(5)])
        candidates.extend([(f"cover_onset_{i}", round(2.0 + i * 15.0, 2)) for i in range(5)])
        candidates.extend([
            ("cover_fail_critical", 33.3),
            ("boundary_shift_critical", 66.6),
        ])

        planned = plan_diagnostic_samples(duration=duration, extra_samples=candidates, max_samples=30)

        self.assertLessEqual(len(planned), 30, "Total samples must not exceed strict budget of 30")
        self.assertEqual(len(planned), 30, "For 50 candidates, budget of 30 should be fully utilized")

        labels = {label for label, _ in planned}
        # Guaranteed baseline frames must be included
        self.assertIn("first", labels, "Baseline 'first' must be preserved")
        self.assertIn("middle", labels, "Baseline 'middle' must be preserved")
        self.assertIn("last", labels, "Baseline 'last' must be preserved")
        self.assertIn("tail", labels, "Baseline 'tail' must be preserved")
        self.assertIn("cover_fail_critical", labels, "A cover failure must outrank routine samples")
        self.assertIn("boundary_shift_critical", labels, "A position shift must outrank routine samples")

        # Test small candidate count (less than budget)
        small_candidates = [("cover_0", 10.0), ("cover_1", 20.0)]
        planned_small = plan_diagnostic_samples(duration=duration, extra_samples=small_candidates, max_samples=30)
        self.assertEqual(len(planned_small), 6, "4 baseline + 2 small candidates = 6 frames")

    def test_run_report_only_qc_caps_ffmpeg_samples_to_budget_30(self):
        """13. Integration test: run_report_only_qc with 50 candidates strictly caps FFmpeg extraction calls <= 30."""
        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = Path(td) / "qc_report.json"
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text("[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n", encoding="utf-8-sig")

            # 50 segments -> generates 50 transition candidates in fallback branch
            segments = []
            for i in range(50):
                segments.append({
                    "id": i + 1,
                    "start": float(i * 2),
                    "end": float(i * 2 + 1.5),
                    "text": f"Seg {i + 1}",
                    "y_pct": 0.70 if i == 11 else 0.85,
                })
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments), encoding="utf-8")

            ffmpeg_frame_calls = []

            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "120.0"}, "streams": [{"codec_type": "video", "duration": "120.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    if "-frames:v" in cmd:
                        _create_synthetic_batch(cmd, width=1080, height=1920)
                        ffmpeg_frame_calls.append(tuple(cmd))
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                report = run_report_only_qc(
                    video_path=video_file,
                    report_path=report_file,
                    ass_path=ass_file,
                    segments_path=seg_file,
                    settings=QCSettings(sample_frames=True, diagnostic_max_samples=30),
                )

            self.assertEqual(len(ffmpeg_frame_calls), 1, "All diagnostic frames must be extracted by one FFmpeg decode")
            self.assertEqual(len(report.diagnostic_artifacts), 30, "Budget of 30 frame artifacts should be fully utilized")
            self.assertLessEqual(len(report.diagnostic_artifacts), 30, "Diagnostic artifacts count must not exceed 30")
            artifact_keys = {item.get("key", "") for item in report.diagnostic_artifacts}
            self.assertIn("frames/boundary_shift_11.png", artifact_keys)
            self.assertIn("frames/boundary_shift_12.png", artifact_keys)

    def test_segment_serialization_preserves_classification_metadata(self):
        """14. GeometryBlock and RuntimeSegment serialization preserves is_subtitle, is_packaging, prob, and type."""
        block = GeometryBlock(
            text="Test Block",
            start=1.0,
            end=3.0,
            x_pct=0.2,
            max_x_pct=0.8,
            y_pct=0.82,
            max_y_pct=0.88,
            prob=0.95,
            is_subtitle=True,
            is_packaging=False,
            is_static=False,
            in_subtitle_band=True,
            type="dialogue",
        )
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Translated content",
            orig_content="Original text",
            y_pct=0.85,
            max_y_pct=0.88,
            best_block=block,
            tracking_blocks=[block],
            is_subtitle=True,
            is_packaging=False,
            is_static=False,
            in_subtitle_band=True,
        )

        data = segment_to_dict(seg)
        serialized_json = json.dumps(data)
        self.assertIn('"is_subtitle": true', serialized_json)
        self.assertIn('"is_packaging": false', serialized_json)
        self.assertIn('"in_subtitle_band": true', serialized_json)

        loaded = segment_from_dict(json.loads(serialized_json))
        self.assertTrue(loaded.is_subtitle)
        self.assertFalse(loaded.is_packaging)
        self.assertTrue(loaded.in_subtitle_band)
        self.assertEqual(len(loaded.tracking_blocks), 1)
        self.assertTrue(loaded.tracking_blocks[0].is_subtitle)
        self.assertFalse(loaded.tracking_blocks[0].is_packaging)
        self.assertEqual(loaded.tracking_blocks[0].prob, 0.95)

        timeline = build_expected_cover_timeline([loaded], video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].src_start, 1.0)
        self.assertEqual(timeline[0].src_end, 3.0)

    def test_labeled_ocr_blocks_preserve_metadata_through_merge_and_timeline(self):
        """15. Labeled OCR blocks preserve metadata across merge, JSON, and timeline ingestion."""
        ocr_seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Original text",
            y_pct=0.82,
            max_y_pct=0.88,
            is_subtitle=True,
            is_packaging=False,
            is_static=False,
            in_subtitle_band=True,
            best_block=OCRBlock(
                text="Sub",
                start=1.0,
                end=3.0,
                x_pct=0.2,
                max_x_pct=0.8,
                y_pct=0.82,
                max_y_pct=0.88,
                prob=0.95,
                is_subtitle=True,
                is_packaging=False,
                is_static=False,
                in_subtitle_band=True,
                type="subtitle",
            ),
            tracking_blocks=[
                OCRBlock(
                    text="Sub",
                    start=1.0,
                    end=3.0,
                    x_pct=0.2,
                    max_x_pct=0.8,
                    y_pct=0.82,
                    max_y_pct=0.88,
                    prob=0.95,
                    is_subtitle=True,
                    is_packaging=False,
                    is_static=False,
                    in_subtitle_band=True,
                    type="subtitle",
                )
            ],
        )

        translated_seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Translated Vietnamese",
        )

        # 1. Merge preserves metadata
        merged = merge_ocr_geometry([translated_seg], [ocr_seg])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0].is_subtitle)
        self.assertFalse(merged[0].is_packaging)
        self.assertTrue(merged[0].in_subtitle_band)

        # 2. Serialization preserves metadata
        dicts = segments_to_dicts(merged)
        serialized_json = json.dumps(dicts)
        self.assertIn('"is_subtitle": true', serialized_json)
        self.assertIn('"in_subtitle_band": true', serialized_json)

        # 3. Deserialized segments correctly ingested into cover timeline
        loaded = segments_from_dicts(json.loads(serialized_json))
        timeline = build_expected_cover_timeline(loaded, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].src_start, 1.0)
        self.assertEqual(timeline[0].src_end, 3.0)
        self.assertEqual(timeline[0].expected_end, 4.0)


class TestRealQCFailureDetections(unittest.TestCase):
    """Real failure detections evaluated against run_report_only_qc output."""

    def _setup_qc_run(self, td: str, ass_content: str, segments_data: list, duration: float = 10.0):
        video_file = Path(td) / "video.mp4"
        video_file.write_bytes(b"dummy")
        report_file = Path(td) / "qc_report.json"
        ass_file = Path(td) / "subtitles.ass"
        ass_file.write_text(ass_content, encoding="utf-8")
        seg_file = Path(td) / "segments.json"
        seg_file.write_text(json.dumps(segments_data), encoding="utf-8")

        def fake_run_command(cmd, timeout):
            cmd_str = " ".join(str(c) for c in cmd)
            if "ffprobe" in cmd_str:
                mock_res = mock.Mock(returncode=0)
                mock_res.stdout = json.dumps({
                    "format": {"duration": str(duration), "size": "1000000", "bit_rate": "1000000"},
                    "streams": [{
                        "codec_type": "video",
                        "width": 1080,
                        "height": 1920,
                        "r_frame_rate": "30/1",
                        "duration": str(duration),
                    }]
                })
                mock_res.stderr = ""
                return mock_res
            elif "ffmpeg" in cmd_str:
                _create_synthetic_batch(cmd, width=1080, height=1920)
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        return video_file, report_file, ass_file, seg_file, fake_run_command

    def test_qc_fails_when_cover_missing_in_ass(self):
        """Scenario 16: Chinese text has NO cover in ASS -> QC must report error."""
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,TextStyle,,0,0,0,,Phu de khong co cover\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Text without cover",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error")

    def test_qc_fails_when_cover_onset_is_late(self):
        """Scenario 17: Cover starts at 1.5s while Chinese text starts at 1.0s -> QC must report error."""
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.50,0:00:03.00,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Late cover text",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error")

    def test_qc_fails_when_cover_exit_is_early(self):
        """Scenario 18: Cover exits at 2.5s while Chinese text continues until 3.0s -> QC must report error."""
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:02.50,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Early exit cover text",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error")

    def test_qc_fails_when_cover_only_holds_0_2s_or_0_5s_instead_of_1_0s(self):
        """Scenario 19:
        Cover ends at 3.5s (only holding 0.5s after text ends at 3.0s, instead of required 1.0s to 4.0s).
        """
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:03.50,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Hold 0.5s instead of 1.0s",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error", "Insufficient hold (0.5s instead of 1.0s) must fail QC")

    def test_qc_fails_when_unbridged_gap_50_to_200ms_between_subs(self):
        """Scenario 20:
        Two adjacent subtitles (1.0-2.0s and 2.1-4.0s) have a 100ms gap.
        Cover 1 closes at 2.0s and Cover 2 opens at 2.1s (unbridged flicker gap).
        """
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:02.00,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
                + r"Dialogue: 0,0:00:02.10,0:00:04.00,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [
                {
                    "id": 1,
                    "start": 1.0,
                    "end": 2.0,
                    "text": "Sub 1",
                    "y_pct": 0.85,
                    "tracking_blocks": [{"start": 1.0, "end": 2.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
                },
                {
                    "id": 2,
                    "start": 2.1,
                    "end": 4.0,
                    "text": "Sub 2",
                    "y_pct": 0.85,
                    "tracking_blocks": [{"start": 2.1, "end": 4.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
                },
            ]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error", "Unbridged gap (100ms) between adjacent subtitles must fail QC")


if __name__ == "__main__":
    unittest.main()
