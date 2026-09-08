import re
import tempfile
import unittest
from pathlib import Path

from backend.ass_utils import generate_ass_file
from backend.ocr_subtitle_locator import select_chinese_subtitle_band
from backend.pipeline_v2.segments import GeometryBlock, RuntimeSegment


def block(segment_id, text, left, right, top, bottom, probability=0.9):
    return {
        "text": text,
        "start": float(segment_id),
        "end": float(segment_id) + 1.0,
        "x_pct": left,
        "max_x_pct": right,
        "y_pct": top,
        "max_y_pct": bottom,
        "prob": probability,
        "sample_segment_id": segment_id,
        "sample_time": float(segment_id) + 0.5,
    }


class ChineseSubtitleLocatorTests(unittest.TestCase):
    def test_asr_match_beats_wide_product_packaging_text(self):
        speech = {
            1: "今天给大家介绍这套贴纸",
            2: "打开以后可以看到很多图案",
            3: "这个价格真的非常划算",
            4: "喜欢的话可以收藏起来",
        }
        subtitles = [
            "介绍这套贴纸",
            "可以看到很多图案",
            "价格真的非常划算",
            "喜欢的话可以收藏",
        ]
        detections = []
        for segment_id, subtitle in enumerate(subtitles, 1):
            detections.extend(
                [
                    block(segment_id, "产品说明", 0.28, 0.72, 0.40, 0.48, 0.99),
                    block(segment_id, subtitle, 0.10, 0.90, 0.75, 0.81, 0.72),
                ]
            )

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "asr_match")
        self.assertAlmostEqual(selected.top, 0.75)
        self.assertAlmostEqual(selected.bottom, 0.81)
        self.assertEqual(selected.support, 4)
        self.assertTrue(
            all(
                item["text"] != "产品说明"
                for item in selected.selected_by_segment.values()
            )
        )

    def test_unmatched_changing_text_does_not_authorize_a_cover(self):
        speech = {index: "speech unavailable" for index in range(1, 6)}
        subtitles = ["第一句话", "接着打开", "看看里面", "价格便宜", "下次再见"]
        detections = []
        for segment_id, subtitle in enumerate(subtitles, 1):
            detections.extend(
                [
                    block(segment_id, "品牌名称", 0.30, 0.70, 0.44, 0.51),
                    block(segment_id, subtitle, 0.12, 0.88, 0.73, 0.79),
                ]
            )

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        # Changing scene labels are not sufficient evidence without ASR support.
        self.assertEqual(selected.mode, "default")
        self.assertEqual(selected.support, 0)
        self.assertEqual(selected.selected_by_sample, {})

    def test_two_matched_lines_are_combined_but_scene_label_is_not(self):
        speech = {
            1: "这是一条需要分成两行显示的字幕",
            2: "第二句话也需要完整遮住两行",
        }
        detections = [
            block(1, "这是一条需要", 0.20, 0.80, 0.69, 0.73),
            block(1, "分成两行显示的字幕", 0.12, 0.88, 0.75, 0.79),
            block(1, "包装参数", 0.34, 0.66, 0.32, 0.38),
            block(2, "第二句话也需要", 0.18, 0.82, 0.69, 0.73),
            block(2, "完整遮住两行", 0.20, 0.80, 0.75, 0.79),
            block(2, "包装参数", 0.34, 0.66, 0.32, 0.38),
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "asr_match")
        self.assertAlmostEqual(selected.top, 0.69)
        self.assertAlmostEqual(selected.bottom, 0.79)
        self.assertIn("两行", selected.selected_by_segment[1]["text"])
        self.assertIn("完整遮住", selected.selected_by_segment[2]["text"])

    def test_tall_vertical_product_text_is_not_a_subtitle_candidate(self):
        detections = [
            block(1, "产品包装说明", 0.45, 0.52, 0.25, 0.65),
        ]
        selected = select_chinese_subtitle_band(
            detections, {1: "speech unavailable"}, 1080, 1920
        )
        self.assertEqual(selected.mode, "default")
        self.assertEqual(selected.candidate_count, 0)
        self.assertAlmostEqual(selected.top, 0.75)


class SubtitleCoverGeometryTests(unittest.TestCase):
    def test_sticker_stays_compact_and_centered_on_source(self):
        segment = RuntimeSegment(
            index=1,
            start=__import__("datetime").timedelta(seconds=0),
            end=__import__("datetime").timedelta(seconds=2),
            content="Xin chào",
            y_pct=0.74,
            max_y_pct=0.80,
            best_block=GeometryBlock(
                text="这是一行很长的中文字幕",
                start=0,
                end=2,
                x_pct=0.11,
                max_x_pct=0.89,
                y_pct=0.74,
                max_y_pct=0.80,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cover.ass"
            generate_ass_file([segment], [], output)
            content = output.read_text(encoding="utf-8-sig")

        background = next(
            line for line in content.splitlines() if ",BgStyle," in line
        )
        match = re.search(
            r"\\pos\((\d+),(\d+)\).*?m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)",
            background,
        )
        self.assertIsNotNone(match)
        box_x, box_y, box_width, box_height = map(int, match.groups())
        self.assertLessEqual(box_width, int(0.99 * 720))
        self.assertLessEqual(box_height, int(0.10 * 1280))
        self.assertLessEqual(box_y - 12, int(0.74 * 1280))
        self.assertGreaterEqual(box_y + box_height + 12, int(0.80 * 1280))
        self.assertAlmostEqual(box_x + (box_width / 2), 0.50 * 720, delta=1)
        self.assertAlmostEqual(box_y + (box_height / 2), 0.77 * 1280, delta=1)
        # Include the 12px same-colour ASS outline when checking coverage.
        self.assertLessEqual(box_x - 12, int(0.11 * 720) + 2)
        self.assertGreaterEqual(box_x + box_width + 12, int(0.89 * 720) - 2)

    def test_long_two_line_sticker_keeps_compact_height(self):
        segment = RuntimeSegment(
            index=1,
            start=__import__("datetime").timedelta(seconds=0),
            end=__import__("datetime").timedelta(seconds=2),
            content="Set bốn tấm siêu nhiều chi tiết với nhiều hình dễ thương để trang trí sổ",
            y_pct=0.74,
            max_y_pct=0.80,
            best_block=GeometryBlock(
                text="四张贴纸有非常多细节",
                start=0,
                end=2,
                x_pct=0.15,
                max_x_pct=0.85,
                y_pct=0.74,
                max_y_pct=0.80,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "compact-cover.ass"
            generate_ass_file([segment], [], output)
            content = output.read_text(encoding="utf-8-sig")

        background = next(
            line for line in content.splitlines() if ",BgStyle," in line
        )
        match = re.search(
            r"\\pos\((\d+),(\d+)\).*?m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)",
            background,
        )
        self.assertIsNotNone(match)
        _, _, box_width, box_height = map(int, match.groups())
        self.assertLessEqual(box_width / 720, 0.99)
        self.assertAlmostEqual(box_height / 1280, 0.091, delta=0.005)


if __name__ == "__main__":
    unittest.main()
