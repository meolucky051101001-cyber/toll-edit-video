import os
import sys
import tempfile
import unittest
from pathlib import Path

from datetime import timedelta
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from ass_utils import generate_ass_file


class TestStreamlinedPipeline(unittest.TestCase):
    def test_portrait_and_landscape_baseline_ass_generation(self):
        # Create mock translated segments
        seg1 = SimpleNamespace(index=1, start=timedelta(seconds=1), end=timedelta(seconds=3), content="Xin chào các bạn")
        seg2 = SimpleNamespace(index=2, start=timedelta(seconds=4), end=timedelta(seconds=6), content="Hôm nay chúng ta cùng học nấu ăn")
        segments = [seg1, seg2]

        with tempfile.TemporaryDirectory() as td:
            # 1. Test Portrait 9:16 (1080x1920) with main_y_pct=0.88
            ass_portrait = os.path.join(td, "portrait.ass")
            generate_ass_file(segments, [], ass_portrait, play_res_x=1080, play_res_y=1920, main_y_pct=0.88)
            self.assertTrue(os.path.isfile(ass_portrait))
            content_p = Path(ass_portrait).read_text(encoding="utf-8")
            self.assertIn("Xin chào các bạn", content_p)
            self.assertIn("Hôm nay chúng ta cùng học nấu ăn", content_p)
            # Check vertical pos tag exists
            self.assertIn("\\pos(", content_p)

            # 2. Test Landscape 16:9 (1920x1080) with main_y_pct=0.85
            ass_landscape = os.path.join(td, "landscape.ass")
            generate_ass_file(segments, [], ass_landscape, play_res_x=1920, play_res_y=1080, main_y_pct=0.85)
            self.assertTrue(os.path.isfile(ass_landscape))
            content_l = Path(ass_landscape).read_text(encoding="utf-8")
            self.assertIn("Xin chào các bạn", content_l)
            self.assertIn("Hôm nay chúng ta cùng học nấu ăn", content_l)
            self.assertIn("\\pos(", content_l)


if __name__ == "__main__":
    unittest.main()
