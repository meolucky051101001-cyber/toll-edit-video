import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

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

        # 2. Populate queue file
        snap_file = self.workspace / "telegram_queue.json"
        payload = {
            "items": [
                {"position": 1, "url": "https://example.com/v1", "chat_id": 999, "name": "Video 1"},
                {"position": 2, "url": "https://example.com/v2", "chat_id": 999, "name": "Video 2"}
            ],
            "pid": os.getpid(),
            "updated_at": time.time()
        }
        snap_file.write_text(json.dumps(payload), encoding="utf-8")

        # 3. Process queue
        with patch("job_tracker._pid_is_running", return_value=True):
            res_proc = self.client.post("/api/queue/process")
            self.assertEqual(res_proc.status_code, 200)
            self.assertEqual(res_proc.json()["status"], "processing")
            self.assertEqual(res_proc.json()["count"], 2)

            trigger_flag = self.workspace / "telegram_queue_trigger.flag"
            self.assertTrue(trigger_flag.exists())

        # 4. Clear queue via API
        res_clear = self.client.post("/api/queue/clear")
        self.assertEqual(res_clear.status_code, 200)
        self.assertEqual(res_clear.json()["status"], "cleared")

        clear_flag = self.workspace / "telegram_queue_clear.flag"
        self.assertTrue(clear_flag.exists())

        data_cleared = json.loads(snap_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data_cleared.get("items", [])), 0)


if __name__ == "__main__":
    unittest.main()
