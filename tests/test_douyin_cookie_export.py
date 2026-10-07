import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.export_douyin_cookies import (
    _copy_minimal_browser_profile,
    _playwright_cookie,
    filtered_cookie_jar,
    is_douyin_cookie,
    main,
)


class DouyinCookieExportTests(unittest.TestCase):
    def test_only_douyin_domains_are_allowed(self):
        self.assertTrue(is_douyin_cookie(SimpleNamespace(domain=".douyin.com")))
        self.assertTrue(is_douyin_cookie(SimpleNamespace(domain="www.douyin.com")))
        self.assertTrue(is_douyin_cookie(SimpleNamespace(domain=".iesdouyin.com")))
        self.assertFalse(is_douyin_cookie(SimpleNamespace(domain="facebook.com")))
        self.assertFalse(is_douyin_cookie(SimpleNamespace(domain="evildouyin.com")))
        self.assertFalse(
            is_douyin_cookie(SimpleNamespace(domain="douyin.com.attacker.invalid"))
        )

    def test_playwright_cookie_keeps_http_only_metadata(self):
        cookie = _playwright_cookie(
            {
                "name": "ttwid",
                "value": "secret",
                "domain": ".douyin.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
                "expires": 1900000000,
            }
        )
        self.assertEqual(cookie.name, "ttwid")
        self.assertEqual(cookie.domain, ".douyin.com")
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.has_nonstandard_attr("HTTPOnly"))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "cookies.txt"
            jar = filtered_cookie_jar([cookie], output)
            jar.save(ignore_discard=True, ignore_expires=True)
            self.assertIn("#HttpOnly_.douyin.com", output.read_text(encoding="utf-8"))

    def test_minimal_profile_copy_excludes_history_and_passwords(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as target_dir:
            source = Path(source_dir)
            target = Path(target_dir)
            (source / "Default" / "Network").mkdir(parents=True)
            (source / "Local State").write_text("state", encoding="utf-8")
            (source / "Default" / "Network" / "Cookies").write_bytes(b"cookies")
            (source / "Default" / "History").write_bytes(b"history")
            (source / "Default" / "Login Data").write_bytes(b"passwords")

            _copy_minimal_browser_profile(source, "Default", target)

            self.assertTrue((target / "Local State").is_file())
            self.assertTrue((target / "Default" / "Network" / "Cookies").is_file())
            self.assertFalse((target / "Default" / "History").exists())
            self.assertFalse((target / "Default" / "Login Data").exists())

    def test_profile_name_cannot_escape_user_data_directory(self):
        for profile in ("..", r"..\Other", r"Default\Network", r"C:\Other"):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                _copy_minimal_browser_profile(Path("unused"), profile, Path("unused"))

    def test_user_data_dir_requires_playwright_method(self):
        argv = [
            "export_douyin_cookies.py",
            "--user-data-dir",
            r"D:\autodub_douyin_profile",
            "--output",
            r"D:\autodub_secrets\douyin-cookies.txt",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)

    def test_playwright_method_rejects_firefox(self):
        argv = [
            "export_douyin_cookies.py",
            "--browser",
            "firefox",
            "--method",
            "playwright",
            "--output",
            r"D:\autodub_secrets\douyin-cookies.txt",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
            main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
