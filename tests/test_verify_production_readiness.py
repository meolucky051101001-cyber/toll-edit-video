# -*- coding: utf-8 -*-
"""Verification of production readiness across all recently modified components."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from backend.ai.translation import _parse_json_array, _subdivide_and_retry, _contains_cjk, translate_subtitles
from backend.script_api import _validate_safe_video_path, SCRIPT_WORKSPACE
from backend.ocr_utils import perform_video_ocr, value
from backend.ai.v1_voice_cache import is_valid_audio
from backend.ai.v1_gemini_dispatcher import mask_secret
import backend.main as main


class ProductionReadinessTests(unittest.TestCase):
    def test_cjk_detection(self):
        self.assertTrue(_contains_cjk("你好"))
        self.assertTrue(_contains_cjk("\u3400"))  # CJK Extension A
        self.assertFalse(_contains_cjk("Xin chào Việt Nam!"))

    def test_json_resilience(self):
        # Markdown block
        self.assertEqual(_parse_json_array('```json\n["a", "b"]\n```'), ["a", "b"])
        # Trailing comma
        self.assertEqual(_parse_json_array('["a", "b", ]'), ["a", "b"])
        # Python literal list
        self.assertEqual(_parse_json_array("['Câu 1', 'Câu 2']"), ["Câu 1", "Câu 2"])

    def test_secret_masking(self):
        prefix = "AQ" + "."
        body = "MockUnitTestTokenBody0123456789abcdefghijkl"
        key = prefix + body
        url_masked = mask_secret(f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={key}")
        self.assertNotIn("MockUnitTestTokenBody", url_masked)
        self.assertIn("[REDACTED]", url_masked)

        standalone_masked = mask_secret(f"Using key: {key}")
        self.assertNotIn("MockUnitTestTokenBody", standalone_masked)
        self.assertIn("AQ.", standalone_masked)
        self.assertIn("...", standalone_masked)

    def test_safe_video_path_allows_workspace_and_blocks_traversal(self):
        # Inside workspace
        test_file = SCRIPT_WORKSPACE / "test_sample.mp4"
        test_file.write_bytes(b"dummy-mp4")
        try:
            resolved = _validate_safe_video_path(str(test_file))
            self.assertEqual(resolved, test_file.resolve())
        finally:
            test_file.unlink(missing_ok=True)

        # Non-existent or outside path
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            _validate_safe_video_path(r"C:\Windows\System32\drivers\etc\hosts")
        self.assertIn(ctx.exception.status_code, (400, 403, 404))

    def test_main_security_middleware_timing_safe_token(self):
        import secrets
        self.assertTrue(secrets.compare_digest(main.LOCAL_TOKEN, main.LOCAL_TOKEN))
        self.assertFalse(secrets.compare_digest("wrong-token-value", main.LOCAL_TOKEN))

    def test_main_cors_excludes_null(self):
        for origin in main.ALLOWED_CORS_ORIGINS:
            self.assertNotEqual(origin.lower(), "null")

    def test_subtitle_free_video_with_scene_cjk_does_not_fail_ocr_geometry(self):
        import types
        from datetime import timedelta
        import numpy as np
        import backend.ocr_utils as ocr_utils

        segs = [
            types.SimpleNamespace(
                index=i,
                content=f"这是第{i}句配音旁白完全没有字幕",
                start=timedelta(seconds=(i - 1) * 3.0),
                end=timedelta(seconds=i * 3.0),
            )
            for i in range(1, 6)
        ]

        class DummyCap:
            def isOpened(self):
                return True
            def get(self, prop):
                import cv2
                return {
                    cv2.CAP_PROP_FRAME_WIDTH: 720,
                    cv2.CAP_PROP_FRAME_HEIGHT: 1280,
                    cv2.CAP_PROP_FPS: 30.0,
                    cv2.CAP_PROP_FRAME_COUNT: 450,
                }.get(prop, 0)
            def set(self, prop, val):
                pass
            def read(self):
                return True, np.zeros((1280, 720, 3), dtype=np.uint8)
            def release(self):
                pass

        # Scene contains static manga/book CJK text ("超能力者") that does not match ASR speech
        def fake_readtext(frames):
            return [[([[200, 800], [450, 800], [450, 1150], [200, 1150]], "超能力者", 0.96)] for _ in frames]

        raw_ocr = ocr_utils.perform_video_ocr
        while hasattr(raw_ocr, "__wrapped__"):
            raw_ocr = raw_ocr.__wrapped__

        with patch.object(ocr_utils.cv2, "VideoCapture", return_value=DummyCap()), \
             patch.object(ocr_utils, "_readtext_batch", side_effect=fake_readtext):
            raw_ocr("manga_coloring.mp4", srt_segments=segs, ocr_strategy="smart_skip", adaptive=False)

        self.assertTrue(all(s.ocr_mask_status == "no_caption_evidence" for s in segs))


if __name__ == "__main__":
    unittest.main()

