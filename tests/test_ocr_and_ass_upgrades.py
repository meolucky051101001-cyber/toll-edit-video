# -*- coding: utf-8 -*-
import codecs
import math
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from backend.ass_utils import generate_ass_file
from backend.ocr_subtitle_locator import select_chinese_subtitle_band
from backend.pipeline_v2.segments import RuntimeSegment


class OCRAndASSUpgradeTests(unittest.TestCase):
    def test_packaging_text_rejection_with_genuine_subtitles(self):
        """Static packaging text repeating across segments must be rejected in favor of real subtitles."""
        blocks = [
            # Static packaging at top center y=0.30 repeating identical text across 3 segments
            {"text": "特级生抽纯天然", "y_pct": 0.30, "max_y_pct": 0.35, "x_pct": 0.35, "max_x_pct": 0.65, "prob": 0.99, "sample_segment_id": 0, "sample_time": 1.0},
            {"text": "特级生抽纯天然", "y_pct": 0.30, "max_y_pct": 0.35, "x_pct": 0.35, "max_x_pct": 0.65, "prob": 0.99, "sample_segment_id": 1, "sample_time": 3.0},
            {"text": "特级生抽纯天然", "y_pct": 0.30, "max_y_pct": 0.35, "x_pct": 0.35, "max_x_pct": 0.65, "prob": 0.99, "sample_segment_id": 2, "sample_time": 5.0},
            # Genuine subtitles changing across segments at bottom y=0.82 matching transcript
            {"text": "大家好欢迎光临", "y_pct": 0.82, "max_y_pct": 0.86, "x_pct": 0.25, "max_x_pct": 0.75, "prob": 0.95, "sample_segment_id": 0, "sample_time": 1.0},
            {"text": "今天分享做菜技巧", "y_pct": 0.82, "max_y_pct": 0.86, "x_pct": 0.20, "max_x_pct": 0.80, "prob": 0.95, "sample_segment_id": 1, "sample_time": 3.0},
            {"text": "记得点赞收藏哦", "y_pct": 0.82, "max_y_pct": 0.86, "x_pct": 0.25, "max_x_pct": 0.75, "prob": 0.95, "sample_segment_id": 2, "sample_time": 5.0},
        ]
        segment_texts = {
            0: "大家好欢迎光临",
            1: "今天分享做菜技巧",
            2: "记得点赞收藏哦",
        }
        band = select_chinese_subtitle_band(blocks, segment_texts, 720, 1280)
        self.assertEqual(band.mode, "asr_match")
        self.assertGreaterEqual(band.support, 2)
        self.assertGreater(band.top, 0.75)
        self.assertLess(band.bottom, 0.92)
        for seg_id in [0, 1, 2]:
            self.assertIn(seg_id, band.selected_by_segment)
            self.assertNotIn("特级生抽", band.selected_by_segment[seg_id]["text"])

    def test_packaging_text_rejection_without_genuine_subtitles(self):
        """When video only has static packaging text and no subtitles, do NOT select packaging."""
        blocks = [
            {"text": "品牌特产老字号", "y_pct": 0.40, "max_y_pct": 0.45, "x_pct": 0.30, "max_x_pct": 0.70, "prob": 0.98, "sample_segment_id": 0, "sample_time": 1.0},
            {"text": "品牌特产老字号", "y_pct": 0.40, "max_y_pct": 0.45, "x_pct": 0.30, "max_x_pct": 0.70, "prob": 0.98, "sample_segment_id": 1, "sample_time": 3.0},
            {"text": "品牌特产老字号", "y_pct": 0.40, "max_y_pct": 0.45, "x_pct": 0.30, "max_x_pct": 0.70, "prob": 0.98, "sample_segment_id": 2, "sample_time": 5.0},
        ]
        segment_texts = {
            0: "我们现在开始准备",
            1: "把锅烧热倒油",
            2: "翻炒均匀即可",
        }
        band = select_chinese_subtitle_band(blocks, segment_texts, 720, 1280)
        self.assertEqual(band.mode, "default")
        self.assertEqual(band.support, 0)
        self.assertEqual(len(band.selected_by_segment), 0)

    def test_two_line_subtitles_detection_and_ass_cover(self):
        """Two-line Chinese subtitles must be combined and covered with full height."""
        blocks = [
            {"text": "这是第一行字幕", "y_pct": 0.78, "max_y_pct": 0.81, "x_pct": 0.25, "max_x_pct": 0.75, "prob": 0.95, "sample_segment_id": 0, "sample_time": 1.0},
            {"text": "这是第二行字幕", "y_pct": 0.82, "max_y_pct": 0.85, "x_pct": 0.25, "max_x_pct": 0.75, "prob": 0.95, "sample_segment_id": 0, "sample_time": 1.0},
        ]
        segment_texts = {0: "这是第一行字幕 这是第二行字幕"}
        band = select_chinese_subtitle_band(blocks, segment_texts, 720, 1280)
        self.assertIn(0, band.selected_by_segment)
        selected = band.selected_by_segment[0]
        self.assertLessEqual(selected["y_pct"], 0.80)
        self.assertGreaterEqual(selected["max_y_pct"], 0.84)

        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3),
            content="Dong mot cua ban dich\\nDong hai cua ban dich rat dai",
        )
        seg.best_block = SimpleNamespace(
            x_pct=selected["x_pct"],
            max_x_pct=selected["max_x_pct"],
            y_pct=selected["y_pct"],
            max_y_pct=selected["max_y_pct"],
        )
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "test_two_line.ass"
            generate_ass_file([seg], [], str(out_file), play_res_x=720, play_res_y=1280)
            content = out_file.read_text(encoding="utf-8")

        self.assertIn("BgStyle", content)
        match = re.search(r"m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)", content)
        self.assertIsNotNone(match)
        draw_h = int(match.group(2))
        self.assertGreaterEqual(draw_h + 24, 70)

    def test_moving_subtitles_tracking(self):
        """Subtitles that move across time within a segment must generate multiple tracking events."""
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3),
            content="Phu de dang di chuyen",
        )
        b1 = SimpleNamespace(start=1.0, end=2.0, x_pct=0.15, max_x_pct=0.45, y_pct=0.80, max_y_pct=0.85)
        b2 = SimpleNamespace(start=2.0, end=3.0, x_pct=0.55, max_x_pct=0.85, y_pct=0.80, max_y_pct=0.85)
        seg.tracking_blocks = [b1, b2]

        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "test_moving.ass"
            generate_ass_file([seg], [], str(out_file), play_res_x=720, play_res_y=1280)
            content = out_file.read_text(encoding="utf-8")

        bg_lines = [line for line in content.splitlines() if line.startswith("Dialogue:") and "BgStyle" in line]
        self.assertEqual(len(bg_lines), 2)
        self.assertIn("0:00:01.00,0:00:02.00", bg_lines[0])
        self.assertIn("0:00:02.00,0:00:04.00", bg_lines[1])

        m1 = re.search(r"\\pos\((\d+),(\d+)\)", bg_lines[0])
        m2 = re.search(r"\\pos\((\d+),(\d+)\)", bg_lines[1])
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        pos_x1 = int(m1.group(1))
        pos_x2 = int(m2.group(1))
        self.assertEqual(pos_x2, pos_x1)  # Centered card covers either source position.

    def test_missing_ocr_keeps_rounded_vietnamese_card(self):
        """Missing source geometry must not hide the Vietnamese text background."""
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3),
            content="Khong co phu de goc trong video nay",
        )
        seg.best_block = None

        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "test_no_sub.ass"
            generate_ass_file([seg], [], str(out_file), play_res_x=720, play_res_y=1280)
            content = out_file.read_text(encoding="utf-8")

        self.assertIn("TextStyle", content)
        self.assertIn("Khong co phu de goc", content)
        dialogue_lines = [l for l in content.splitlines() if l.startswith("Dialogue:")]
        backgrounds = [dl for dl in dialogue_lines if ",BgStyle," in dl]
        self.assertEqual(len(backgrounds), 1)
        self.assertIn("\\p1", backgrounds[0])
        self.assertIn(" b ", backgrounds[0])
        self.assertEqual(sum(",TextStyle," in dl for dl in dialogue_lines), 1)

    def test_low_confidence_no_random_fallback(self):
        """Isolated low-confidence Chinese text with no ASR match must not be picked as fallback."""
        blocks = [
            {"text": "北京烤鸭", "y_pct": 0.25, "max_y_pct": 0.30, "x_pct": 0.40, "max_x_pct": 0.60, "prob": 0.50, "sample_segment_id": 0, "sample_time": 1.5},
        ]
        segment_texts = {
            0: "今天的天气真的很不错",
            1: "大家准备出门走走吗",
        }
        band = select_chinese_subtitle_band(blocks, segment_texts, 720, 1280)
        self.assertEqual(band.mode, "default")
        self.assertEqual(band.support, 0)
        self.assertEqual(len(band.selected_by_segment), 0)

    def test_cover_box_symmetrical_expansion_for_longer_vietnamese(self):
        """Cover box must expand symmetrically around Chinese subtitle center if translation is longer."""
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=3),
            content="Ban dich tieng Viet nay dai hon chu Trung goc rat nhieu",
        )
        seg.best_block = SimpleNamespace(
            x_pct=0.40,
            max_x_pct=0.60,
            y_pct=0.80,
            max_y_pct=0.85,
        )

        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "test_expand.ass"
            generate_ass_file([seg], [], str(out_file), play_res_x=720, play_res_y=1280)
            content = out_file.read_text(encoding="utf-8")

        match_bg = re.search(r"\\pos\((\d+),(\d+)\).*?m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)", content)
        self.assertIsNotNone(match_bg)
        box_x = int(match_bg.group(1))
        box_w = int(match_bg.group(3))
        box_center = box_x + box_w // 2
        self.assertAlmostEqual(box_center, 360, delta=2)
        self.assertGreater(box_w + 24, 144 + 16)

    def test_landscape_and_portrait_video_resolutions(self):
        """Landscape videos must use 1280x720 canvas, portrait videos must use 720x1280."""
        seg = RuntimeSegment(
            index=1,
            start=timedelta(seconds=1),
            end=timedelta(seconds=2),
            content="Test do phan giai",
        )
        seg.best_block = SimpleNamespace(x_pct=0.3, max_x_pct=0.7, y_pct=0.8, max_y_pct=0.85)

        with tempfile.TemporaryDirectory() as td:
            # Landscape 1920x1080
            land_file = Path(td) / "landscape.ass"
            generate_ass_file([seg], [], str(land_file), play_res_x=1920, play_res_y=1080)
            land_content = land_file.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1280", land_content)
            self.assertIn("PlayResY: 720", land_content)

            # Portrait 1080x1920
            port_file = Path(td) / "portrait.ass"
            generate_ass_file([seg], [], str(port_file), play_res_x=1080, play_res_y=1920)
            port_content = port_file.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 720", port_content)
            self.assertIn("PlayResY: 1280", port_content)


if __name__ == "__main__":
    unittest.main()
