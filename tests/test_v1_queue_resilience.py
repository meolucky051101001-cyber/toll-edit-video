import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import main
import telegram_queue_monitor
from telegram_queue_monitor import MonitoredQueue


class TestQueueResilience(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        self.old_workspace_main = main.WORKSPACE
        self.old_snapshot = telegram_queue_monitor.SNAPSHOT

        main.WORKSPACE = str(self.workspace)
        telegram_queue_monitor.SNAPSHOT = self.workspace / "telegram_queue.json"

        self.client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})

    def tearDown(self):
        main.WORKSPACE = self.old_workspace_main
        telegram_queue_monitor.SNAPSHOT = self.old_snapshot
        self.temp_dir.cleanup()

    def test_queue_publish_and_clear(self):
        q = MonitoredQueue()
        # Put item with rich metadata
        q.put_nowait({
            "type": "url",
            "pos": 1,
            "url": "https://example.com/video1",
            "chat_id": 12345,
            "filename": "video1.mp4"
        })
        snap_file = self.workspace / "telegram_queue.json"
        self.assertTrue(snap_file.is_file())

        data = json.loads(snap_file.read_text(encoding="utf-8"))
        items = data.get("items", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["url"], "https://example.com/video1")
        self.assertEqual(items[0]["chat_id"], 12345)

        # Clear queue
        q.clear()
        self.assertTrue(q.empty())
        data_after = json.loads(snap_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data_after.get("items", [])), 0)

    def test_api_queue_endpoints(self):
        # 1. When empty
        res_empty = self.client.post("/api/queue/process")
        self.assertEqual(res_empty.status_code, 200)
        self.assertEqual(res_empty.json()["status"], "empty")

        owner = MonitoredQueue()
        owner.put_nowait({"job_id": "one", "hold": {"kind": "manual"}})
        owner.put_nowait({"job_id": "two"})
        payload = {"target_job_ids": ["one"], "expected_revision": owner.revision}
        process = self.client.post("/api/queue/process", json=payload)
        self.assertEqual(process.status_code, 202, process.text)
        active = owner.get_nowait()
        self.assertEqual(active["job_id"], "one")
        payload = {"target_job_ids": ["one", "two"], "expected_revision": owner.revision}
        cleared = self.client.post("/api/queue/clear", json=payload)
        self.assertEqual(cleared.status_code, 202, cleared.text)
        self.assertEqual(owner.qsize(), 1)
        owner.apply_commands()
        operation = self.client.get("/api/queue/operations/" + cleared.json()["operation_id"]).json()
        self.assertEqual(operation["result"], {"one": "too_late", "two": "removed"})
        self.assertEqual(owner.qsize(), 0)
        self.assertEqual(owner.active_job["job_id"], "one")


if __name__ == "__main__":
    unittest.main()
