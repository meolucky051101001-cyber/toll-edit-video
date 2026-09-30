# -*- coding: utf-8 -*-
"""Unit & Integration tests for Phase B on Tool V2."""

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
from dashboard_monitor import app

client = TestClient(app)


class TestVoicePreviewV2(unittest.TestCase):
    def test_01_catalog_and_sample_texts(self):
        cat = get_voice_catalog()
        self.assertGreater(len(cat), 0)
        self.assertGreater(len(SAMPLE_TEXTS), 2)

    def test_02_rest_endpoints(self):
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
            "voice_id": "microsoft-hoaimy",
            "text": "Kiểm thử nghe thử trên hệ thống V2.",
            "speed": 1.1,
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
