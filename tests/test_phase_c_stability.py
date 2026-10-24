# -*- coding: utf-8 -*-
"""Kiểm thử ổn định vận hành (GĐ1): merge cấu hình worker-settings, hồi phục VRAM throttle, chặn sửa video đang chạy."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import job_tracker
import main
import phase_c_routes
import worker_settings


class TestWorkerSettingsStability(unittest.TestCase):
    def test_partial_payload_keeps_existing_thresholds(self):
        """POST /api/worker-settings chỉ gửi {concurrency} — ngưỡng VRAM cũ phải được giữ nguyên (hết lỗi trượt 1500 -> 1800)."""
        current = {
            "concurrency": 2,
            "effective_concurrency": 2,
            "auto_vram_guard": False,
            "max_vram_percent_threshold": 80.0,
            "min_free_vram_mb": 1400.0,
            "auto_throttled": False,
            "throttle_reason": "",
            "throttled_at": 0.0,
        }
        client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})
        with patch.object(main, "get_worker_settings", return_value=current), \
             patch.object(main, "save_worker_settings", return_value=dict(current)) as save_mock:
            resp = client.post("/api/worker-settings", json={"concurrency": 1})
        self.assertEqual(resp.status_code, 200)
        save_mock.assert_called_once_with(1, False, 80.0, 1400.0)

    def test_reset_vram_throttle_restores_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "worker_settings.json"
            tmp_path.write_text(json.dumps({
                "concurrency": 2,
                "auto_vram_guard": False,
                "max_vram_percent_threshold": 85.0,
                "min_free_vram_mb": 1500.0,
                "auto_throttled": True,
                "throttle_reason": "test",
                "throttled_at": 123.0,
            }), encoding="utf-8")
            old = worker_settings.SETTINGS_FILE
            worker_settings.SETTINGS_FILE = tmp_path
            try:
                ok = worker_settings.reset_vram_throttle()
                data = json.loads(tmp_path.read_text(encoding="utf-8"))
            finally:
                worker_settings.SETTINGS_FILE = old
        self.assertTrue(ok)
        self.assertFalse(data["auto_throttled"])
        self.assertEqual(data["throttle_reason"], "")
        # guard người dùng đã tắt phải được tôn trọng, không bị ép True
        self.assertFalse(data["auto_vram_guard"])


class TestSegmentEditGuards(unittest.TestCase):
    def _fake_status(self, active, video_name, workers=None):
        return {"active": active, "video_name": video_name, "workers": workers or {}}

    def test_not_blocked_when_idle(self):
        with patch.object(job_tracker, "get_status", return_value=self._fake_status(False, "")):
            self.assertFalse(phase_c_routes._job_is_being_processed("telegram_abc"))

    def test_blocked_when_same_video_running(self):
        st = self._fake_status(True, "telegram_abc_视频.mp4", {"1": {"video_name": "telegram_abc_视频.mp4"}})
        with patch.object(job_tracker, "get_status", return_value=st):
            self.assertTrue(phase_c_routes._job_is_being_processed("telegram_abc_视频"))

    def test_not_blocked_when_other_video_running(self):
        st = self._fake_status(True, "telegram_other_123.mp4", {"1": {"video_name": "telegram_other_123.mp4"}})
        with patch.object(job_tracker, "get_status", return_value=st):
            self.assertFalse(phase_c_routes._job_is_being_processed("telegram_abc"))


if __name__ == "__main__":
    unittest.main()
