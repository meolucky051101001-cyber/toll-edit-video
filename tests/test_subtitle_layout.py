import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from backend.ass_utils import generate_ass_file
from backend.ai.translation import translate_subtitles
from backend.pipeline_v2.segments import RuntimeSegment
from backend.pipeline_v2.timing import solve_segment_timing
from backend.subtitle_layout import subtitle_measure, wrap_subtitle_text
from backend.subtitle_text import normalize_subtitle_text, split_subtitle_sentences


def segment(text, start=10, end=22):
    return RuntimeSegment(index=14, start=timedelta(seconds=start),
                          end=timedelta(seconds=end), content=text, source_segment_id=9,
                          y_pct=0.75, max_y_pct=0.79)


def render_events(text):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "captions.ass"
        generate_ass_file([segment(text)], [], path)
        return [line.split(",", 9) for line in path.read_text(encoding="utf-8-sig").splitlines()
                if line.startswith("Dialogue:")]


class SubtitleTextTests(unittest.TestCase):
    def test_ellipsis_cleanup_keeps_words_and_sentence_stops(self):
        self.assertEqual(
            normalize_subtitle_text("...Đẹp nha… chốt luôn. Ôi...toàn con trai . . ."),
            "Đẹp nha chốt luôn. Ôi toàn con trai",
        )
        self.assertEqual(normalize_subtitle_text("Giá 1.5 triệu, mã v2.3.5."),
                         "Giá 1.5 triệu, mã v2.3.5.")

    def test_sentence_split_keeps_decimals_and_abbreviations(self):
        self.assertEqual(split_subtitle_sentences('Ở TP. HCM giá 1.5 triệu. “Đẹp nha.” Chốt luôn!'),
                         ['Ở TP. HCM giá 1.5 triệu.', '“Đẹp nha.”', 'Chốt luôn!'])

    def test_translation_cleanup_applies_to_llm_and_fallback(self):
        for use_llm in (True, False):
            gemini = Mock(return_value=["...Đẹp nha…"] if use_llm else None)
            google = Mock()
            google.return_value.translate.return_value = "...Đẹp nha…"
            # Other suites reload translation modules. Patch the globals of
            # the function under test, rather than a later sys.modules entry.
            with self.subTest(use_llm=use_llm), patch.dict("os.environ", {}, clear=True), \
                    patch.dict(translate_subtitles.__globals__, {
                        "translate_with_gemini": gemini, "GoogleTranslator": google,
                    }):
                result = translate_subtitles([segment("真漂亮")], api_key="test", enable_g4f=False, strict=True)
                self.assertEqual(result[0].content, "Đẹp nha")
                self.assertEqual(result[0].orig_content, "真漂亮")
                gemini.assert_called_once()
                self.assertEqual(google.called, not use_llm)

    def test_cleanup_does_not_disguise_an_untranslated_chinese_response(self):
        gemini = Mock(return_value=["你好"])
        google, memory = Mock(), Mock()
        google.return_value.translate.return_value = "你好"
        memory.return_value.translate.return_value = "你好"
        with patch.dict("os.environ", {}, clear=True), patch.dict(translate_subtitles.__globals__, {
            "translate_with_gemini": gemini, "GoogleTranslator": google, "MyMemoryTranslator": memory,
        }):
            with self.assertRaises(RuntimeError):
                translate_subtitles([segment("...你好...")], api_key="test", enable_g4f=False, strict=True)

    def test_fitting_speech_stays_grouped_but_sentences_display_separately(self):
        original = segment("...là kịch kim rồi. Chốt hạ bằng trai đẹp nha, chốt luôn. Ôi toàn con trai...", end=30)
        solved = solve_segment_timing([original]).segments
        self.assertEqual([item.content for item in solved],
                         ["là kịch kim rồi. Chốt hạ bằng trai đẹp nha, chốt luôn. Ôi toàn con trai"])
        self.assertEqual(solved[0].start, original.start)
        self.assertEqual(solved[-1].end, original.end)
        self.assertTrue(all(a.end == b.start for a, b in zip(solved, solved[1:])))
        self.assertEqual([item.index for item in solved], [1])
        self.assertTrue(all(item.source_segment_id == 9 and item.y_pct == 0.75 for item in solved))
        events = [event for event in render_events(solved[0].content) if event[3] == "TextStyle"]
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0][9].endswith("là kịch kim rồi."))
        self.assertTrue(events[1][9].endswith("Chốt hạ bằng trai đẹp nha, chốt luôn."))
        self.assertTrue(events[2][9].endswith("Ôi toàn con trai"))


class SubtitleLayoutTests(unittest.TestCase):
    def test_text_that_fits_does_not_wrap_by_character_count(self):
        events = [event for event in render_events("Set bốn tấm siêu nhiều chi tiết") if event[3] == "TextStyle"]
        self.assertEqual(len(events), 1)
        self.assertNotIn(r"\N", events[0][9])

    def test_wrapping_fills_first_line_using_actual_glyph_width(self):
        measure = subtitle_measure("Arial", 38, True)
        text = "Những món đồ này có nhiều chi tiết nhỏ xinh để trang trí cuốn sổ và làm quà cho bạn bè"
        lines = wrap_subtitle_text(text, 608, measure)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), text)
        self.assertTrue(all(measure(line) <= 608 for line in lines))
        for first, second in zip(lines, lines[1:]):
            self.assertGreater(measure(first + " " + second.split()[0]), 608)
        self.assertGreater(measure(lines[0]) / 720, 0.78)
        self.assertGreater(measure("WWWWWWWW"), measure("iiiiiiii"))

    def test_sentence_switch_is_a_new_cue_not_a_line_in_the_same_card(self):
        text_events = [event for event in render_events("đẹp nha, chốt luôn. Ôi toàn con trai...") if event[3] == "TextStyle"]
        self.assertEqual(len(text_events), 2)
        self.assertTrue(text_events[0][9].endswith("đẹp nha, chốt luôn."))
        self.assertTrue(text_events[1][9].endswith("Ôi toàn con trai"))
        self.assertEqual(text_events[0][2], text_events[1][1])

    def test_long_sentence_uses_two_line_pages_inside_frame(self):
        text = " ".join(["Những chiếc váy rất xinh với nhiều chi tiết tinh tế"] * 7) + "."
        events = render_events(text)
        text_events = [event for event in events if event[3] == "TextStyle"]
        self.assertGreater(len(text_events), 1)
        rendered = [re.sub(r"^\{[^}]*\}", "", event[9]) for event in text_events]
        self.assertEqual(" ".join(rendered).replace(r"\N", " "), text)
        self.assertTrue(all(item.count(r"\N") <= 1 for item in rendered))
        for event in (event for event in events if event[3] == "BgStyle"):
            match = re.search(r"\\pos\((\d+),(\d+)\).*?m (\d+) [\d.]+ l [\d.]+ [\d.]+ b [\d.]+ [\d.]+ [\d.]+ (\d+)", event[9])
            x, _, width, height = map(int, match.groups())
            self.assertGreaterEqual(x - 12, 36)
            self.assertLessEqual(x + width + 12, 684)
            self.assertLessEqual(height + 24, 128)


if __name__ == "__main__":
    unittest.main()
