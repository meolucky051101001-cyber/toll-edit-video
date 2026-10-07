import unittest
from datetime import timedelta
from backend.ai.transcription import _join_aligned_tokens
from backend.ai.translation import build_translation_prompt
from backend.subtitle_text import normalize_subtitle_text
from backend.pipeline_v2.timing import TimingPolicy, estimate_tts_duration


class SpeechPausesAndPunctuationTests(unittest.TestCase):
    def test_join_aligned_tokens_inserts_comma_between_cjk_lines(self):
        # When two separate CJK speech segments without punctuation are joined,
        # a comma is inserted to mark the speech pause boundary between phrases.
        left = "假如一颗榴莲被扔回一亿五千万年前的侏罗纪"
        right = "你砸下来的时候"
        joined = _join_aligned_tokens(left, right, join_cjk_lines_with_comma=True)
        self.assertEqual(joined, "假如一颗榴莲被扔回一亿五千万年前的侏罗纪，你砸下来的时候")

    def test_join_aligned_tokens_does_not_duplicate_existing_punctuation(self):
        left = "假如一颗榴莲被扔回一亿五千万年前的侏罗纪，"
        right = "你砸下来的时候"
        joined = _join_aligned_tokens(left, right, join_cjk_lines_with_comma=True)
        self.assertEqual(joined, "假如一颗榴莲被扔回一亿五千万年前的侏罗纪，你砸下来的时候")

        left_period = "第一句。"
        right_period = "第二句。"
        joined_period = _join_aligned_tokens(left_period, right_period, join_cjk_lines_with_comma=True)
        self.assertEqual(joined_period, "第一句。第二句。")

    def test_normalize_subtitle_text_preserves_commas_and_cleans_duplicates(self):
        text = "  Giả sử  , , một quả sầu riêng, khi rơi xuống .  "
        normalized = normalize_subtitle_text(text)
        self.assertEqual(normalized, "Giả sử, một quả sầu riêng, khi rơi xuống.")

    def test_estimate_tts_duration_accounts_for_commas(self):
        text_without_comma = "Giả sử một quả sầu riêng bị ném về kỷ Jura khi bạn rơi xuống"
        text_with_comma = "Giả sử, một quả sầu riêng bị ném về kỷ Jura, khi bạn rơi xuống."
        dur1 = estimate_tts_duration(text_without_comma)
        dur2 = estimate_tts_duration(text_with_comma)
        # Punctuation pauses should increase the estimated duration
        self.assertGreater(dur2, dur1)

    def test_translation_prompt_contains_pause_and_comma_rules(self):
        texts = ["假如一颗榴莲被扔回一亿五千万年前的侏罗纪，你砸下来的时候"]
        prompt = build_translation_prompt(texts, target_lang="vi")
        self.assertIn("DẤU NGẮT NGHỈ (DẤU PHẨY, DẤU CHẤM)", prompt)
        self.assertIn("dấu phẩy (,)", prompt)
        self.assertIn("Viết hoa chữ cái đầu câu", prompt)


if __name__ == "__main__":
    unittest.main()
