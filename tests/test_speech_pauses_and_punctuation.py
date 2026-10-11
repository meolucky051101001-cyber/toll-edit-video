import unittest
from datetime import timedelta
from types import SimpleNamespace
from backend.ai.transcription import _merge_short_fragments, _transcribe_raw_segments
from backend.ai.translation import ensure_speech_pauses_and_entity
from backend.subtitle_text import normalize_subtitle_text
from backend.pipeline_v2.timing import TimingPolicy, estimate_tts_duration


class SpeechPausesAndPunctuationTests(unittest.TestCase):
    def test_complete_cjk_phrases_are_not_merged_across_speech_pauses(self):
        left = "假如一颗榴莲被扔回一亿五千万年前的侏罗纪"
        right = "你砸下来的时候"
        segments = [{"start": 0, "end": 4, "text": left}, {"start": 4.3, "end": 6, "text": right}]
        self.assertEqual(_merge_short_fragments(segments), segments)

    def test_transcription_preserves_source_punctuation_without_duplication(self):
        left = "假如一颗榴莲被扔回一亿五千万年前的侏罗纪，"
        right = "你砸下来的时候"
        text = left + right
        model = SimpleNamespace(transcribe=lambda *a, **kw: (
            [SimpleNamespace(start=0, end=5, text=text, words=[])], None))
        self.assertEqual(_transcribe_raw_segments(model, "fixture.wav")[0]["text"], text)

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

    def test_translation_adds_pauses_only_when_the_source_contains_them(self):
        translated = ["khi trời mưa chúng ta sẽ ở nhà"]
        with_pause = ensure_speech_pauses_and_entity(["下雨时，我们待在家。"], translated)[0]
        without_pause = ensure_speech_pauses_and_entity(["下雨时我们待在家"], translated)[0]
        self.assertTrue(with_pause.startswith("Khi"))
        self.assertIn(",", with_pause)
        self.assertTrue(with_pause.endswith("."))
        self.assertNotIn(",", without_pause)
        self.assertFalse(without_pause.endswith("."))


if __name__ == "__main__":
    unittest.main()
