"""Unit tests for URL sanitization in downloader and logging."""

import unittest
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from social_downloader import sanitize_url


class TestUrlSanitization(unittest.TestCase):
    def test_sanitize_url_empty_and_no_query(self):
        self.assertEqual(sanitize_url(""), "")
        self.assertEqual(sanitize_url(None), "")
        self.assertEqual(sanitize_url("https://example.com/video.mp4"), "https://example.com/video.mp4")

    def test_sanitize_url_redacts_sensitive_keys(self):
        url = (
            "https://sns-video-qn.xhscdn.com/video.mp4?"
            "xsec_token=secret123&sign=abc456&sig=def789&pass=mypass&ticket=tkt999&"
            "token=tok111&session=sess222&api_key=key333&v=1080p&lang=vi"
        )
        sanitized = sanitize_url(url)
        self.assertNotIn("secret123", sanitized)
        self.assertNotIn("abc456", sanitized)
        self.assertNotIn("def789", sanitized)
        self.assertNotIn("mypass", sanitized)
        self.assertNotIn("tkt999", sanitized)
        self.assertNotIn("tok111", sanitized)
        self.assertNotIn("sess222", sanitized)
        self.assertNotIn("key333", sanitized)
        self.assertIn("xsec_token=%5BREDACTED%5D", sanitized)
        self.assertIn("sign=%5BREDACTED%5D", sanitized)
        self.assertIn("sig=%5BREDACTED%5D", sanitized)
        self.assertIn("pass=%5BREDACTED%5D", sanitized)
        self.assertIn("ticket=%5BREDACTED%5D", sanitized)
        self.assertIn("v=1080p", sanitized)
        self.assertIn("lang=vi", sanitized)

    def test_sanitize_url_case_insensitive(self):
        url = "https://example.com/api?SIGN=uppercase_sig&PassWord=secret_pass&safe=true"
        sanitized = sanitize_url(url)
        self.assertNotIn("uppercase_sig", sanitized)
        self.assertNotIn("secret_pass", sanitized)
        self.assertIn("safe=true", sanitized)


if __name__ == "__main__":
    unittest.main()
