# -*- coding: utf-8 -*-
"""Unit & Integration tests for Phase C on Tool V2."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from segment_editor_service import (
    get_job_segments,
    parse_srt,
    format_srt,
    save_segment_draft,
)
from fastapi.testclient import TestClient
from dashboard_monitor import app

client = TestClient(app)

SAMPLE_V2_JOB = "batch_0f15ed6091_yt1 (109)"


class TestSegmentEditorV2(unittest.TestCase):
    def test_01_load_v2_job_segments(self):
        data = get_job_segments(SAMPLE_V2_JOB)
        self.assertGreater(data.get("total_segments", 0), 0)
        first_seg = data["segments"][0]
        self.assertIn("segment_id", first_seg)
        self.assertIn("translated_text", first_seg)
        self.assertIn("audio_url", first_seg)

    def test_02_rest_endpoints_v2(self):
        # 1. GET /api/jobs/{id}/segments
        res = client.get(f"/api/jobs/{SAMPLE_V2_JOB}/segments")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertGreater(len(data["segments"]), 0)

        # 2. PATCH /api/jobs/{id}/segments
        cur_rev = data["revision"]
        patch_payload = {
            "expected_revision": cur_rev,
            "segments": [{"segment_id": "seg_1", "translated_text": "Bản dịch sửa đổi trên V2."}],
        }
        patch_res = client.patch(f"/api/jobs/{SAMPLE_V2_JOB}/segments", json=patch_payload)
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["revision"], cur_rev + 1)


if __name__ == "__main__":
    unittest.main()
