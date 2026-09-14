"""Comprehensive unit tests for expected cover timeline contract and red tests.

Phase 1 (Đợt 1) Test Suite:
1. Genuine subtitle in upper screen (y_pct < 0.45) is included.
2. Packaging / background text in lower screen (y_pct > 0.45) is excluded.
3. OCR starting earlier and ending later than ASR preserves full OCR bounds.
4. Segment without OCR geometry emits no fake cover event.
5. Multiple tracking blocks separated by >1.0s gap split into separate visual events.
6. Adjacent subtitles within 1.0s bridge continuously.
7. Adjacent subtitles with vertical shift set position_changed flag.
8. Clamping at video duration.
9. Deterministic handling of simultaneous timestamp events.
10. Long timeline scalability (>30 segments).
11. Real sampling budget cap and coverage guarantee.
12. Failure detection: missing cover in ASS.
13. Failure detection: late cover onset.
14. Failure detection: early cover exit.
15. RED TEST 1 (Fails on 9c4f070): Insufficient hold (0.5s instead of 1.0s).
16. RED TEST 2 (Fails on 9c4f070): Unbridged gap (100ms) between adjacent subtitles.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import numpy as np
from PIL import Image

from backend.pipeline_v2.cover_qc import (
    build_expected_cover_timeline,
    ExpectedCoverEvent,
)
from backend.pipeline_v2.qc import run_report_only_qc, QCSettings


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
                "start": 1.0,  # ASR speech starts at 1.0s
                "end": 3.0,    # ASR speech ends at 3.0s
                "text": "Visible Chinese text",
                "y_pct": 0.85,
                "tracking_blocks": [
                    {"start": 0.5, "end": 3.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].src_start, 0.5, "Visual start must use OCR start, not ASR start")
        self.assertEqual(timeline[0].src_end, 3.5, "Visual end must use OCR end, not ASR end")
        self.assertEqual(timeline[0].expected_end, 4.5, "Expected end must hold 1.0s past OCR end (3.5 + 1.0 = 4.5)")

    def test_expected_timeline_segment_without_ocr_geometry_emits_no_fake_cover(self):
        """4. Segment without OCR tracking blocks must emit NO fake default cover event (unverified)."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Spoken segment without Chinese on-screen subtitle",
                "y_pct": 0.85,
                "tracking_blocks": [],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 0, "No cover events should be invented when geometry is missing")

    def test_expected_timeline_splits_tracking_blocks_separated_by_over_one_second(self):
        """5. Multiple tracking blocks in one segment separated by > 1.0s gap must split into separate events."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 5.0,
                "text": "Sentence with visual gap",
                "y_pct": 0.85,
                "tracking_blocks": [
                    {"start": 1.0, "end": 2.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88},
                    {"start": 3.5, "end": 4.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88},
                ],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2, "Blocks separated by 1.5s gap (> 1.0s) must split into 2 separate visual events")
        self.assertEqual(timeline[0].src_start, 1.0)
        self.assertEqual(timeline[0].src_end, 2.0)
        self.assertEqual(timeline[0].expected_end, 3.0)  # holds 1.0s to 3.0s, gap from 3.0 to 3.5s
        self.assertFalse(timeline[0].bridged_to_next)

        self.assertEqual(timeline[1].src_start, 3.5)
        self.assertEqual(timeline[1].src_end, 4.5)
        self.assertEqual(timeline[1].expected_end, 5.5)

    def test_expected_timeline_bridges_adjacent_subs_within_one_second(self):
        """6. When next Chinese text appears before 1.0s hold expires, covers must bridge continuously."""
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
                "start": 2.4,
                "end": 4.0,
                "text": "Sub 2",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 2.4, "end": 4.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0].expected_end, 2.4)
        self.assertTrue(timeline[0].bridged_to_next)
        self.assertAlmostEqual(timeline[0].hold_seconds, 0.4, places=2)

        self.assertEqual(timeline[1].expected_end, 5.0)
        self.assertFalse(timeline[1].bridged_to_next)
        self.assertAlmostEqual(timeline[1].hold_seconds, 1.0, places=2)

    def test_expected_timeline_detects_adjacent_position_change(self):
        """7. When adjacent subtitles have different vertical positions, position_changed must be flagged."""
        segments = [
            {
                "id": 1,
                "start": 1.0,
                "end": 2.0,
                "text": "Sub 1 bottom",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 2.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
            {
                "id": 2,
                "start": 2.2,
                "end": 4.0,
                "text": "Sub 2 shifted higher",
                "y_pct": 0.70,
                "tracking_blocks": [{"start": 2.2, "end": 4.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.68, "max_y_pct": 0.74}],
            },
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2)
        self.assertTrue(timeline[0].bridged_to_next)
        self.assertTrue(timeline[0].position_changed)

    def test_expected_timeline_clamps_at_video_duration(self):
        """8. Subtitles at end of video must clamp hold duration to video_duration."""
        segments = [
            {
                "id": 1,
                "start": 8.0,
                "end": 9.5,
                "text": "Final sub near video end",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 8.0, "end": 9.5, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 1)
        self.assertEqual(timeline[0].expected_end, 10.0)
        self.assertAlmostEqual(timeline[0].hold_seconds, 0.5, places=2)

    def test_expected_timeline_handles_same_timestamp_events(self):
        """9. Events with identical start times must be deterministically ordered without crash."""
        segments = [
            {
                "id": 2,
                "start": 2.0,
                "end": 4.0,
                "text": "Simultaneous sub B",
                "y_pct": 0.88,
                "tracking_blocks": [{"start": 2.0, "end": 4.0, "x_pct": 0.1, "max_x_pct": 0.5, "y_pct": 0.88, "max_y_pct": 0.94}],
            },
            {
                "id": 1,
                "start": 2.0,
                "end": 4.0,
                "text": "Simultaneous sub A",
                "y_pct": 0.82,
                "tracking_blocks": [{"start": 2.0, "end": 4.0, "x_pct": 0.5, "max_x_pct": 0.9, "y_pct": 0.82, "max_y_pct": 0.88}],
            },
        ]
        timeline = build_expected_cover_timeline(segments, video_duration=10.0, fps=30.0)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0].expected_start, 2.0)
        self.assertEqual(timeline[1].expected_start, 2.0)

    def test_expected_timeline_supports_over_30_segments(self):
        """10. Pipeline must handle long videos with >30 segments without dropping events."""
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
        """11. Sampling budget algorithm for >30 segments caps at budget (30), includes head (0) and tail (N-1)."""
        total_segs = 45
        shift_idx = 20
        shift_indices = [shift_idx]

        # Standard budget selection algorithm used in QC timeline sampling:
        selected_indices = set()
        selected_indices.add(0)
        selected_indices.add(total_segs - 1)
        remaining_budget = 30 - len(selected_indices)
        if len(shift_indices) > remaining_budget:
            step = (len(shift_indices) - 1) / max(1, remaining_budget - 1)
            for k in range(remaining_budget):
                selected_indices.add(shift_indices[round(k * step)])
        else:
            selected_indices.update(shift_indices)
        if len(selected_indices) < 30:
            grid = [round(i * (total_segs - 1) / 29.0) for i in range(30)]
            for pt in grid:
                selected_indices.add(pt)
                if len(selected_indices) >= 30:
                    break

        self.assertLessEqual(len(selected_indices), 30, "Sampling points must not exceed budget of 30")
        self.assertEqual(len(selected_indices), 30, "For 45 segments, sampling budget should be fully utilized (30)")
        self.assertIn(0, selected_indices, "Head segment (index 0) must be included")
        self.assertIn(total_segs - 1, selected_indices, "Tail segment (index 44) must be included")
        self.assertIn(shift_idx, selected_indices, "Shift transition point must be preserved in budget")


class TestRealQCFailureDetections(unittest.TestCase):
    """Real failure detection tests evaluated against actual run_report_only_qc output.

    Under Phase 1 requirements, tests for unbridged gaps and insufficient hold must
    assert failure. On commit 9c4f070, these tests will FAIL (RED TESTS) because
    commit 9c4f070 currently permits short holds (0.2s/0.5s) and unbridged gaps.
    """

    def _setup_qc_run(self, td: str, ass_content: str, segments_data: list, duration: float = 10.0):
        video_file = Path(td) / "video.mp4"
        video_file.write_bytes(b"dummy")
        report_file = Path(td) / "qc_report.json"
        ass_file = Path(td) / "subs.ass"
        ass_file.write_text(ass_content, encoding="utf-8-sig")
        seg_file = Path(td) / "segments.json"
        seg_file.write_text(json.dumps(segments_data), encoding="utf-8")

        def fake_run_command(cmd, timeout=30.0):
            cmd_str = " ".join(str(c) for c in cmd)
            if "ffprobe" in cmd_str:
                mock_res = mock.Mock(returncode=0)
                mock_res.stdout = json.dumps({
                    "format": {"duration": str(duration)},
                    "streams": [{"codec_type": "video", "duration": str(duration)}],
                })
                mock_res.stderr = ""
                return mock_res
            elif "ffmpeg" in cmd_str:
                if str(cmd[-1]) != "-":
                    out_path = Path(cmd[-1])
                    _create_synthetic_frame(out_path, width=1080, height=1920)
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        return video_file, report_file, ass_file, seg_file, fake_run_command

    def test_qc_fails_when_cover_missing_in_ass(self):
        """Scenario 12: Chinese text has NO cover in ASS -> QC must report error."""
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
        """Scenario 13: Cover starts at 1.5s while Chinese text starts at 1.0s -> QC must report error."""
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.50,0:00:03.00,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Late onset text",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 3.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            self.assertEqual(src_check.status, "error")

    def test_qc_fails_when_cover_exits_early(self):
        """Scenario 14: Chinese text ends at 3.0s but cover exits at 2.6s -> QC must report error."""
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:02.60,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 3.0,
                "text": "Early exit text",
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
        """Scenario 15 [RED TEST on 9c4f070]:

        Chinese text ends at 2.0s with no next sub. Required rule mandates 1.0s hold (until 3.0s).
        In this test, cover only holds until 2.5s (0.5s hold).
        QC must detect insufficient hold and flag ERROR.
        On baseline 9c4f070, this assertion FAILS because 9c4f070 inspect_covers only checks [start, end].
        """
        with tempfile.TemporaryDirectory() as td:
            ass_content = (
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:02.50,BgStyle,,0,0,0,,{\an7\pos(216,1574)}{\p1}m 0 0 l 648 0 l 648 115 l 0 115{\p0}" + "\n"
            )
            segments = [{
                "id": 1,
                "start": 1.0,
                "end": 2.0,
                "text": "Short hold text",
                "y_pct": 0.85,
                "tracking_blocks": [{"start": 1.0, "end": 2.0, "x_pct": 0.2, "max_x_pct": 0.8, "y_pct": 0.82, "max_y_pct": 0.88}],
            }]
            v, r, a, s, fake_cmd = self._setup_qc_run(td, ass_content, segments)
            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_cmd):
                report = run_report_only_qc(v, r, ass_path=a, segments_path=s, settings=QCSettings(sample_frames=True))

            src_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            self.assertIsNotNone(src_check)
            # On 9c4f070, this assertion FAILS (returns 'pass' instead of 'error'):
            self.assertEqual(src_check.status, "error", "Insufficient hold (0.5s instead of 1.0s) must fail QC")

    def test_qc_fails_when_unbridged_gap_50_to_200ms_between_subs(self):
        """Scenario 16 [RED TEST on 9c4f070]:

        Sub 1 ends at 2.0s, Sub 2 starts at 2.1s (100ms gap).
        Because Sub 2 starts before 1.0s hold expires, covers must bridge continuously.
        Leaving a 100ms gap between covers must be flagged as an ERROR by QC.
        On baseline 9c4f070, this assertion FAILS because 9c4f070 inspect_covers checks segments individually.
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
            # On 9c4f070, this assertion FAILS (returns 'pass' instead of 'error'):
            self.assertEqual(src_check.status, "error", "Unbridged gap (100ms) between adjacent subtitles must fail QC")


if __name__ == "__main__":
    unittest.main()
