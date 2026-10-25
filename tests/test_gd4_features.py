# -*- coding: utf-8 -*-
"""Kiểm thử GĐ4: F16 tìm kiếm toàn cục, F18 xuất/nhập cấu hình, F19 kéo-thả vị trí hàng chờ."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main


class TestGlobalSearch(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input_dir = self.root / "phoi"
        self.output_dir = self.root / "banve"
        self.input_dir.mkdir()
        self.output_dir.mkdir()
        (self.input_dir / "video_test_abc.mp4").write_bytes(b"x")
        (self.input_dir / "clip ĐẸP.mp4").write_bytes(b"x")
        (self.output_dir / "Dubbed_video_test_abc.mp4").write_bytes(b"x")
        self._patches = [
            patch.object(main, "get_input_dir", return_value=self.input_dir),
            patch.object(main, "get_output_dir", return_value=self.output_dir),
            patch.object(main, "WORKSPACE", str(self.root)),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.temporary.cleanup()

    def test_short_query_rejected(self):
        res = self.client.get("/api/search", params={"q": "a"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["groups"], [])

    def test_search_finds_phoi_and_banve(self):
        res = self.client.get("/api/search", params={"q": "video_test"})
        groups = {g["id"]: g for g in res.json()["groups"]}
        self.assertIn("phoi", groups)
        self.assertIn("banve", groups)
        self.assertEqual(groups["phoi"]["items"][0]["title"], "video_test_abc.mp4")

    def test_search_folds_vietnamese_diacritics(self):
        res = self.client.get("/api/search", params={"q": "dep"})
        groups = {g["id"]: g for g in res.json()["groups"]}
        self.assertIn("phoi", groups)
        self.assertEqual(groups["phoi"]["items"][0]["title"], "clip ĐẸP.mp4")


class TestConfigExportImport(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def test_export_returns_bundle(self):
        res = self.client.get("/api/config/export")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["format"], "toolv1-dashboard-config")
        self.assertEqual(data["version"], 1)
        # Các phần tối thiểu phải có mặt (đọc được từ máy thật)
        self.assertIn("audio_settings", data)
        self.assertIn("worker_settings", data)

    def test_import_rejects_wrong_format(self):
        res = self.client.post("/api/config/import", json={"data": {"foo": 1}})
        self.assertEqual(res.status_code, 400)

    def test_import_applies_glossary_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(main, "WORKSPACE", str(tmp_path)):
                payload = {
                    "data": {
                        "format": "toolv1-dashboard-config",
                        "version": 1,
                        "glossary": {"thuật-ngữ тест": "giá trị mới"},
                    },
                    "mode": "merge",
                }
                res = self.client.post("/api/config/import", json=payload)
                self.assertEqual(res.status_code, 200)
                body = res.json()
                self.assertEqual(body["status"], "ok")
                self.assertTrue(any("glossary" in a for a in body["applied"]))
                from ai.translation import load_custom_glossary
                self.assertEqual(load_custom_glossary(str(tmp_path)).get("thuật-ngữ тест"), "giá trị mới")


class TestQueuePositionMove(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        qdir = self.root / "bot_system"
        qdir.mkdir(parents=True)
        (qdir / "telegram_queue.json").write_text(json.dumps({
            "pid": 123, "items": [
                {"name": "a.mp4"}, {"name": "b.mp4"}, {"name": "c.mp4"},
            ]
        }), encoding="utf-8")
        self._patch = patch.object(main, "WORKSPACE", str(self.root))
        self._patch.start()
        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def tearDown(self):
        self._patch.stop()
        self.temporary.cleanup()

    def test_move_to_position(self):
        res = self.client.post("/api/queue/move", json={"index": 0, "action": "position", "to": 2})
        self.assertEqual(res.status_code, 200)
        names = [i["name"] for i in res.json()["items"]]
        self.assertEqual(names, ["b.mp4", "c.mp4", "a.mp4"])
        self.assertEqual([i["position"] for i in res.json()["items"]], [1, 2, 3])

    def test_move_to_same_position_unchanged(self):
        res = self.client.post("/api/queue/move", json={"index": 1, "action": "position", "to": 1})
        self.assertEqual(res.json()["status"], "unchanged")


if __name__ == "__main__":
    unittest.main()
