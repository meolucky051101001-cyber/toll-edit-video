"""Unit tests verifying pixel-level cover QC on rendered frames."""

from pathlib import Path
import tempfile
import unittest
from PIL import Image
import numpy as np

from backend.pipeline_v2.cover_qc import inspect_frame_pixel_coverage, parse_ass_covers


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


if __name__ == "__main__":
    unittest.main()
