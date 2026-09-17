"""Phase 4: visible geometry and scene-text rejection on production paths."""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from backend.ass_utils import generate_ass_file
from backend.ocr_subtitle_locator import select_chinese_subtitle_band
from backend.pipeline_v2.cover_qc import inspect_covers, parse_ass_covers
from backend.pipeline_v2.segments import GeometryBlock, RuntimeSegment


def detection(sid=1, text="今天我们学习做饭", left=0.25, right=0.75, **flags):
    return dict(text=text, start=sid - 1, end=sid, sample_segment_id=sid,
                sample_time=sid - 0.5, x_pct=left, max_x_pct=right,
                y_pct=0.75, max_y_pct=0.79, prob=0.95, **flags)


class PhaseFourCoverTests(unittest.TestCase):
    def render(self, geometry, size=(1080, 1920), tracks=False, **flags):
        segment = RuntimeSegment(
            index=1, start=timedelta(), end=timedelta(seconds=2),
            content="Phụ đề tiếng Việt", best_block=geometry,
            tracking_blocks=[geometry] if tracks else [], **flags,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover.ass"
            generate_ass_file([segment], [], path, *size)
            result = path.read_text(encoding="utf-8-sig")
        return result, segment

    def test_visible_bounds_and_exact_center_across_aspect_ratios(self):
        for size in [(1080, 1920), (1920, 1080), (1080, 1080),
                     (1440, 1080), (1001, 720), (3840, 2160)]:
            for left, right in [(0, 1), (0.05, 0.95), (0.01, 0.4), (0.6, 0.99)]:
                with self.subTest(size=size, left=left, right=right):
                    geometry = GeometryBlock(start=0, end=2, x_pct=left,
                        max_x_pct=right, y_pct=0.75, max_y_pct=0.79)
                    ass, _ = self.render(geometry, size)
                    covers, width, _ = parse_ass_covers(ass)
                    self.assertTrue(covers)
                    for _, _, x1, _, x2, _ in covers:
                        self.assertLessEqual(x2 - x1, int(width * 0.90))
                        self.assertGreaterEqual(x1, width * 0.05)
                        self.assertGreaterEqual(width - x2, width * 0.05)
                        self.assertAlmostEqual((x1 + x2) / 2, width / 2)
                    background = [line for line in ass.splitlines()
                                  if line.startswith("Dialogue: 0,")]
                    self.assertTrue(all(line.count(" b ") == 4 for line in background))

    def test_classified_scene_text_never_draws_cover_or_mutates_input(self):
        for flags in [dict(is_packaging=True), dict(is_static=True),
                      dict(is_subtitle=False), dict(in_subtitle_band=False),
                      dict(type="watermark"), dict(type="logo")]:
            for tracks in (False, True):
                with self.subTest(flags=flags, tracks=tracks):
                    geometry = GeometryBlock(start=0, end=2, x_pct=0.25,
                        max_x_pct=0.75, y_pct=0.75, max_y_pct=0.79, **flags)
                    ass, original = self.render(geometry, tracks=tracks)
                    self.assertEqual(parse_ass_covers(ass)[0], [])
                    self.assertIn("Dialogue: 1,", ass)
                    self.assertIs(original.best_block, geometry)
                    self.assertEqual(len(original.tracking_blocks), int(tracks))

    def test_segment_classification_overrides_unmarked_geometry(self):
        geometry = GeometryBlock(start=0, end=2, x_pct=0.25,
            max_x_pct=0.75, y_pct=0.75, max_y_pct=0.79)
        ass, _ = self.render(geometry, tracks=True, is_packaging=True)
        self.assertEqual(parse_ass_covers(ass)[0], [])

    def test_ocr_rejects_oversize_and_explicit_scene_labels_even_with_asr_match(self):
        for flags in [dict(left=0.01, right=0.99), dict(is_packaging=True),
                      dict(is_static=True), dict(is_subtitle=False),
                      dict(in_subtitle_band=False), dict(type="watermark")]:
            with self.subTest(flags=flags):
                result = select_chinese_subtitle_band(
                    [detection(**flags)], {1: "今天我们学习做饭"}, 1080, 1920)
                self.assertEqual(result.support, 0)
                self.assertEqual(result.selected_by_sample, {})

    def test_exact_90_percent_and_stationary_real_dialogue_are_kept(self):
        speech = {1: "今天我们学习做饭", 2: "先把所有材料准备好", 3: "然后放入锅里翻炒"}
        result = select_chinese_subtitle_band(
            [detection(sid, text, 0.05, 0.95) for sid, text in speech.items()],
            speech, 1080, 1920)
        self.assertEqual(set(result.selected_by_segment), set(speech))

    def test_repeated_unmatched_label_cannot_use_bracket_recovery(self):
        speech = {1: "今天我们学习做饭", 2: "先把所有材料准备好",
                  3: "然后放入锅里翻炒", 4: "这样我们的晚餐就做好了"}
        rows = [detection(1, speech[1]), detection(2, "优质产品说明"),
                detection(3, "优质产品说明"), detection(4, speech[4])]
        result = select_chinese_subtitle_band(rows, speech, 1080, 1920)
        self.assertEqual(set(result.selected_by_segment), {1, 4})

    def test_clamped_source_that_cannot_fit_is_rejected_by_cover_qc(self):
        geometry = GeometryBlock(text="中文字幕", start=0, end=2, x_pct=0,
            max_x_pct=1, y_pct=0.75, max_y_pct=0.79)
        ass, segment = self.render(geometry)
        report = inspect_covers([dict(id=1, best_block=vars(geometry))],
                                ass, video_duration=2)
        self.assertTrue(report["failures"])


if __name__ == "__main__":
    unittest.main()
