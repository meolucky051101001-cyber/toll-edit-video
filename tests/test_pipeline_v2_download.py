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


if __name__ == "__main__":
    unittest.main()
