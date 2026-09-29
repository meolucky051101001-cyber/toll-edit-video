import unittest
from unittest.mock import patch, MagicMock
import json
import srt
from datetime import timedelta

from backend.ai.translation import (
    build_translation_prompt,
    translate_with_gemini,
    translate_with_openai,
    translate_with_deepseek,
    translate_subtitles,
)

class TestMultiLLMTranslation(unittest.TestCase):
    def test_prompt_builder(self):
        texts = ["你好", "世界"]
        prompt = build_translation_prompt(texts, target_lang="vi", with_vision=True)
        self.assertIn("Tiếng Việt", prompt)
        self.assertIn("你好", prompt)
        self.assertIn("TRỰC QUAN", prompt)

    @patch("backend.ai.translation.requests.post")
    def test_openai_translation_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps(["Xin chào", "Thế giới"])}}]
        }
        mock_post.return_value = mock_response

        texts = ["你好", "世界"]
        result = translate_with_openai(texts, target_lang="vi", api_key="sk-test-key", model="gpt-4o")
        self.assertEqual(result, ["Xin chào", "Thế giới"])

    @patch("backend.ai.translation.requests.post")
    def test_deepseek_translation_success(self, mock_post):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": json.dumps(["Xin chào", "Thế giới"])}}]
        }
        mock_post.return_value = mock_response

        texts = ["你好", "世界"]
        result = translate_with_deepseek(texts, target_lang="vi", api_key="sk-test-key", model="deepseek-v4")
        self.assertEqual(result, ["Xin chào", "Thế giới"])

    @patch("backend.ai.translation.translate_with_deepseek")
    def test_translate_subtitles_with_deepseek(self, mock_deepseek):
        mock_deepseek.return_value = ["Xin chào"]
        sub = srt.Subtitle(index=1, start=timedelta(seconds=0), end=timedelta(seconds=2), content="你好")
        with patch.dict("os.environ", {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-test"}):
            res = translate_subtitles([sub], target_lang="vi")
            self.assertEqual(res[0].content, "Xin chào")

    def test_prompt_builder_with_glossary_entity_speaker_and_cross_batch_context(self):
        texts = ["小帅来到了北京", "老张正在吃火锅"]
        glossary = {"火锅": "lẩu"}
        entity_map = {"小帅": "Tiểu Soái", "北京": "Bắc Kinh"}
        speaker_map = {"SPEAKER_00": "Người kể chuyện"}
        prior_context = [{"source": "很久以前", "translated": "Ngày xửa ngày xưa"}]

        prompt = build_translation_prompt(
            texts,
            target_lang="vi",
            prior_context=prior_context,
            glossary=glossary,
            entity_map=entity_map,
            speaker_map=speaker_map,
        )
        self.assertIn("GLOSSARY", prompt)
        self.assertIn("lẩu", prompt)
        self.assertIn("ENTITY MAP", prompt)
        self.assertIn("Tiểu Soái", prompt)
        self.assertIn("SPEAKER MAP", prompt)
        self.assertIn("Người kể chuyện", prompt)
        self.assertIn("Ngày xửa ngày xưa", prompt)
        self.assertIn("giữ nguyên cách viết tên riêng", prompt)
        self.assertIn("không dịch lại theo nghĩa", prompt)

    @patch("backend.ai.translation.translate_with_deepseek")
    def test_translate_subtitles_forwards_glossary_and_maps(self, mock_deepseek):
        mock_deepseek.return_value = ["Tiểu Soái đi ăn lẩu"]
        sub = srt.Subtitle(index=1, start=timedelta(seconds=0), end=timedelta(seconds=2), content="小帅吃火锅")
        glossary = {"火锅": "lẩu"}
        entity_map = {"小帅": "Tiểu Soái"}
        speaker_map = {"SPEAKER_00": "Tiểu Soái"}
        with patch.dict("os.environ", {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "sk-test"}):
            res = translate_subtitles(
                [sub],
                target_lang="vi",
                glossary=glossary,
                entity_map=entity_map,
                speaker_map=speaker_map,
            )
            self.assertEqual(res[0].content, "Tiểu Soái đi ăn lẩu")
            self.assertEqual(mock_deepseek.call_count, 1)
            _, kwargs = mock_deepseek.call_args
            self.assertEqual(kwargs.get("glossary"), glossary)
            self.assertEqual(kwargs.get("entity_map"), entity_map)
            self.assertEqual(kwargs.get("speaker_map"), speaker_map)

    def test_gemini_health_cache_and_retry_limits(self):
        from backend.ai.translation import is_gemini_available, mark_gemini_unhealthy

        # Should be available initially
        mark_gemini_unhealthy(cooldown_seconds=-1.0)
        self.assertTrue(is_gemini_available())

        # When cooldown marked, is_gemini_available becomes False
        mark_gemini_unhealthy(cooldown_seconds=60.0)
        self.assertFalse(is_gemini_available())

        # When unavailable, translate_with_gemini immediately returns None without calling API
        res = translate_with_gemini(["你好"], api_key="dummy_key")
        self.assertIsNone(res)

        # Reset cooldown
        mark_gemini_unhealthy(cooldown_seconds=-1.0)
        self.assertTrue(is_gemini_available())


if __name__ == "__main__":
    unittest.main()
