# -*- coding: utf-8 -*-
"""Kiểm thử GĐ2: F13 Trung tâm thông báo, F1 Thống kê hiệu suất, F9 Lịch chạy đêm."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main
import notification_service
import stats_service


class TestNotificationService(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.control = Path(self.temporary.name) / "control"
        self.control.mkdir()
        self.old_notif = notification_service.NOTIF_FILE
        notification_service.NOTIF_FILE = self.control / "notifications.json"

    def tearDown(self):
        notification_service.NOTIF_FILE = self.old_notif
        self.temporary.cleanup()

    def test_push_list_unread_mark_read_roundtrip(self):
        notification_service.push("error", "Video lỗi", "lỗi giả lập", "test")
        notification_service.push("success", "Hoàn tất", "video A", "test")
        items = notification_service.list_notifications()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["title"], "Hoàn tất")  # mới nhất lên đầu
        self.assertEqual(notification_service.unread_count(), 2)
        changed = notification_service.mark_read(mark_all=True)
        self.assertEqual(changed, 2)
        self.assertEqual(notification_service.unread_count(), 0)
        self.assertEqual(notification_service.list_notifications(unread_only=True), [])

    def test_ring_buffer_cap(self):
        for i in range(120):
            notification_service.push("info", f"n{i}", "", "test")
        self.assertEqual(len(notification_service.list_notifications(limit=100)), 100)

    def test_corrupt_file_resets(self):
        notification_service.NOTIF_FILE.write_text("not json", encoding="utf-8")
        item = notification_service.push("info", "sau khi hỏng", "", "test")
        self.assertTrue(item["id"])
        self.assertEqual(len(notification_service.list_notifications()), 1)


class TestNotificationEndpoints(unittest.TestCase):
    def test_list_and_mark_read_over_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = notification_service.NOTIF_FILE
            notification_service.NOTIF_FILE = Path(tmp) / "notifications.json"
            try:
                notification_service.push("info", "Thông báo test API", "", "test")
                client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})
                res = client.get("/api/notifications")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json()["unread_count"], 1)
                res2 = client.post("/api/notifications/mark-read", json={"all": True})
                self.assertEqual(res2.status_code, 200)
                self.assertEqual(res2.json()["marked"], 1)
            finally:
                notification_service.NOTIF_FILE = old


class TestStatsService(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        (Path(self.temporary.name) / "control").mkdir()
        self.old_stats = stats_service.STATS_FILE
        stats_service.STATS_FILE = Path(self.temporary.name) / "control" / "video_stats.json"

    def tearDown(self):
        stats_service.STATS_FILE = self.old_stats
        self.temporary.cleanup()

    def test_summary_aggregates(self):
        stats_service.append_video_stats("a.mp4", 60.0, 300.0, {"2": 100.0, "8": 50.0}, "SHORT", True)
        stats_service.append_video_stats("b.mp4", 60.0, 400.0, {"2": 200.0}, "SHORT", True)
        stats_service.append_video_stats("c.mp4", 60.0, 500.0, {}, "SHORT", False)
        s = stats_service.get_summary(days=14)
        self.assertEqual(s["videos"], 2)  # chỉ video thành công
        self.assertEqual(s["errors"], 1)
        self.assertEqual(s["source_minutes"], 3.0)
        self.assertEqual(s["per_step_median_s"]["2"], 150.0)
        self.assertGreater(s["avg_total_s"], 0)
        self.assertEqual(len(s["per_day"]), 1)
        self.assertIsNotNone(s["speed_ratio"])  # 1200s xử lý / 180s nguồn

    def test_empty_summary_is_safe(self):
        s = stats_service.get_summary(days=14)
        self.assertEqual(s["videos"], 0)
        self.assertEqual(s["per_step_median_s"], {})
        self.assertEqual(s["per_day"], [])


class TestScheduleAPI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.old_workspace = main.WORKSPACE
        main.WORKSPACE = str(Path(self.temporary.name))
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def tearDown(self):
        main.WORKSPACE = self.old_workspace
        self.temporary.cleanup()

    def test_invalid_time_rejected(self):
        res = self.client.post("/api/schedule", json={"time": "25:99"})
        self.assertEqual(res.status_code, 400)

    def test_schedule_lifecycle(self):
        res = self.client.post("/api/schedule", json={"time": "02:00", "video_mode": "AUTO"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["active"])
        self.assertTrue((Path(self.temporary.name) / "control" / "schedule.json").is_file())

        got = self.client.get("/api/schedule").json()
        self.assertTrue(got["active"])
        self.assertEqual(got["time"], "02:00")

        deleted = self.client.delete("/api/schedule")
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(deleted.json()["active"])
        self.assertFalse((Path(self.temporary.name) / "control" / "schedule.json").exists())

        # Xóa lần nữa vẫn an toàn
        self.assertEqual(self.client.delete("/api/schedule").status_code, 200)


if __name__ == "__main__":
    unittest.main()
