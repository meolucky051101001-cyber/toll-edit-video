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
    def test_edge_to_edge_asr_matched_caption_keeps_geometry(self):
        speech = {5: "推重音KTO的到底有多少人"}
        detections = [
            block(5, "推重音teto的到底有多少", 36 / 720, 684 / 720,
                  0.104, 0.149, 0.998),
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "asr_match")
        self.assertIn(5, selected.selected_by_segment)
        self.assertAlmostEqual(
            selected.selected_by_segment[5]["x_pct"], 36 / 720
        )
        self.assertAlmostEqual(
            selected.selected_by_segment[5]["max_x_pct"], 684 / 720
        )

    def test_edge_to_edge_unmatched_scene_text_is_not_selected(self):
        speech = {5: "推重音KTO的到底有多少人"}
        detections = [
            block(5, "产品包装参数使用说明", 0.01, 0.995,
                  0.35, 0.40, 0.999),
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "default")
        self.assertNotIn(5, selected.selected_by_segment)

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

    def test_midscreen_packaging_text_is_filtered_even_if_frequent(self):
        speech = {
            1: "今天给各位推荐一款很好用的收纳盒",
            2: "收纳盒可以放很多小卡",
        }
        detections = [
            # Mid-screen product packaging text (center_y = 0.45)
            block(1, "优质收纳", 0.30, 0.70, 0.42, 0.48, 0.95),
            block(2, "优质收纳", 0.30, 0.70, 0.42, 0.48, 0.95),
            # Bottom real subtitle (center_y = 0.78)
            block(1, "推荐一款很好用的收纳盒", 0.10, 0.90, 0.75, 0.81, 0.88),
            block(2, "可以放很多小卡", 0.15, 0.85, 0.75, 0.81, 0.88),
        ]
        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)
        self.assertEqual(selected.mode, "asr_match")
        self.assertAlmostEqual(selected.top, 0.75)
        self.assertAlmostEqual(selected.bottom, 0.81)
        self.assertTrue(all("优质收纳" not in item["text"] for item in selected.selected_by_segment.values()))

    def test_phonetic_homophone_first_cue_is_selected(self):
        # Whisper: "真的是完百想折扣", OCR: "真的是晚买享折扣"
        speech = {
            1: "真的是完百想折扣",
            2: "谁敢想这个现在才一米一个呢",
            3: "果然很漂亮",
        }
        detections = [
            block(1, "真的是晚买享折扣", 0.37, 0.63, 0.81, 0.84, 0.99),
            block(2, "谁敢想这个现在才一米一个呢", 0.20, 0.80, 0.81, 0.84, 0.99),
            block(3, "果然很漂亮", 0.30, 0.70, 0.81, 0.84, 0.99),
        ]
        selected = select_chinese_subtitle_band(detections, speech, 2160, 3840)
        self.assertEqual(selected.mode, "asr_match")
        self.assertEqual(selected.support, 3)
        self.assertIn(1, selected.selected_by_segment)
        self.assertIn("真的是晚买享折扣", selected.selected_by_segment[1]["text"])

    def test_boundary_first_cue_rescued_by_following_cue_when_track_established(self):
        speech = {
            1: "完全不一样的识别错误文本",
            2: "谁敢想这个现在才一米一个呢",
            3: "果然很漂亮",
        }
        detections = [
            # Cue 1 has un-matching text, but correct geometry inside established track
            block(1, "屏幕上的中文字幕第一句", 0.25, 0.75, 0.81, 0.84, 0.95),
            block(2, "谁敢想这个现在才一米一个呢", 0.20, 0.80, 0.81, 0.84, 0.99),
            block(3, "果然很漂亮", 0.30, 0.70, 0.81, 0.84, 0.99),
        ]
        selected = select_chinese_subtitle_band(detections, speech, 2160, 3840)
        self.assertEqual(selected.mode, "asr_match")
        self.assertIn(1, selected.selected_by_segment)

    def test_non_chinese_transcript_finds_dynamic_bottom_captions(self):
        # English ASR over burnt-in Chinese captions: no text match is possible,
        # but real speech exists so the band must be detected visually.
        speech = {
            1: "One minute makeup tutorial",
            2: "Apply foundation and concealer",
            3: "Blend everything with a sponge",
            4: "Set the base with loose powder",
            5: "Finish with a glossy lip",
        }
        subtitles = [
            "一分钟化妆教程",
            "先上粉底和遮瑕",
            "用海绵拍开底妆",
            "散粉定妆更持久",
            "最后涂个亮面唇",
        ]
        detections = []
        for segment_id, subtitle in enumerate(subtitles, 1):
            detections.extend(
                [
                    block(segment_id, "品牌名称", 0.40, 0.60, 0.44, 0.50, 0.95),
                    block(segment_id, subtitle, 0.12, 0.88, 0.73, 0.79, 0.92),
                ]
            )

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "visual_match")
        self.assertEqual(selected.support, 5)
        self.assertAlmostEqual(selected.top, 0.73)
        self.assertAlmostEqual(selected.bottom, 0.79)
        self.assertEqual(selected.selected_by_segment[1]["text"], "一分钟化妆教程")
        self.assertIn("亮面唇", selected.selected_by_segment[5]["text"])
        self.assertTrue(selected.selected_by_sample)
        self.assertTrue(
            all(
                abs(item["y_pct"] - 0.73) < 0.02
                for rows in selected.selected_by_sample.values()
                for item in rows
            )
        )
        # The mid-screen brand label must never be covered.
        self.assertTrue(
            all(
                item["text"] != "品牌名称"
                for item in selected.selected_by_segment.values()
            )
        )

    def test_non_chinese_transcript_without_dynamic_captions_stays_default(self):
        speech = {index: "One minute makeup tutorial" for index in range(1, 5)}
        detections = [
            # Static bottom banner repeated across every cue: no caption track.
            block(segment_id, "品牌名称", 0.30, 0.70, 0.73, 0.79, 0.95)
            for segment_id in range(1, 5)
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "default")
        self.assertEqual(selected.support, 0)

    def test_bilingual_captions_cover_chinese_row_only(self):
        # Chinese row on top, English translation row underneath. Even though the
        # English row is the one that matches the transcript, the Chinese row is
        # the masking priority and the English row must stay untouched.
        pairs = [
            ("一分钟化妆教程", "One minute makeup tutorial"),
            ("先上粉底和遮瑕", "Apply foundation and concealer"),
            ("用海绵拍开底妆", "Blend everything with a sponge"),
            ("散粉定妆更持久", "Set the base with loose powder"),
        ]
        speech = {index: english for index, (_, english) in enumerate(pairs, 1)}
        detections = []
        for segment_id, (chinese, english) in enumerate(pairs, 1):
            detections.extend(
                [
                    block(segment_id, chinese, 0.15, 0.85, 0.73, 0.78, 0.92),
                    block(segment_id, english, 0.12, 0.88, 0.80, 0.85, 0.90),
                ]
            )

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "visual_match")
        self.assertAlmostEqual(selected.top, 0.73)
        self.assertAlmostEqual(selected.bottom, 0.78)
        self.assertTrue(
            all(
                re.search(r"[\u4e00-\u9fff]", item["text"])
                for item in selected.selected_by_segment.values()
            )
        )
        self.assertTrue(
            all(
                item["max_y_pct"] <= 0.78
                for rows in selected.selected_by_sample.values()
                for item in rows
            )
        )

    def test_english_only_captions_are_located_from_latin_transcript(self):
        captions = [
            "One minute makeup tutorial",
            "Apply foundation and concealer",
            "Blend everything with a sponge",
            "Set the base with loose powder",
        ]
        speech = {index: text for index, text in enumerate(captions, 1)}
        detections = [
            block(index, text, 0.12, 0.88, 0.73, 0.79, 0.90)
            for index, text in enumerate(captions, 1)
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "english_match")
        self.assertEqual(selected.support, 4)
        self.assertAlmostEqual(selected.top, 0.73)
        self.assertAlmostEqual(selected.bottom, 0.79)
        self.assertEqual(
            selected.selected_by_segment[1]["text"], "One minute makeup tutorial"
        )
        self.assertIn(4, selected.selected_by_sample)

    def test_english_scene_labels_without_transcript_support_stay_default(self):
        speech = {index: "One minute makeup tutorial" for index in range(1, 4)}
        detections = [
            block(segment_id, "NEW ARRIVAL", 0.30, 0.70, 0.73, 0.79, 0.95)
            for segment_id in range(1, 4)
        ]

        selected = select_chinese_subtitle_band(detections, speech, 1080, 1920)

        self.assertEqual(selected.mode, "default")
        self.assertEqual(selected.support, 0)
        self.assertEqual(selected.selected_by_sample, {})


