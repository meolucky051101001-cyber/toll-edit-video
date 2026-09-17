import unittest

from backend.url_utils import extract_http_urls


class TelegramUrlExtractionTests(unittest.TestCase):
    def test_extracts_unique_urls_and_strips_punctuation(self):
        text = (
            "https://xhslink.com/o/abc123,\n"
            "https://example.com/watch?v=42&lang=vi!\n"
            "https://xhslink.com/o/abc123"
        )
        self.assertEqual(
            extract_http_urls(text),
            [
                "https://xhslink.com/o/abc123",
                "https://example.com/watch?v=42&lang=vi",
            ],
        )

    def test_removes_timestamp_appended_to_xhs_short_link(self):
        self.assertEqual(
            extract_http_urls("http://xhslink.com/o/2mzI6tcxPzB09:52"),
            ["http://xhslink.com/o/2mzI6tcxPzB"],
        )


if __name__ == "__main__":
    unittest.main()
