import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

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

        # 2. Populate a real consumer queue, retaining one active job.
        snap_file = self.workspace / "telegram_queue.json"
        queue = MonitoredQueue()
        queue.put_nowait({"job_id": "active", "type": "url", "url": "https://example.com/active"})
        queue.get_nowait()
        for index in (1, 2):
            queue.put_nowait({"job_id": f"pending_{index}", "type": "url",
                             "url": f"https://example.com/v{index}", "chat_id": 999})

        # 3. Process queue
        with patch("job_tracker._pid_is_running", return_value=True):
            res_proc = self.client.post("/api/queue/process")
            self.assertEqual(res_proc.status_code, 200)
            self.assertEqual(res_proc.json()["status"], "processing")
            self.assertEqual(res_proc.json()["count"], 2)

            trigger_flag = self.workspace / "telegram_queue_trigger.flag"
            self.assertTrue(trigger_flag.exists())

        # 4. Clear queue via API
        before_clear = snap_file.read_bytes()
        res_clear = self.client.post("/api/queue/clear")
        self.assertEqual(res_clear.status_code, 200)
        self.assertEqual(res_clear.json()["status"], "pending")
        self.assertEqual(snap_file.read_bytes(), before_clear)

        clear_flag = self.workspace / "telegram_queue_clear.flag"
        self.assertTrue(clear_flag.exists())

        # 5. Only the bot consumes the command and publishes confirmation.
        import telegram_bot as bot
        async def end_iteration(*args):
            raise asyncio.CancelledError
        with patch.object(bot, "WORKSPACE", str(self.workspace)), \
             patch.object(bot, "global_queue", queue), \
             patch.object(bot, "worker_task", SimpleNamespace(done=lambda: False)), \
             patch.object(bot, "clear_network_retries"), \
             patch.object(bot, "enqueue_pending_queue_jobs", AsyncMock(return_value=0)), \
             patch.object(bot.asyncio, "sleep", end_iteration):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(bot.queue_signal_watcher())
        self.assertFalse(clear_flag.exists())
        data_cleared = json.loads(snap_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data_cleared.get("items", [])), 0)
        self.assertEqual(data_cleared["active_job"]["job_id"], "active")
        self.assertEqual(queue._unfinished_tasks, 1)
        tombstones = [json.loads(path.read_text())["job_id"]
                      for path in (self.workspace / "queue_commands").glob("*.json")]
        self.assertEqual(set(tombstones), {"pending_1", "pending_2"})


if __name__ == "__main__":
    unittest.main()
