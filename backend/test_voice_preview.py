# -*- coding: utf-8 -*-
"""Unit & Integration tests for Phase B: Voice Preview Service."""

import asyncio
import os
import sys
import unittest
from pathlib import Path

backend_dir = Path(__file__).resolve().parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from voice_preview_service import (
    SAMPLE_TEXTS,
    clean_cache,
    get_preview_audio_path,
    get_voice_catalog,
    synthesize_voice_preview,
)
from fastapi.testclient import TestClient
from main import app

client = TestClient(app)


class TestVoicePreviewService(unittest.TestCase):
    def test_01_catalog_and_sample_texts(self):
        cat = get_voice_catalog()
        self.assertGreater(len(cat), 0)
        voice_ids = [v["id"] for v in cat]
        self.assertIn("microsoft-hoaimy", voice_ids)
        self.assertIn("microsoft-namminh", voice_ids)

        self.assertGreater(len(SAMPLE_TEXTS), 2)

    def test_02_synthesis_and_caching(self):
        async def run():
            # First call: synthesize fresh or copy verified sample
            res1 = await synthesize_voice_preview(
                voice_id="microsoft-hoaimy",
                text="Xin chào, đây là bài kiểm thử âm thanh tự động.",
                speed=1.0,
            )
            self.assertEqual(res1["status"], "ready")
            self.assertIn("preview_id", res1)
            self.assertIn("audio_url", res1)
            self.assertGreater(res1["duration"], 0.5)

            # Second call: MUST hit cache
            res2 = await synthesize_voice_preview(
                voice_id="microsoft-hoaimy",
                text="Xin chào, đây là bài kiểm thử âm thanh tự động.",
                speed=1.0,
            )
            self.assertTrue(res2["cached"])
            self.assertEqual(res1["preview_id"], res2["preview_id"])

            # Call with speed 1.3: Must have shorter duration than speed 1.0
            res_fast = await synthesize_voice_preview(
                voice_id="microsoft-hoaimy",
                text="Xin chào, đây là bài kiểm thử âm thanh tự động.",
                speed=1.3,
            )
            self.assertEqual(res_fast["status"], "ready")
            self.assertLess(res_fast["duration"], res1["duration"])

        asyncio.run(run())

    def test_03_rest_endpoints(self):
        # 1. GET /api/voice-preview/sample-texts
        res = client.get("/api/voice-preview/sample-texts")
        self.assertEqual(res.status_code, 200)
        self.assertIn("sample_texts", res.json())

        # 2. GET /api/voice-preview/catalog
        res = client.get("/api/voice-preview/catalog")
        self.assertEqual(res.status_code, 200)
        self.assertIn("voices", res.json())

        # 3. POST /api/voice-preview
        payload = {
            "voice_id": "microsoft-namminh",
            "text": "Kiểm tra phản hồi qua giao thức REST API.",
            "speed": 1.0,
        }
        res = client.post("/api/voice-preview", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ready")
        preview_id = data["preview_id"]

        # 4. GET /api/voice-preview/{id}/audio
        audio_res = client.get(f"/api/voice-preview/{preview_id}/audio")
        self.assertEqual(audio_res.status_code, 200)
        self.assertEqual(audio_res.headers.get("content-type"), "audio/mpeg")
        self.assertGreater(len(audio_res.content), 256)


if __name__ == "__main__":
    unittest.main()
