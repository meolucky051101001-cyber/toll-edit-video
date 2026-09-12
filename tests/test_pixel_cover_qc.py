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


if __name__ == "__main__":
    unittest.main()
