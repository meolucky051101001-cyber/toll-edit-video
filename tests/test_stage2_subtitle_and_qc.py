import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.ass_utils import generate_ass_file
from backend.pipeline_v2.cover_qc import check_watermark_collision, parse_ass_covers
from backend.pipeline_v2.segments import GeometryBlock, RuntimeSegment


class TestStage2SubtitleAndQC(unittest.TestCase):
    def test_landscape_subtitle_width_not_clamped_to_65_percent(self):
        """When watermark_box is None, landscape bottom subtitle can use 80-90% width."""
        long_text = "Đây là một câu phụ đề tương đối dài dùng để kiểm tra việc không bị ép cụt dòng ở 65 phần trăm chiều ngang của video phong cảnh."
        geom = GeometryBlock(start=0, end=3, x_pct=0.1, max_x_pct=0.9, y_pct=0.85, max_y_pct=0.90)
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=3),
            content=long_text,
            best_block=geom,
            tracking_blocks=[geom],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            ass_path = Path(tmpdir) / "test_unclamped.ass"
            # 1920x1080 landscape
            generate_ass_file([seg], [], str(ass_path), 1920, 1080, main_y_pct=0.85, watermark_box=None)
            content = ass_path.read_text(encoding="utf-8-sig")

            # Check cover boxes
            covers, canvas_w, canvas_h = parse_ass_covers(content)
            self.assertEqual(canvas_w, 1280)
            self.assertEqual(canvas_h, 720)
            self.assertTrue(len(covers) > 0)
            for c in covers:
                _, _, x1, _, x2, _ = c
                width = x2 - x1
                # Should be allowed to exceed 65% (canvas_w * 0.65 = 832px) and reach 80-90%
                # Total available width is 90% (1152px)
                self.assertLessEqual(width, canvas_w * 0.90 + 2)
                self.assertGreaterEqual(x1, canvas_w * 0.05 - 1)
                self.assertGreaterEqual(canvas_w - x2, canvas_w * 0.05 - 1)

            # Check that watermark collision is clean when watermark_box is None
            wm_res = check_watermark_collision(covers, canvas_w, canvas_h, watermark_box=None)
            self.assertFalse(wm_res["has_collision"])
            self.assertEqual(wm_res["collision_count"], 0)

    def test_dynamic_watermark_box_clamps_safely(self):
        """When watermark_box is explicitly supplied, bottom text avoids the watermark zone."""
        long_text = "Câu phụ đề thử nghiệm né tránh con dấu góc dưới bên phải khi watermark box được cung cấp đầy đủ."
        geom = GeometryBlock(start=0, end=3, x_pct=0.1, max_x_pct=0.9, y_pct=0.85, max_y_pct=0.90)
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=0),
            end=timedelta(seconds=3),
            content=long_text,
            best_block=geom,
            tracking_blocks=[geom],
        )

        # Douyin red seal watermark box at bottom-right corner: X >= 1626/1920, Y >= 980/1080
        wm_box = (1626.0, 980.0, 1920.0, 1080.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            ass_path = Path(tmpdir) / "test_clamped.ass"
            generate_ass_file([seg], [], str(ass_path), 1920, 1080, main_y_pct=0.85, watermark_box=wm_box)
            content = ass_path.read_text(encoding="utf-8-sig")

            covers, canvas_w, canvas_h = parse_ass_covers(content)
            self.assertTrue(len(covers) > 0)

            # Watermark check with the dynamic watermark box must report NO collision
            wm_res = check_watermark_collision(covers, canvas_w, canvas_h, watermark_box=wm_box)
            self.assertFalse(wm_res["has_collision"])
            self.assertEqual(wm_res["collision_count"], 0)

    def test_no_false_positive_on_wide_subtitles_without_watermark(self):
        """A subtitle spanning 85% width must not trigger false positive watermark error."""
        # A cover box extending to 85% canvas width
        covers = [(0.0, 3.0, 64.0, 600.0, 1150.0, 660.0)]
        canvas_w, canvas_h = 1280, 720

        # Without watermark_box: clean pass
        wm_res = check_watermark_collision(covers, canvas_w, canvas_h, watermark_box=None)
        self.assertFalse(wm_res["has_collision"])
        self.assertEqual(wm_res["collision_count"], 0)


if __name__ == "__main__":
    unittest.main()
