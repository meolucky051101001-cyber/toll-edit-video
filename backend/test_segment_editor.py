# -*- coding: utf-8 -*-
"""Unit & Integration tests for Phase C: Segment Editing & Partial Regeneration."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from segment_editor_service import (
    format_srt,
    format_srt_time,
    get_job_segments,
    parse_srt,
    parse_srt_time,
    regenerate_segments,
    save_segment_draft,
)
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

SAMPLE_JOB_ID = "1790048525_99843237_douyin_7674877629485534574"


class TestSegmentEditor(unittest.TestCase):
    def test_01_srt_parsing_and_formatting(self):
        sample_srt = """1
00:00:01,000 --> 00:00:03,500
Xin chào các bạn

2
00:00:03,500 --> 00:00:06,200
Hôm nay chúng ta cùng đánh giá sản phẩm
"""
        segments = parse_srt(sample_srt)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 1.0)
        self.assertEqual(segments[0]["end"], 3.5)
        self.assertEqual(segments[0]["duration"], 2.5)
        self.assertIn("Xin chào", segments[0]["content"])

        rebuilt = format_srt(segments)
        self.assertIn("00:00:01,000 --> 00:00:03,500", rebuilt)
        self.assertIn("Xin chào các bạn", rebuilt)

    def test_02_load_job_segments(self):
        data = get_job_segments(SAMPLE_JOB_ID)
        self.assertEqual(data["job_id"], SAMPLE_JOB_ID)
        self.assertGreater(data["total_segments"], 0)
        first_seg = data["segments"][0]
        self.assertIn("segment_id", first_seg)
        self.assertIn("source_text", first_seg)
        self.assertIn("translated_text", first_seg)
        self.assertIn("audio_url", first_seg)

    def test_03_save_draft_and_optimistic_locking(self):
        data = get_job_segments(SAMPLE_JOB_ID)
        cur_rev = data.get("revision", 1)

        # 1. Successful draft save
        upd = [{"segment_id": "seg_1", "translated_text": "Câu dịch đã được người dùng chỉnh sửa nháp."}]
        res = save_segment_draft(SAMPLE_JOB_ID, upd, expected_revision=cur_rev)
        self.assertEqual(res["status"], "success")
        new_rev = res["revision"]
        self.assertEqual(new_rev, cur_rev + 1)

        # 2. Conflict test: Old revision should fail
        with self.assertRaises(ValueError):
            save_segment_draft(SAMPLE_JOB_ID, upd, expected_revision=cur_rev)

    def test_04_selective_regeneration(self):
        async def run():
            data = get_job_segments(SAMPLE_JOB_ID)
            cur_rev = data.get("revision", 1)

            res = await regenerate_segments(
                job_id_or_name=SAMPLE_JOB_ID,
                segment_ids=["seg_1"],
                expected_revision=cur_rev,
                idempotency_key="test_idemp_key_123",
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["regenerated_count"], 1)
            self.assertIn("results", res)
            first_res = res["results"][0]
            self.assertEqual(first_res["segment_id"], "seg_1")
            self.assertGreater(first_res["new_duration"], 0.5)

            # Test idempotency cache hit
            res_cached = await regenerate_segments(
                job_id_or_name=SAMPLE_JOB_ID,
                segment_ids=["seg_1"],
                expected_revision=res["revision"],
                idempotency_key="test_idemp_key_123",
            )
            self.assertEqual(res_cached["revision"], res["revision"])

        asyncio.run(run())

    def test_05_rest_endpoints(self):
        # 1. GET /api/jobs/{id}/segments
        res = client.get(f"/api/jobs/{SAMPLE_JOB_ID}/segments")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["job_id"], SAMPLE_JOB_ID)
        self.assertGreater(len(data["segments"]), 0)

        # 2. PATCH /api/jobs/{id}/segments
        cur_rev = data["revision"]
        patch_payload = {
            "expected_revision": cur_rev,
            "segments": [{"segment_id": "seg_1", "translated_text": "REST API Draft edit."}],
        }
        patch_res = client.patch(f"/api/jobs/{SAMPLE_JOB_ID}/segments", json=patch_payload)
        self.assertEqual(patch_res.status_code, 200)
        self.assertEqual(patch_res.json()["revision"], cur_rev + 1)

        # 3. GET /api/jobs/{id}/segments/{seg_id}/audio
        audio_res = client.get(f"/api/jobs/{SAMPLE_JOB_ID}/segments/seg_1/audio")
        self.assertEqual(audio_res.status_code, 200)
        self.assertEqual(audio_res.headers.get("content-type"), "audio/mpeg")
        self.assertGreater(len(audio_res.content), 256)


if __name__ == "__main__":
    unittest.main()
