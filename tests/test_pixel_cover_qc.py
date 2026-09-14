"""Unit tests verifying pixel-level cover QC on rendered frames."""

from pathlib import Path
import tempfile
import unittest
from PIL import Image
import numpy as np

from backend.pipeline_v2.cover_qc import inspect_frame_pixel_coverage, parse_ass_covers


def _write_mock_batch_frames(command, payload=b"dummy frame"):
    """Materialize the image sequence requested by batched FFmpeg extraction."""
    if "-frames:v" not in command:
        return []
    pattern = Path(command[-1])
    pattern.parent.mkdir(parents=True, exist_ok=True)
    count = int(command[command.index("-frames:v") + 1])
    paths = []
    for index in range(count):
        path = Path(str(pattern).replace("%06d", "{:06d}".format(index)))
        path.write_bytes(payload)
        paths.append(path)
    return paths


class TestPixelCoverQC(unittest.TestCase):
    def test_inspect_frame_pixel_coverage_detects_white_fill(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_001.png"

            # Create an image 1080x1920 with a white rectangle at [100, 1500, 980, 1650]
            arr = np.zeros((1920, 1080, 3), dtype=np.uint8)
            arr[1500:1650, 100:980] = [255, 255, 255]
            img = Image.fromarray(arr)
            img.save(frame_file)

            covers = [
                # (start, end, x1, y1, x2, y2)
                (0.0, 5.0, 100, 1500, 980, 1650)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertTrue(result["all_boxes_filled"])
            self.assertGreaterEqual(result["details"][0]["white_ratio"], 0.95)

    def test_inspect_frame_pixel_coverage_detects_missing_fill(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_black.png"

            # Black image with NO cover
            arr = np.zeros((1920, 1080, 3), dtype=np.uint8)
            img = Image.fromarray(arr)
            img.save(frame_file)

            covers = [
                (0.0, 5.0, 100, 1500, 980, 1650)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertFalse(result["all_boxes_filled"])
            self.assertEqual(result["details"][0]["white_ratio"], 0.0)

    def test_inspect_frame_pixel_coverage_detects_chinese_text_bleed_over_cover(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_bleed.png"

            # Create an image where the cover area has heavy dark Chinese text strokes
            # taking up more than 70% of the box (white_ratio < 0.35)
            arr = np.zeros((1920, 1080, 3), dtype=np.uint8)
            # Only 20% of pixels in the box are white, the rest are dark text strokes
            arr[1500:1530, 100:980] = [255, 255, 255]
            img = Image.fromarray(arr)
            img.save(frame_file)

            covers = [
                (0.0, 5.0, 100, 1500, 980, 1650)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertFalse(result["all_boxes_filled"])
            self.assertLess(result["details"][0]["white_ratio"], 0.35)

    def test_inspect_frame_pixel_coverage_accepts_valid_sticker_with_5_to_15_percent_text(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_valid_sticker.png"

            # 1080x1920 image with background
            arr = np.zeros((1920, 1080, 3), dtype=np.uint8)
            # Fill sticker box [100, 1500, 980, 1650] with white/light sticker background
            arr[1500:1650, 100:980] = [240, 240, 240]
            # Draw dark subtitle text characters occupying ~9% of the sticker (between 5% and 15%)
            # Sticker area = 150 * 880 = 132,000 px. Text = 15 * 780 = 11,700 px (8.86%)
            arr[1560:1575, 150:930] = [20, 20, 20]
            img = Image.fromarray(arr)
            img.save(frame_file)

            covers = [
                (0.0, 5.0, 100, 1500, 980, 1650)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertTrue(result["all_boxes_filled"])
            detail = result["details"][0]
            self.assertGreaterEqual(detail["foreground_ratio"], 0.05)
            self.assertLessEqual(detail["foreground_ratio"], 0.15)
            self.assertGreaterEqual(detail["white_ratio"], 0.85)

    def test_inspect_frame_pixel_coverage_rejects_insufficient_white_cover_at_50_percent(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_50_pct.png"

            # Image where only 50% of the sticker is white (would wrongly pass under 35% threshold)
            arr = np.zeros((1920, 1080, 3), dtype=np.uint8)
            arr[1500:1575, 100:980] = [245, 245, 245] # 75px / 150px = 50%
            img = Image.fromarray(arr)
            img.save(frame_file)

            covers = [
                (0.0, 5.0, 100, 1500, 980, 1650)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertFalse(result["all_boxes_filled"]) # Must FAIL because white_ratio < 0.65

    def test_inspect_frame_pixel_coverage_detects_source_text_overflow_outside_sticker(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_overflow.png"
            arr = np.full((240, 400, 3), 120, dtype=np.uint8)
            # The rendered sticker covers x=100..300, while OCR says the source
            # subtitle occupies x=70..330. High-contrast source glyph strokes
            # remain visible in the uncovered strips on both sides.
            arr[120:180, 100:300] = [240, 240, 240]
            for x in (74, 82, 90, 310, 318, 326):
                arr[128:172, x:x + 3] = [10, 10, 10]
                arr[134:138, max(70, x - 4):min(330, x + 7)] = [245, 245, 245]
            Image.fromarray(arr).save(frame_file)

            result = inspect_frame_pixel_coverage(
                frame_file,
                [(0.0, 5.0, 100, 120, 300, 180)],
                canvas_w=400,
                canvas_h=240,
                timestamp=2.0,
                expected_regions=[{
                    "segment_id": 1,
                    "x_pct": 70 / 400,
                    "max_x_pct": 330 / 400,
                    "y_pct": 120 / 240,
                    "max_y_pct": 180 / 240,
                    "text": "中文字幕",
                }],
            )

            self.assertTrue(result["checked"])
            self.assertFalse(result["all_boxes_filled"])
            self.assertTrue(result["overflow_detected"])
            self.assertEqual(result["reason"], "source_text_outside_cover")
            self.assertGreater(
                result["expected_region_checks"][0]["uncovered_ratio"], 0.20
            )

    def test_inspect_frame_pixel_coverage_accepts_ocr_region_fully_inside_sticker(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_contained.png"
            arr = np.zeros((240, 400, 3), dtype=np.uint8)
            arr[100:190, 80:320] = [240, 240, 240]
            Image.fromarray(arr).save(frame_file)

            result = inspect_frame_pixel_coverage(
                frame_file,
                [(0.0, 5.0, 80, 100, 320, 190)],
                canvas_w=400,
                canvas_h=240,
                timestamp=2.0,
                expected_regions=[{
                    "segment_id": 1,
                    "x_pct": 100 / 400,
                    "max_x_pct": 300 / 400,
                    "y_pct": 120 / 240,
                    "max_y_pct": 170 / 240,
                    "text": "中文字幕",
                }],
            )

            self.assertTrue(result["checked"])
            self.assertTrue(result["all_boxes_filled"])
            self.assertFalse(result["overflow_detected"])
            self.assertTrue(result["expected_region_checks"][0]["geometry_covered"])

    def test_rendered_chinese_overflow_becomes_blocking_pixel_qc_error(self):
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import (
            QCSettings,
            evaluate_qc_gate,
            run_report_only_qc,
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video_file = root / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = root / "qc_report.json"
            ass_file = root / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 400\nPlayResY: 240\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:04.00,BgStyle,,0,0,0,,{\an7\pos(100,120)}{\p1}m 0 0 l 200 0 l 200 60 l 0 60{\p0}"
                + "\n",
                encoding="utf-8-sig",
            )
            segments_file = root / "segments.json"
            segments_file.write_text(
                json.dumps([{
                    "id": 1,
                    "index": 1,
                    "start": 1.0,
                    "end": 3.0,
                    "text": "Bản dịch",
                    "is_subtitle": True,
                    "tracking_blocks": [{
                        "start": 1.0,
                        "end": 3.0,
                        "x_pct": 70 / 400,
                        "max_x_pct": 330 / 400,
                        "y_pct": 120 / 240,
                        "max_y_pct": 180 / 240,
                        "text": "中文字幕",
                        "is_subtitle": True,
                    }],
                }], ensure_ascii=False),
                encoding="utf-8",
            )

            def fake_run_command(command, timeout=30.0):
                command_text = " ".join(str(item) for item in command)
                if "ffprobe" in command_text:
                    return mock.Mock(
                        returncode=0,
                        stdout=json.dumps({
                            "format": {"duration": "5.0"},
                            "streams": [
                                {
                                    "codec_type": "video",
                                    "duration": "5.0",
                                    "avg_frame_rate": "25/1",
                                },
                                {"codec_type": "audio", "duration": "5.0"},
                            ],
                        }),
                        stderr="",
                    )
                if "ffmpeg" in command_text and "-frames:v" in command:
                    pattern = Path(command[-1])
                    count = int(command[command.index("-frames:v") + 1])
                    for index in range(count):
                        frame = np.full((240, 400, 3), 120, dtype=np.uint8)
                        frame[120:180, 100:300] = [240, 240, 240]
                        for x in (74, 82, 90, 310, 318, 326):
                            frame[128:172, x:x + 3] = [10, 10, 10]
                            frame[134:138, max(70, x - 4):min(330, x + 7)] = [245, 245, 245]
                        output = Path(
                            str(pattern).replace("%06d", "{:06d}".format(index))
                        )
                        output.parent.mkdir(parents=True, exist_ok=True)
                        Image.fromarray(frame).save(output)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch(
                "backend.pipeline_v2.qc._run_command", side_effect=fake_run_command
            ):
                report = run_report_only_qc(
                    video_path=video_file,
                    report_path=report_file,
                    ass_path=ass_file,
                    segments_path=segments_file,
                    settings=QCSettings(sample_frames=True, gate_policy="block"),
                )

            pixel_check = next(
                check for check in report.checks if check.name == "pixel_cover_qc"
            )
            self.assertEqual(pixel_check.status, "error")
            self.assertFalse(report.metrics["pixel_cover_qc"]["all_boxes_filled"])
            self.assertGreater(report.metrics["pixel_cover_qc"]["overflow_frames"], 0)
            self.assertGreater(
                report.metrics["pixel_cover_qc"]["uncovered_source_frames"], 0
            )
            self.assertTrue(any(
                result.get("overflow_detected")
                for result in report.metrics["pixel_cover_qc"]["results"]
            ))
            decision = evaluate_qc_gate(report, "block")
            self.assertFalse(decision.allowed)
            self.assertIn("pixel_cover_qc", decision.blocking_checks)

    def test_pixel_cover_qc_failure_blocks_delivery_when_policy_is_block(self):
        from backend.pipeline_v2.qc import evaluate_qc_gate

        # Report with pixel_cover_qc error
        fake_report = {
            "checks": [
                {"name": "audio_duration", "status": "pass"},
                {"name": "pixel_cover_qc", "status": "error", "message": "Incomplete cover fill / exposed Chinese text"},
            ]
        }

        # Under "block" policy, gate decision MUST be allowed=False
        decision = evaluate_qc_gate(fake_report, "block")
        self.assertFalse(decision.allowed)
        self.assertIn("pixel_cover_qc", decision.blocking_checks)

        # Under "warn" policy, gate decision allows delivery
        warn_decision = evaluate_qc_gate(fake_report, "warn")
        self.assertTrue(warn_decision.allowed)

    def test_inspect_frame_pixel_coverage_degenerate_box_fails(self):
        with tempfile.TemporaryDirectory() as td:
            frame_file = Path(td) / "frame_zero.png"
            arr = np.ones((1920, 1080, 3), dtype=np.uint8) * 255
            img = Image.fromarray(arr)
            img.save(frame_file)

            # Active cover at timestamp 2.0, but with degenerate zero-area box [500, 500, 500, 500]
            covers = [
                (0.0, 5.0, 500, 500, 500, 500)
            ]

            result = inspect_frame_pixel_coverage(
                frame_file, covers, canvas_w=1080, canvas_h=1920, timestamp=2.0
            )
            self.assertTrue(result["checked"])
            self.assertFalse(result["all_boxes_filled"])
            self.assertEqual(result["boxes_checked"], 0)
            self.assertEqual(result["reason"], "active_covers_unverifiable_or_degenerate")

    def test_full_timeline_segment_sampling_in_qc(self):
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "test_video.mp4"
            video_file.write_bytes(b"video content")
            report_file = Path(td) / "qc_report.json"
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\pos(540,1550)}Hello\n",
                encoding="utf-8-sig",
            )

            # Create 12 segments spanning 30 seconds
            segments_data = []
            for i in range(12):
                start_t = float(i * 2 + 1)
                end_t = start_t + 1.5
                segments_data.append({
                    "id": i + 1,
                    "start": start_t,
                    "end": end_t,
                    "text": f"Segment {i + 1}",
                    "y_pct": 0.85,
                })
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments_data), encoding="utf-8")

            # Mock ffprobe duration to 30.0s and ffmpeg frame dumps
            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "30.0"}, "streams": [{"codec_type": "video", "duration": "30.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    _write_mock_batch_frames(cmd)
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = ""
                    mock_res.stderr = ""
                    return mock_res
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage") as mock_pix:
                    mock_pix.return_value = {
                        "checked": True,
                        "all_boxes_filled": True,
                        "boxes_checked": 1,
                        "details": [{"white_ratio": 0.9}],
                    }
                    report = run_report_only_qc(
                        video_path=video_file,
                        report_path=report_file,
                        ass_path=ass_file,
                        segments_path=seg_file,
                        settings=QCSettings(sample_frames=True),
                    )

            # Check that samples were generated across all 12 segments (not capped at 4)
            keys = [a.get("key", "") for a in report.diagnostic_artifacts]
            transition_keys = [k for k in keys if "transition_" in k]
            # Should have sampled all 12 segments: transition_0 through transition_11
            self.assertEqual(len(transition_keys), 12)
            self.assertIn("frames/transition_0.png", keys)
            self.assertIn("frames/transition_11.png", keys)
            # Pixel cover qc should be pass
            self.assertIn("pixel_cover_qc", report.metrics)
            self.assertTrue(report.metrics["pixel_cover_qc"]["all_boxes_filled"])
            self.assertEqual(report.metrics["pixel_cover_qc"]["checked_frames"], len(keys))

    def test_full_timeline_sampling_preserves_priority_shifts_tail_and_budget(self):
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        test_cases = [(26, 13), (40, 20), (49, 24), (100, 50),
                      (100, 11), (100, 35), (100, 59), (100, 83)]
        for count, shift_idx in test_cases:
            with self.subTest(segment_count=count, shift_index=shift_idx):
                with tempfile.TemporaryDirectory() as td:
                    video_file = Path(td) / "video.mp4"
                    video_file.write_bytes(b"dummy")
                    report_file = Path(td) / "qc_report.json"
                    ass_file = Path(td) / "subs.ass"
                    ass_file.write_text(
                        "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                        "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\pos(540,1550)}Hello\n",
                        encoding="utf-8-sig",
                    )

                    duration = float(count * 2 + 10)
                    segments_data = []
                    for i in range(count):
                        start_t = float(i * 2 + 1)
                        end_t = start_t + 1.5
                        # Inject a y position shift at shift_idx
                        y_pos = 0.70 if i == shift_idx else 0.85
                        segments_data.append({
                            "id": i + 1,
                            "start": start_t,
                            "end": end_t,
                            "text": f"Seg {i}",
                            "y_pct": y_pos,
                        })
                    seg_file = Path(td) / "segments.json"
                    seg_file.write_text(json.dumps(segments_data), encoding="utf-8")

                    def fake_run_command(cmd, timeout=30.0):
                        cmd_str = " ".join(str(c) for c in cmd)
                        if "ffprobe" in cmd_str:
                            mock_res = mock.Mock(returncode=0)
                            mock_res.stdout = json.dumps({"format": {"duration": str(duration)}, "streams": [{"codec_type": "video", "duration": str(duration)}]})
                            mock_res.stderr = ""
                            return mock_res
                        elif "ffmpeg" in cmd_str:
                            _write_mock_batch_frames(cmd, payload=b"dummy")
                            mock_res = mock.Mock(returncode=0)
                            mock_res.stdout = ""
                            mock_res.stderr = ""
                            return mock_res
                        return mock.Mock(returncode=0, stdout="", stderr="")

                    with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                        with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage") as mock_pix:
                            mock_pix.return_value = {
                                "checked": True,
                                "all_boxes_filled": True,
                                "boxes_checked": 1,
                                "details": [{"white_ratio": 0.9}],
                            }
                            report = run_report_only_qc(
                                video_path=video_file,
                                report_path=report_file,
                                ass_path=ass_file,
                                segments_path=seg_file,
                                settings=QCSettings(sample_frames=True),
                            )

                    keys = [a.get("key", "") for a in report.diagnostic_artifacts]
                    transition_keys = [k for k in keys if "transition_" in k]
                    shift_keys = [k for k in keys if "boundary_shift_" in k]
                    timeline_keys = transition_keys + shift_keys

                    # 1. Total transition samples must NEVER exceed 30
                    self.assertLessEqual(len(timeline_keys), 26)

                    # 2. For 26 segments (<= 30), all 26 must be present
                    if count == 26:
                        self.assertEqual(len(timeline_keys), 26)
                    else:
                        # For 40, 49, 100 segments, transition samples fill remaining budget after 4 baseline frames (30 - 4 = 26)
                        self.assertEqual(len(timeline_keys), 26)

                    # Total diagnostic frames (including baseline) must strictly be <= 30
                    self.assertLessEqual(len(keys), 30)

                    # 3. Head (transition_0) must ALWAYS be sampled
                    self.assertIn("frames/transition_0.png", keys)

                    # 4. Tail (transition_{count - 1}) must ALWAYS be sampled (NEVER dropped)
                    self.assertIn(f"frames/transition_{count - 1}.png", keys)

                    # 5. Position shift must be preserved
                    self.assertIn(f"frames/boundary_shift_{shift_idx}.png", keys)
                    self.assertIn(f"frames/boundary_shift_{shift_idx + 1}.png", keys)

    def test_boundary_transition_sampling_detects_onset_exit_and_shift(self):
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = Path(td) / "qc_report.json"
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                "Dialogue: 0,0:00:01.00,0:00:04.00,BgStyle,,0,0,0,,{\\an7\\pos(540,1550)}{\\p1}m 0 0 l 100 0 l 100 50 l 0 50{\\p0}\n"
                "Dialogue: 0,0:00:05.00,0:00:08.00,BgStyle,,0,0,0,,{\\an7\\pos(540,1200)}{\\p1}m 0 0 l 100 0 l 100 50 l 0 50{\\p0}\n",
                encoding="utf-8-sig",
            )
            segments = [
                {"id": 1, "start": 1.0, "end": 4.0, "text": "First seg", "y_pct": 0.85},
                {"id": 2, "start": 5.0, "end": 8.0, "text": "Second seg (shifted)", "y_pct": 0.65},
            ]
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments), encoding="utf-8")

            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "10.0"}, "streams": [{"codec_type": "video", "duration": "10.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    _write_mock_batch_frames(cmd, payload=b"dummy")
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage") as mock_pix:
                    mock_pix.return_value = {
                        "checked": True,
                        "all_boxes_filled": True,
                        "boxes_checked": 1,
                        "details": [{"white_ratio": 0.9}],
                    }
                    report = run_report_only_qc(
                        video_path=video_file,
                        report_path=report_file,
                        ass_path=ass_file,
                        segments_path=seg_file,
                        settings=QCSettings(sample_frames=True),
                    )

            keys = [a.get("key", "") for a in report.diagnostic_artifacts]
            # Must sample from ASS covers: onset, mid, exit, shift pre
            self.assertTrue(any("cover_onset_0" in k or "transition_0" in k for k in keys))
            self.assertTrue(any("cover_mid_0" in k or "boundary_mid_0" in k for k in keys))
            self.assertTrue(any("cover_exit_0" in k or "boundary_exit_0" in k for k in keys))
            self.assertTrue(any("cover_shift_1_pre" in k or "boundary_shift_1_pre" in k for k in keys))

    def test_missing_cover_causes_qc_error(self):
        """If an expected cover frame has no cover in ASS, QC must fail closed (status=error)."""
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = Path(td) / "qc_report.json"
            # ASS with NO covers
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                "Dialogue: 1,0:00:01.00,0:00:04.00,TextStyle,,0,0,0,,Phu de khong co cover\n",
                encoding="utf-8-sig",
            )
            segments = [
                {"id": 1, "start": 1.0, "end": 4.0, "text": "Phu de", "y_pct": 0.85},
            ]
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments), encoding="utf-8")

            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "10.0"}, "streams": [{"codec_type": "video", "duration": "10.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    _write_mock_batch_frames(cmd, payload=b"dummy")
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage") as mock_pix:
                    mock_pix.return_value = {"checked": False, "reason": "no_active_covers_at_timestamp"}
                    report = run_report_only_qc(
                        video_path=video_file,
                        report_path=report_file,
                        ass_path=ass_file,
                        segments_path=seg_file,
                        settings=QCSettings(sample_frames=True),
                    )

            pix_check = next((c for c in report.checks if getattr(c, "name", None) == "pixel_cover_qc"), None)
            self.assertIsNotNone(pix_check)
            # Must fail closed with status error!
            self.assertEqual(pix_check.status, "error")
            self.assertFalse(report.metrics.get("pixel_cover_qc", {}).get("all_boxes_filled"))

    def test_late_cover_appearance_causes_qc_error(self):
        """Cover appears late (t=2.0s instead of t=1.0s), Chinese text exposed at 1.0s-2.0s must fail QC."""
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = Path(td) / "qc_report.json"
            # ASS cover starts at 2.0s, but source Chinese text is active from 1.0s to 4.0s
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:02.00,0:00:04.00,BgStyle,,0,0,0,,{\an7\pos(540,1550)}{\p1}m 0 0 l 100 0 l 100 50 l 0 50{\p0}" + "\n",
                encoding="utf-8-sig",
            )
            segments = [
                {
                    "id": 1,
                    "index": 1,
                    "start": 1.0,
                    "end": 4.0,
                    "text": "Late cover text",
                    "y_pct": 0.85,
                    "tracking_blocks": [
                        {"start": 1.0, "end": 4.0, "x_pct": 0.3, "max_x_pct": 0.7, "y_pct": 0.85, "max_y_pct": 0.90}
                    ],
                },
            ]
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments), encoding="utf-8")

            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "10.0"}, "streams": [{"codec_type": "video", "duration": "10.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    _write_mock_batch_frames(cmd, payload=b"dummy")
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            def fake_inspect_pixel(
                fpath,
                ass_covers,
                canvas_w=1080,
                canvas_h=1920,
                timestamp=None,
                expected_regions=None,
            ):
                t = float(timestamp or 0.0)
                # Cover only exists at [2.0, 4.0]! At t < 2.0s, cover is missing!
                if t < 2.0:
                    return {"checked": False, "reason": "no_active_covers_at_timestamp"}
                return {"checked": True, "all_boxes_filled": True, "boxes_checked": 1, "details": []}

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage", side_effect=fake_inspect_pixel):
                    report = run_report_only_qc(
                        video_path=video_file,
                        report_path=report_file,
                        ass_path=ass_file,
                        segments_path=seg_file,
                        settings=QCSettings(sample_frames=True),
                    )

            # Both source_cover and pixel_cover_qc must report error because cover was late!
            source_check = next((c for c in report.checks if getattr(c, "name", None) == "source_cover"), None)
            pix_check = next((c for c in report.checks if getattr(c, "name", None) == "pixel_cover_qc"), None)
            self.assertIsNotNone(source_check)
            self.assertEqual(source_check.status, "error")
            self.assertIsNotNone(pix_check)
            self.assertEqual(pix_check.status, "error")

    def test_hold_period_grace_rule_sampling(self):
        """When source Chinese text ends early but cover holds, hold sample must be tested."""
        import json
        from unittest import mock
        from backend.pipeline_v2.qc import run_report_only_qc, QCSettings

        with tempfile.TemporaryDirectory() as td:
            video_file = Path(td) / "video.mp4"
            video_file.write_bytes(b"dummy")
            report_file = Path(td) / "qc_report.json"
            # ASS cover extends to 4.0s
            ass_file = Path(td) / "subs.ass"
            ass_file.write_text(
                "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n\n[Events]\n"
                + r"Dialogue: 0,0:00:01.00,0:00:04.00,BgStyle,,0,0,0,,{\an7\pos(540,1550)}{\p1}m 0 0 l 100 0 l 100 50 l 0 50{\p0}" + "\n",
                encoding="utf-8-sig",
            )
            # Source Chinese text ended at 2.0s! Cover holds until 4.0s (hold grace rule)
            segments = [
                {
                    "id": 1,
                    "start": 1.0,
                    "end": 4.0,
                    "text": "Hold grace text",
                    "y_pct": 0.85,
                    "tracking_blocks": [
                        {"start": 1.0, "end": 2.0, "x_pct": 0.3, "max_x_pct": 0.7, "y_pct": 0.85, "max_y_pct": 0.90}
                    ],
                }
            ]
            seg_file = Path(td) / "segments.json"
            seg_file.write_text(json.dumps(segments), encoding="utf-8")

            def fake_run_command(cmd, timeout=30.0):
                cmd_str = " ".join(str(c) for c in cmd)
                if "ffprobe" in cmd_str:
                    mock_res = mock.Mock(returncode=0)
                    mock_res.stdout = json.dumps({"format": {"duration": "10.0"}, "streams": [{"codec_type": "video", "duration": "10.0"}]})
                    mock_res.stderr = ""
                    return mock_res
                elif "ffmpeg" in cmd_str:
                    _write_mock_batch_frames(cmd, payload=b"dummy")
                    return mock.Mock(returncode=0, stdout="", stderr="")
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch("backend.pipeline_v2.qc._run_command", side_effect=fake_run_command):
                with mock.patch("backend.pipeline_v2.cover_qc.inspect_frame_pixel_coverage") as mock_pix:
                    mock_pix.return_value = {"checked": True, "all_boxes_filled": True, "boxes_checked": 1, "details": []}
                    report = run_report_only_qc(
                        video_path=video_file,
                        report_path=report_file,
                        ass_path=ass_file,
                        segments_path=seg_file,
                        settings=QCSettings(sample_frames=True),
                    )

            keys = [a.get("key", "") for a in report.diagnostic_artifacts]
            # Must contain cover_hold_0
            self.assertTrue(any("cover_hold_0" in k for k in keys))


if __name__ == "__main__":
    unittest.main()


