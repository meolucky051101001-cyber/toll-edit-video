"""Unit tests for URL sanitization in downloader and logging."""

import unittest
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from social_downloader import (
    SensitiveUrlFilter,
    sanitize_exception,
    sanitize_text,
    sanitize_url,
)


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

    def test_sanitize_text_redacts_urls_inside_arbitrary_strings(self):
        msg = "Failed fetching from https://api.douyin.com/v1/play?token=secret999&v=1 and redirected to https://cdn.xhs.com/clip.mp4?sign=xyz123"
        sanitized = sanitize_text(msg)
        self.assertNotIn("secret999", sanitized)
        self.assertNotIn("xyz123", sanitized)
        self.assertIn("token=%5BREDACTED%5D", sanitized)
        self.assertIn("sign=%5BREDACTED%5D", sanitized)

    def test_sanitize_exception_redacts_requests_http_error(self):
        import requests
        exc = requests.exceptions.HTTPError("403 Client Error: Forbidden for url: https://example.com/video?token=supersecret&sign=sigval")
        sanitized = sanitize_exception(exc)
        self.assertNotIn("supersecret", sanitized)
        self.assertNotIn("sigval", sanitized)
        self.assertIn("token=%5BREDACTED%5D", sanitized)

    def test_sanitize_exception_with_traceback(self):
        try:
            raise ValueError("Crash with url https://example.com/data?pass=mypassword&id=10")
        except ValueError as e:
            tb_sanitized = sanitize_exception(e, include_traceback=True)
            self.assertNotIn("mypassword", tb_sanitized)
            self.assertIn("pass=%5BREDACTED%5D", tb_sanitized)
            self.assertIn("Traceback", tb_sanitized)

    def test_sensitive_url_filter_intercepts_log_records(self):
        import logging
        test_logger = logging.getLogger("test_sanitizer_filter")
        test_logger.setLevel(logging.INFO)
        test_filter = SensitiveUrlFilter()
        test_logger.addFilter(test_filter)

        captured_records = []
        class ListHandler(logging.Handler):
            def emit(self, record):
                captured_records.append(self.format(record))

        handler = ListHandler()
        formatter = logging.Formatter("%(message)s")
        handler.setFormatter(formatter)
        test_logger.addHandler(handler)

        test_logger.info("Connecting to https://secret-domain.com/feed?token=leaked_token_12345")
        self.assertEqual(len(captured_records), 1)
        self.assertNotIn("leaked_token_12345", captured_records[0])
        self.assertIn("token=%5BREDACTED%5D", captured_records[0])

    def test_sensitive_url_filter_scrubs_tracebacks_when_exc_info_is_true(self):
        import logging
        test_logger = logging.getLogger("test_traceback_sanitization")
        test_logger.setLevel(logging.INFO)
        test_filter = SensitiveUrlFilter()
        test_logger.addFilter(test_filter)

        captured_records = []
        class ListHandler(logging.Handler):
            def emit(self, record):
                captured_records.append(self.format(record))

        handler = ListHandler()
        formatter = logging.Formatter("%(levelname)s: %(message)s")
        handler.setFormatter(formatter)
        test_logger.addHandler(handler)

        secret_token = "SUPER_SECRET_TRACEBACK_TOKEN_98765"
        try:
            raise ValueError(f"HTTP request failed on https://api.social.com/fetch?token={secret_token}&sign=abc12345")
        except ValueError as exc:
            test_logger.error("Download exception caught: %s", exc, exc_info=True)

        self.assertEqual(len(captured_records), 1)
        full_log_output = captured_records[0]
        self.assertNotIn(secret_token, full_log_output)
        self.assertNotIn("abc12345", full_log_output)
        self.assertIn("token=%5BREDACTED%5D", full_log_output)
        self.assertIn("sign=%5BREDACTED%5D", full_log_output)
        self.assertIn("Traceback", full_log_output)


if __name__ == "__main__":
    unittest.main()

