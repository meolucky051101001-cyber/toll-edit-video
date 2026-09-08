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

if __name__ == "__main__":
    unittest.main()
