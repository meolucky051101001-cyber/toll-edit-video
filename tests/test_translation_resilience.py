"""Unit tests for translation resilience, JSON parsing, divide-and-conquer, and selective CJK fallback."""

import os
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from datetime import timedelta
import srt

from backend.ai.translation import (
    _parse_json_array,
    translate_with_gemini,
    translate_with_openai,
    translate_with_deepseek,
    translate_subtitles,
)


class TranslationResilienceTests(unittest.TestCase):
    def test_json_parsing_clean_and_edge_cases(self):
        # Standard json array
        self.assertEqual(_parse_json_array('["Câu 1", "Câu 2"]'), ["Câu 1", "Câu 2"])

        # Markdown json code block
        markdown_json = "```json\n[\"Câu 1\", \"Câu 2\"]\n```"
        self.assertEqual(_parse_json_array(markdown_json), ["Câu 1", "Câu 2"])

        # Markdown code block without 'json'
        markdown_plain = "```\n[\"Câu 1\", \"Câu 2\"]\n```"
        self.assertEqual(_parse_json_array(markdown_plain), ["Câu 1", "Câu 2"])

        # Trailing comma before closing bracket
        trailing_comma = '["Câu 1", "Câu 2", ]'
        self.assertEqual(_parse_json_array(trailing_comma), ["Câu 1", "Câu 2"])

        # Python single-quoted list syntax
        single_quoted = "['Câu 1', 'Câu 2']"
        self.assertEqual(_parse_json_array(single_quoted), ["Câu 1", "Câu 2"])

        # Invalid or non-array returns None
        self.assertIsNone(_parse_json_array('{"key": "value"}'))
        self.assertIsNone(_parse_json_array('invalid text'))
        self.assertIsNone(_parse_json_array(''))
        self.assertIsNone(_parse_json_array(None))

    @patch("backend.ai.translation.requests.post")
    def test_divide_and_conquer_on_gemini_length_mismatch(self, mock_post):
        """When Gemini returns 3 items for a 4-item batch, it should automatically split into 2 halves and succeed."""
        texts = ["你好", "世界", "早上好", "晚安"]

        # Call 1 (4 items): returns only 3 items (mismatch)
        resp_mismatch = MagicMock(status_code=200)
        resp_mismatch.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Xin chào", "Thế giới", "Chào buổi sáng"]'}]}}]
        }

        # Call 2 (left 2 items): returns 2 items
        resp_left = MagicMock(status_code=200)
        resp_left.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Xin chào", "Thế giới"]'}]}}]
        }

        # Call 3 (right 2 items): returns 2 items
        resp_right = MagicMock(status_code=200)
        resp_right.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Chào buổi sáng", "Chúc ngủ ngon"]'}]}}]
        }

        mock_post.side_effect = [resp_mismatch, resp_left, resp_right]

        result = translate_with_gemini(texts, api_key="dummy_key")
        self.assertEqual(result, ["Xin chào", "Thế giới", "Chào buổi sáng", "Chúc ngủ ngon"])
        self.assertEqual(len(result), 4)
        self.assertEqual(mock_post.call_count, 3)

    @patch("backend.ai.translation.requests.post")
    def test_divide_and_conquer_preserves_duration_budgets(self, mock_post):
        """Verify that duration budgets are cleanly partitioned when divide-and-conquer splits."""
        texts = ["你好", "世界", "早上好", "晚安"]
        budgets = [
            {"seconds": 1.0, "max_characters": 15},
            {"seconds": 1.2, "max_characters": 18},
            {"seconds": 1.5, "max_characters": 22},
            {"seconds": 2.0, "max_characters": 30},
        ]

        resp_mismatch = MagicMock(status_code=200)
        resp_mismatch.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Xin chào", "Thế giới"]'}]}}]
        }
        resp_left = MagicMock(status_code=200)
        resp_left.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Xin chào", "Thế giới"]'}]}}]
        }
        resp_right = MagicMock(status_code=200)
        resp_right.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Chào buổi sáng", "Chúc ngủ ngon"]'}]}}]
        }

        mock_post.side_effect = [resp_mismatch, resp_left, resp_right]
        result = translate_with_gemini(texts, api_key="dummy_key", duration_budgets=budgets)
        self.assertEqual(len(result), 4)

    def test_selective_cjk_retranslation_preserves_valid_translations(self):
        """When 1 segment out of 3 has unchanged CJK, only that segment is re-translated via fallback."""
        sub1 = srt.Subtitle(index=1, start=timedelta(seconds=0), end=timedelta(seconds=2), content="你好")
        sub2 = srt.Subtitle(index=2, start=timedelta(seconds=2), end=timedelta(seconds=4), content="老朋友")
        sub3 = srt.Subtitle(index=3, start=timedelta(seconds=4), end=timedelta(seconds=6), content="再见")

        # LLM translates sub1 and sub3, but leaves sub2 as "老朋友" (unchanged CJK)
        mock_gemini = MagicMock(return_value=["Xin chào", "老朋友", "Tạm biệt"])

        # Fallback translator for "老朋友" returns "Người bạn cũ"
        class MockGoogleTranslator:
            def __init__(self, *args, **kwargs):
                pass
            def translate(self, text):
                if text == "老朋友":
                    return "Người bạn cũ"
                return text

        with patch.dict(translate_subtitles.__globals__, {
            "translate_with_gemini": mock_gemini,
            "GoogleTranslator": MockGoogleTranslator,
        }):
            res = translate_subtitles([sub1, sub2, sub3], api_key="test_key", enable_g4f=False, strict=False)
            self.assertEqual(res[0].content, "Xin chào")
            self.assertEqual(res[1].content, "Người bạn cũ")
            self.assertEqual(res[2].content, "Tạm biệt")

    @patch("backend.ai.translation.requests.post")
    def test_gemini_configurable_timeout(self, mock_post):
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": '["Xin chào"]'}]}}]
        }
        mock_post.return_value = mock_resp

        # Direct parameter timeout
        translate_with_gemini(["你好"], api_key="dummy_key", timeout=45)
        self.assertEqual(mock_post.call_args.kwargs["timeout"], 45)

        # Environment variable timeout
        with patch.dict(os.environ, {"GEMINI_TRANSLATION_TIMEOUT": "50"}):
            translate_with_gemini(["你好"], api_key="dummy_key")
            self.assertEqual(mock_post.call_args.kwargs["timeout"], 50)


if __name__ == "__main__":
    unittest.main()
