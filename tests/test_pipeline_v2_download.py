import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.pipeline_v2.download_validation import (
    DownloadValidationError,
    probe_downloaded_video,
    require_complete_response,
    require_partial_content,
)


class PartialContentTests(unittest.TestCase):
    def test_range_requires_exact_206_and_content_range(self):
        require_partial_content(
            206,
            {"Content-Range": "bytes 100-199/1000"},
            100,
            199,
            1000,
        )
        with self.assertRaises(DownloadValidationError):
            require_partial_content(200, {}, 100, 199, 1000)
        with self.assertRaises(DownloadValidationError):
            require_partial_content(
                206,
                {"Content-Range": "bytes 0-999/1000"},
                100,
                199,
                1000,
            )

    def test_full_stream_rejects_incomplete_206(self):
        require_complete_response(200, {})
        require_complete_response(206, {"Content-Range": "bytes 0-999/1000"})
        with self.assertRaises(DownloadValidationError):
            require_complete_response(206, {"Content-Range": "bytes 0-499/1000"})


class DownloadProbeTests(unittest.TestCase):
    def test_probe_requires_container_video_stream_and_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.download"
            path.write_bytes(b"not-empty")
            response = SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "format": {"format_name": "mov,mp4", "duration": "12.5"},
                        "streams": [
                            {"codec_type": "video"},
                            {"codec_type": "audio"},
                        ],
                    }
                ),
                stderr="",
            )
            with patch(
                "backend.pipeline_v2.download_validation.subprocess.run",
                return_value=response,
            ):
                probe = probe_downloaded_video(path, expected_duration_seconds=12.0)
            self.assertEqual(probe.video_stream_count, 1)
            self.assertAlmostEqual(probe.duration_seconds, 12.5)

    def test_xhs_clean_origin_cdn_patch_is_preserved(self):
        source_path = Path(__file__).parents[1] / "backend" / "social_downloader.py"
        module = ast.parse(source_path.read_text(encoding="utf-8"))
        assignment = next(
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "XHS_ORIGIN_CDN_DOMAINS"
                for target in node.targets
            )
        )
        hosts = "\n".join(ast.literal_eval(assignment.value))
        for name in ("qn", "ws", "ct", "bd", "qc", "hw", "al"):
            self.assertIn("sns-video-{}".format(name), hosts)

    def test_xhs_rejects_non_200_response_before_parsing(self):
        from backend import social_downloader

        response = SimpleNamespace(
            status_code=403,
            url="https://www.xiaohongshu.com/explore/item",
            text="forbidden",
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            social_downloader.requests, "get", return_value=response
        ):
            ok, path, title, error = social_downloader.download_xiaohongshu(
                "https://xhslink.com/o/example", directory, "test"
            )
        self.assertFalse(ok)
        self.assertEqual(path, "")
        self.assertEqual(title, "")
        self.assertIn("HTTP 403", error)

    def test_xhs_deleted_redirect_ignores_query_string(self):
        from backend import social_downloader

        response = SimpleNamespace(
            status_code=200,
            url="https://www.xiaohongshu.com/explore?app_platform=ios",
            text="",
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            social_downloader.requests, "get", return_value=response
        ):
            ok, _, _, error = social_downloader.download_xiaohongshu(
                "https://xhslink.com/o/example", directory, "test"
            )
        self.assertFalse(ok)
        self.assertIn("không tồn tại", error)


class DouyinDirectTests(unittest.TestCase):
    def test_signed_detail_url_prefers_abogus_when_available(self):
        from backend import douyin_direct

        signed = douyin_direct._signed_url(
            douyin_direct.DOUYIN_DETAIL_PATH,
            {"aid": "6383", "aweme_id": "7676769981752790308"},
        )
        self.assertIn("a_bogus=", signed)

    def test_xbogus_matches_upstream_vector(self):
        from backend.douyin_direct import DOUYIN_USER_AGENT, _XBogus

        url = (
            "https://www.douyin.com/aweme/v1/web/aweme/detail/"
            "?aid=6383&aweme_id=7676769981752790308"
        )
        with patch("backend.douyin_direct.time.time", return_value=1700000000):
            signed = _XBogus(DOUYIN_USER_AGENT).build(url)
        self.assertEqual(
            signed,
            url + "&X-Bogus=DFSzswVYM3hANj00tmWx-e9WX7jU",
        )

    def test_resolver_prefers_highest_clean_direct_cdn(self):
        from backend.douyin_direct import resolve_douyin_video

        response = SimpleNamespace(
            status_code=200,
            json=lambda: {
                "aweme_detail": {
                    "aweme_id": "7676769981752790308",
                    "desc": "video thử nghiệm",
                    "video": {
                        "bit_rate": [
                            {
                                "bit_rate": 500000,
                                "play_addr": {
                                    "width": 720,
                                    "height": 1280,
                                    "url_list": ["https://low.example/video.mp4"],
                                },
                            },
                            {
                                "bit_rate": 2000000,
                                "play_addr": {
                                    "width": 1080,
                                    "height": 1920,
                                    "url_list": [
                                        "https://water.example/playwm/video.mp4",
                                        "https://high.example/video.mp4?watermark=0",
                                    ],
                                },
                            },
                        ]
                    },
                }
            },
        )
        session = SimpleNamespace(get=lambda *args, **kwargs: response)
        info = resolve_douyin_video(
            "7676769981752790308",
            session=session,
            environment={"DOUYIN_COOKIE": "sessionid=must-not-reach-cdn"},
        )
        self.assertEqual(info.title, "video thử nghiệm")
        self.assertEqual(info.media_urls[0], "https://high.example/video.mp4?watermark=0")
        self.assertNotIn("playwm", "\n".join(info.media_urls))
        self.assertNotIn("Cookie", info.download_headers)

    def test_cookie_file_supports_netscape_format_without_logging_value(self):
        from backend.douyin_direct import load_douyin_cookies

        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".douyin.com\tTRUE\t/\tTRUE\t0\tmsToken\tsecret-token\n"
                "#HttpOnly_.douyin.com\tTRUE\t/\tTRUE\t0\tttwid\tweb-token\n"
                ".evildouyin.com\tTRUE\t/\tTRUE\t0\tbad1\tsecret\n"
                ".douyin.com.attacker.tld\tTRUE\t/\tTRUE\t0\tbad2\tsecret\n"
                ".example.com\tTRUE\t/\tTRUE\t0\tignored\tsecret\n",
                encoding="utf-8",
            )
            cookies = load_douyin_cookies({"DOUYIN_COOKIE_FILE": str(cookie_file)})
        self.assertEqual(cookies, {"msToken": "secret-token", "ttwid": "web-token"})

    def test_social_downloader_uses_direct_resolver_before_tikwm(self):
        from backend import social_downloader
        from backend.douyin_direct import DouyinVideoInfo

        info = DouyinVideoInfo(
            title="direct",
            media_urls=("https://cdn.example/clean.mp4",),
            download_headers={"User-Agent": "test"},
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            social_downloader, "resolve_douyin_video", return_value=info
        ) as resolver, patch.object(
            social_downloader, "download_file_stream", return_value=True
        ), patch.object(
            social_downloader.requests, "post"
        ) as tikwm:
            ok, path, title, error = social_downloader.download_douyin_tiktok(
                "https://www.douyin.com/video/7676769981752790308",
                directory,
                "job",
            )
        self.assertTrue(ok)
        self.assertTrue(path.endswith("job_direct.mp4"))
        self.assertEqual(title, "direct")
        self.assertEqual(error, "")
        resolver.assert_called_once_with("7676769981752790308")
        tikwm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
