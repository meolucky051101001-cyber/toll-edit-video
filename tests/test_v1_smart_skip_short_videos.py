# -*- coding: utf-8 -*-
import os
import unittest
from unittest import mock
from pathlib import Path

from backend.v1_video_router import route_video, VideoMode, VideoMetadata, get_router_thresholds
from backend.ocr_utils import perform_video_ocr


class SmartSkipShortVideoTests(unittest.TestCase):

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_short_standard_video_uses_full_ocr(self, mock_extract):
        # 30s video 1080p 30fps
        mock_extract.return_value = VideoMetadata(
            duration_s=30.0, width=1080, height=1920, fps=30.0, bitrate_kbps=4000.0
        )
        dec = route_video("short.mp4", requested_mode="auto")
        self.assertEqual(dec.planned_pipeline["ocr_strategy"], "full")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_short_escalated_video_still_uses_full_ocr(self, mock_extract):
        # 36s video 4K 60fps (escalated to MEDIUM for separation/chunking)
        # Smart skip OCR must be turned OFF for this short video!
        mock_extract.return_value = VideoMetadata(
            duration_s=36.4, width=2160, height=3840, fps=60.0, bitrate_kbps=50000.0
        )
        dec = route_video("short_4k_60fps.mp4", requested_mode="auto")
        self.assertEqual(dec.resolved_mode, VideoMode.MEDIUM)
        self.assertTrue(dec.is_escalated)
        # Even though escalated to MEDIUM, ocr_strategy MUST be "full"
        self.assertEqual(dec.planned_pipeline["ocr_strategy"], "full", "Smart skip OCR must be turned off for short videos!")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_long_video_over_7min_uses_smart_skip(self, mock_extract):
        # 480s video (8 min > 7 min)
        mock_extract.return_value = VideoMetadata(
            duration_s=480.0, width=1920, height=1080, fps=30.0, bitrate_kbps=4000.0
        )
        dec = route_video("long_8m.mp4", requested_mode="auto")
        self.assertEqual(dec.resolved_mode, VideoMode.LONG)
        self.assertEqual(dec.planned_pipeline["ocr_strategy"], "smart_skip")


if __name__ == "__main__":
    unittest.main()