class SubtitleCoverGeometryTests(unittest.TestCase):
    def test_edge_to_edge_source_gets_full_white_cover_at_source_y(self):
        segment = RuntimeSegment(
            index=5,
            start=__import__("datetime").timedelta(seconds=13.32),
            end=__import__("datetime").timedelta(seconds=14.64),
            content="đang thúc đẩy để nhấn mạnh KTO",
            y_pct=0.104,
            max_y_pct=0.149,
            best_block=GeometryBlock(
                text="推重音teto的到底有多少",
                start=13.32,
                end=14.64,
                x_pct=10 / 720,
                max_x_pct=716 / 720,
                y_pct=0.104,
                max_y_pct=0.149,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "edge-cover.ass"
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
        # The white cover must contain the full source region; it may extend
        # past the normalized source bounds to include the configured padding.
        self.assertLessEqual(box_x, 10)
        self.assertGreaterEqual(box_x + box_width, 716)
        self.assertLessEqual(box_y, int(0.104 * 1280))
        self.assertGreaterEqual(box_y + box_height, int(0.149 * 1280))

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
        self.assertLessEqual(box_width, int(0.90 * 720))
        self.assertLessEqual(box_height, int(0.10 * 1280))
        self.assertLessEqual(box_y - 12, int(0.74 * 1280))
        self.assertGreaterEqual(box_y + box_height + 12, int(0.80 * 1280))
        self.assertAlmostEqual(box_x + (box_width / 2), 0.50 * 720, delta=1)
        self.assertAlmostEqual(box_y + (box_height / 2), 0.77 * 1280, delta=1)
        # BgStyle has no outline: the drawing itself must cover source text.
        self.assertLessEqual(box_x, int(0.11 * 720) + 2)
        self.assertGreaterEqual(box_x + box_width, int(0.89 * 720) - 2)

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
        self.assertLessEqual(box_width / 720, 0.90)
        self.assertGreaterEqual(box_height, 84)
        self.assertLessEqual(box_height, 108)


if __name__ == "__main__":
    unittest.main()
