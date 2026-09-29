import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.ass_utils import generate_ass_file
from backend.pipeline_v2.segments import GeometryBlock, RuntimeSegment


class TestSubtitleCoverStability(unittest.TestCase):
    def test_vertical_y_coordinate_locked_across_jittery_sentences(self):
        """Sentence 1 and Sentence 2 with slight OCR Y-jitter are locked to identical draw_y."""
        seg1 = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Câu thứ nhất rất rõ ràng",
            best_block=GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.81, max_y_pct=0.85),
            tracking_blocks=[
                GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.81, max_y_pct=0.85)
            ],
        )
        seg2 = RuntimeSegment(
            index=2,
            start=timedelta(seconds=3.0),
            end=timedelta(seconds=5.0),
            content="Câu thứ hai tiếp tục xuất hiện",
            best_block=GeometryBlock(start=3.0, end=5.0, x_pct=0.25, max_x_pct=0.75, y_pct=0.83, max_y_pct=0.87),
            tracking_blocks=[
                GeometryBlock(start=3.0, end=5.0, x_pct=0.25, max_x_pct=0.75, y_pct=0.83, max_y_pct=0.87)
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ass"
            generate_ass_file([seg1, seg2], [], path, 1080, 1920)
            ass = path.read_text(encoding="utf-8-sig")

        bg_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: 0,")]
        self.assertGreaterEqual(len(bg_lines), 2)
        y_coords = []
        for line in bg_lines:
            match = re.search(r"\\pos\([0-9.]+,\s*([0-9.]+)\)", line)
            self.assertIsNotNone(match, f"Could not find pos in {line}")
            y_coords.append(float(match.group(1)))

        # All dialogue covers in the main track must have the exact same Y position
        self.assertEqual(len(set(y_coords)), 1, f"Y positions differ across sentences: {y_coords}")

    def test_short_gap_between_adjacent_subtitles_is_bridged_continuously(self):
        """300ms gap between adjacent subtitles is bridged so the cover box does not flicker/disappear."""
        seg1 = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Câu số 1",
            best_block=GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86),
            tracking_blocks=[
                GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86)
            ],
        )
        seg2 = RuntimeSegment(
            index=2,
            start=timedelta(seconds=3.3),  # 300ms gap
            end=timedelta(seconds=5.0),
            content="Câu số 2",
            best_block=GeometryBlock(start=3.3, end=5.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86),
            tracking_blocks=[
                GeometryBlock(start=3.3, end=5.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86)
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ass"
            generate_ass_file([seg1, seg2], [], path, 1080, 1920)
            ass = path.read_text(encoding="utf-8-sig")

        bg_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: 0,")]
        self.assertGreaterEqual(len(bg_lines), 2)
        match1 = re.search(r"Dialogue:\s*0,\s*([0-9:.]+),\s*([0-9:.]+)", bg_lines[0])
        match2 = re.search(r"Dialogue:\s*0,\s*([0-9:.]+),\s*([0-9:.]+)", bg_lines[1])
        end_time_seg1 = match1.group(2)
        start_time_seg2 = match2.group(1)
        self.assertEqual(end_time_seg1, start_time_seg2,
                         f"Gap between seg1 end ({end_time_seg1}) and seg2 start ({start_time_seg2}) was not bridged")

    def test_isolated_subtitle_holds_cover_for_one_second(self):
        """Isolated subtitle ending at 3.0s with no following subtitle holds cover until 4.0s."""
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1.0),
            end=timedelta(seconds=3.0),
            content="Câu đơn lẻ",
            best_block=GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86),
            tracking_blocks=[
                GeometryBlock(start=1.0, end=3.0, x_pct=0.20, max_x_pct=0.80, y_pct=0.82, max_y_pct=0.86)
            ],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.ass"
            generate_ass_file([seg], [], path, 1080, 1920)
            ass = path.read_text(encoding="utf-8-sig")

        bg_lines = [line for line in ass.splitlines() if line.startswith("Dialogue: 0,")]
        self.assertTrue(bg_lines)
        last_bg = bg_lines[-1]
        match = re.search(r"Dialogue:\s*0,\s*([0-9:.]+),\s*([0-9:.]+)", last_bg)
        end_time = match.group(2)
        self.assertEqual(end_time, "0:00:04.00", f"Cover did not hold for 1.0s: {last_bg}")


if __name__ == "__main__":
    unittest.main()
