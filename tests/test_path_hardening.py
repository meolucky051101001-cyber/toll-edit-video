"""Hồi quy nhóm 4: safe_stem dùng chung, URL encode, allowlist xfade/export, sanitize canvas."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi import HTTPException
from fastapi.testclient import TestClient

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import workflow_api
import main as main_app
from main import app
from workflow_api import (
    _build_timeline_filter_graph,
    artifact_stem,
    build_ass_force_style,
    build_watermark_filter,
    resolve_output_dir,
    safe_font_name,
    safe_hex_color,
    safe_stem,
    safe_version,
    stream_file_url,
)


def _assert_400(test, fn, *args, **kwargs):
    with test.assertRaises(HTTPException) as ctx:
        fn(*args, **kwargs)
    test.assertEqual(ctx.exception.status_code, 400, ctx.exception.detail)
    return ctx.exception


class SafeStemTests(unittest.TestCase):
    def test_accepts_real_filenames_with_spaces_and_unicode(self):
        self.assertEqual(safe_stem("batch_9f2_测试 视频 01"), "batch_9f2_测试 视频 01")
        self.assertEqual(safe_stem("  video A&B #1 +x  "), "video A&B #1 +x")

    def test_rejects_empty_and_traversal(self):
        for value in (None, "", "   ", ".", ".."):
            _assert_400(self, safe_stem, value)

    def test_rejects_separators_paths_and_control_chars(self):
        for value in ("a/b", "a\\b", "../evil", "x/../../y", "C:evil", "bad<name>", "a\x00b", "a\nb"):
            _assert_400(self, safe_stem, value)

    def test_rejects_overlong_stem(self):
        _assert_400(self, safe_stem, "a" * 181)
        self.assertEqual(len(safe_stem("a" * 180)), 180)

    def test_artifact_stem_is_safe_filename_token(self):
        cleaned = artifact_stem("../evil")
        self.assertNotIn("/", cleaned)
        self.assertNotIn("\\", cleaned)
        self.assertEqual(cleaned, "evil")
        self.assertEqual(artifact_stem("My Clip 01"), "My_Clip_01")
        self.assertEqual(artifact_stem("..."), "video")
        self.assertEqual(len(artifact_stem("a" * 300, 40)), 40)

    def test_safe_version_allowlist(self):
        self.assertEqual(safe_version("FINAL"), "final")
        self.assertEqual(safe_version(" raw "), "raw")
        for value in ("", None, "clean", "../final", "final.mp4"):
            _assert_400(self, safe_version, value)


class StreamUrlTests(unittest.TestCase):
    def test_encodes_special_characters_in_path(self):
        path = Path(r"D:\banve\视频 A&B #1 +x.mp4")
        url = stream_file_url(path)
        self.assertEqual(url.split("?", 1)[0], "/api/workflow/stream-file")
        query = url.split("?", 1)[1]
        self.assertNotIn("&", query)
        self.assertNotIn("#", query)
        self.assertEqual(parse_qs(urlparse(url).query)["path"][0], str(path))
        self.assertNotIn("t", parse_qs(urlparse(url).query))

    def test_cache_bust_adds_numeric_timestamp(self):
        url = stream_file_url(Path(r"D:\banve\out.mp4"), cache_bust=True)
        stamp = parse_qs(urlparse(url).query).get("t", [""])[0]
        self.assertTrue(stamp.isdigit(), stamp)


class CanvasAssetUrlTests(unittest.TestCase):
    def test_asset_url_encodes_filename(self):
        from canvas_settings import _stream_url

        path = "D:/assets/nền & #1 +x.mp4"
        url = _stream_url(path)
        query = url.split("?", 1)[1]
        self.assertNotIn("&", query)
        self.assertNotIn("#", query)
        self.assertEqual(parse_qs(urlparse(url).query)["path"][0], path)


class CanvasSanitizeTests(unittest.TestCase):
    def test_ass_style_falls_back_on_injected_font_and_colors(self):
        style = build_ass_force_style({
            "sub_font_name": "Arial; PrimaryColour=&H00FF0000",
            "sub_font_color": "red;PrimaryColour",
            "sub_bg_color": "#fff",
            "sub_stroke_color": "url(evil)",
        })
        self.assertIn("FontName=Arial,", style)
        self.assertNotIn("Arial;", style)
        self.assertIn("PrimaryColour=&H00000000", style)
        self.assertIn("BackColour=&H00ffffff", style)
        self.assertNotIn("url(evil)", style)

    def test_ass_style_accepts_shorthand_hex(self):
        style = build_ass_force_style({"sub_font_color": "#f00", "sub_bg_opacity": 0.0})
        self.assertIn("PrimaryColour=&H000000ff", style)

    def test_watermark_filter_sanitizes_colors(self):
        filt = build_watermark_filter({
            "watermark_enabled": True,
            "watermark_text": "abc",
            "watermark_color": "red:box=1",
            "watermark_bg_enabled": True,
            "watermark_bg_color": "x@1",
        }, 1080, 1920)
        self.assertIn("fontcolor=0xffffff@0.45", filt)
        self.assertIn("boxcolor=0x000000@0.40", filt)
        self.assertNotIn("red", filt)
        self.assertNotIn("x@1", filt)

    def test_watermark_filter_accepts_valid_color(self):
        filt = build_watermark_filter({
            "watermark_enabled": True,
            "watermark_text": "abc",
            "watermark_color": "#0F0",
        }, 1080, 1920)
        self.assertIn("fontcolor=0x00ff00@", filt)

    def test_color_and_font_helpers(self):
        self.assertEqual(safe_hex_color("fff", "#000000"), "#ffffff")
        self.assertEqual(safe_hex_color("#AABBCC", "#000000"), "#aabbcc")
        self.assertEqual(safe_hex_color("#GGGGGG", "#123456"), "#123456")
        self.assertEqual(safe_hex_color(None, "#abc"), "#abc")
        self.assertEqual(safe_font_name("Times New Roman"), "Times New Roman")
        self.assertEqual(safe_font_name("../../evil"), "Arial")
        self.assertEqual(safe_font_name("-bad"), "Arial")
        self.assertEqual(safe_font_name(""), "Arial")


class XfadeAllowlistTests(unittest.TestCase):
    @staticmethod
    def _clip(start, end, **kw):
        clip = {"start": start, "end": end}
        clip.update(kw)
        return clip

    def test_unknown_transition_is_skipped_and_pair_falls_back_to_concat(self):
        graph = _build_timeline_filter_graph([
            self._clip(0, 4, trans={"type": "fade:drawtext=text=x", "dur": 0.5}),
            self._clip(4, 8),
        ], has_audio=False)
        joined = ";".join(graph["filter_lines"])
        self.assertNotIn("xfade", joined)
        self.assertNotIn("drawtext", joined)
        self.assertIn("concat=n=2:v=1:a=0[outv]", joined)
        self.assertAlmostEqual(graph["total_dur"], 8.0, places=3)

    def test_transition_type_is_case_insensitive_and_allowlisted(self):
        graph = _build_timeline_filter_graph([
            self._clip(0, 4, trans={"type": "FADE", "dur": 0.5}),
            self._clip(4, 8),
        ], has_audio=False)
        self.assertIn("xfade=transition=fade:duration=0.500", ";".join(graph["filter_lines"]))


class ExportValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.output_dir = Path(self._tmp.name) / "output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        patcher = patch.object(workflow_api, "OUTPUT_DIR", self.output_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _payload(self, **overrides):
        payload = {
            "stem": "vid",
            "resolution": "720p",
            "bitrate_kbps": 500,
            "fps": "30",
            "format": "mp4",
            "export_srt": False,
            "output_folder": str(self.output_dir),
            "prepend_thumbnail": False,
            "canvas_settings": {"aspect_ratio": "9:16", "bg_color": "#f59e0b"},
        }
        payload.update(overrides)
        return payload

    def test_rejects_unknown_format_before_touching_input(self):
        for fmt in ("webm", "mp4 -vf scale=1:1", "MP4;rm", ""):
            err = _assert_400(self, workflow_api._execute_custom_export, self._payload(format=fmt))
            self.assertIn("Định dạng xuất", err.detail)

    def test_rejects_relative_or_file_output_folder(self):
        err = _assert_400(
            self, workflow_api._execute_custom_export,
            self._payload(output_folder="relative/output"),
        )
        self.assertIn("tuyệt đối", err.detail)

        target_file = Path(self._tmp.name) / "not_a_dir.mp4"
        target_file.write_bytes(b"x")
        err = _assert_400(
            self, workflow_api._execute_custom_export,
            self._payload(output_folder=str(target_file)),
        )
        self.assertIn("tệp", err.detail)

    def test_resolve_output_dir_creates_and_normalizes(self):
        nested = Path(self._tmp.name) / "a" / "b"
        resolved = resolve_output_dir(f'"{nested}"', self.output_dir)
        self.assertTrue(resolved.is_dir())
        self.assertEqual(resolved, nested.resolve())

    def test_rejects_bad_bitrate_and_resolution(self):
        _assert_400(self, workflow_api._execute_custom_export, self._payload(bitrate_kbps="abc"))
        _assert_400(self, workflow_api._execute_custom_export, self._payload(bitrate_kbps=50))
        _assert_400(self, workflow_api._execute_custom_export, self._payload(bitrate_kbps=100001))
        _assert_400(self, workflow_api._execute_custom_export, self._payload(resolution="8k"))

    def test_rejects_traversal_stem(self):
        _assert_400(self, workflow_api._execute_custom_export, self._payload(stem="../../evil"))


class PathEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.input_dir = root / "input"
        self.output_dir = root / "output"
        self.downloads = root / "downloads"
        self.thumbs = root / "thumbs"
        for d in (self.workspace, self.input_dir, self.output_dir, self.downloads, self.thumbs):
            d.mkdir(parents=True, exist_ok=True)
        for name, value in {
            "WORKSPACE": self.workspace,
            "INPUT_DIR": self.input_dir,
            "OUTPUT_DIR": self.output_dir,
            "DOWNLOADS_DIR": self.downloads,
            "THUMBNAILS_DIR": self.thumbs,
        }.items():
            patcher = patch.object(workflow_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})

    def test_video_data_rejects_traversal_stem(self):
        for stem in ("..", "../evil", "a/b", "a\\b"):
            res = self.client.get("/api/workflow/video-data", params={"stem": stem})
            self.assertEqual(res.status_code, 400, res.text)

    def test_download_srt_rejects_bad_stem_and_type(self):
        res = self.client.get("/api/workflow/download-srt", params={"stem": ".."})
        self.assertEqual(res.status_code, 400, res.text)
        res = self.client.get("/api/workflow/download-srt", params={"stem": "vid", "type": "../../x"})
        self.assertEqual(res.status_code, 400, res.text)

    def test_export_endpoint_rejects_bad_stem_before_queue(self):
        payload = {
            "stem": "../evil",
            "resolution": "720p",
            "bitrate_kbps": 500,
            "format": "mp4",
            "output_folder": str(self.output_dir),
        }
        with workflow_api._EXPORT_JOBS_LOCK:
            workflow_api._EXPORT_JOBS.clear()
        res = self.client.post("/api/workflow/export-custom", json=payload)
        self.assertEqual(res.status_code, 400, res.text)
        with workflow_api._EXPORT_JOBS_LOCK:
            self.assertEqual(workflow_api._EXPORT_JOBS, {})


if __name__ == "__main__":
    unittest.main()
