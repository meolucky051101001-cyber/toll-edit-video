import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor


class TelegramBulkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.workspace = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.snapshot = self.workspace / "telegram_queue.json"
        self.idle_worker = SimpleNamespace(done=lambda: False)
        self.queue = monitor.MonitoredQueue()
        for name, value in {
            "WORKSPACE": str(self.workspace),
            "OUTPUT_DIR": str(self.workspace / "output"),
            "global_queue": self.queue,
            "queue_counter": 0,
            "worker_task": self.idle_worker,
            "_network_retry_jobs": {},
        }.items():
            stack.enter_context(patch.object(bot, name, value))
        stack.enter_context(patch.object(monitor, "SNAPSHOT", self.snapshot))
        stack.enter_context(patch.object(bot, "_ensure_network_retry_scheduler"))

    def queue_snapshot(self):
        return json.loads(self.snapshot.read_text(encoding="utf-8"))

    def update(self, **fields):
        return SimpleNamespace(message=SimpleNamespace(chat_id=123, reply_text=AsyncMock(), **fields))

    async def test_100_links_persist_together_and_resume_in_order(self):
        urls = [f"https://www.douyin.com/video/{index:06d}" for index in range(100)]
        message = "\n".join(urls + [urls[18]])
        self.assertLessEqual(len(message), 4096)
        update = self.update(text=message)
        with patch.object(monitor, "atomic_write_json", wraps=monitor.atomic_write_json) as persist:
            await bot.handle_message(update, None)
        saved = self.queue_snapshot()["items"]
        self.assertEqual([item["url"] for item in saved], urls)
        ids = [item["job_id"] for item in saved]
        self.assertEqual(len(set(ids)), 100)
        self.assertEqual(persist.call_count, 1)
        self.assertIn("Đã thêm 100 link", update.message.reply_text.call_args.args[0])

        await self.queue.get()
        self.assertEqual(self.queue_snapshot()["active_job"]["job_id"], ids[0])
        recovered = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", recovered), patch.object(bot, "queue_counter", 0):
            self.assertEqual(await bot.enqueue_pending_queue_jobs(), 100)
            self.assertEqual(await bot.enqueue_pending_queue_jobs(), 0)
        self.assertEqual([item["job_id"] for item in self.queue_snapshot()["items"]], ids)

        received = []
        state = {}
        job_ids = iter(ids)

        def start_batch(*_args, **_kwargs):
            state.update(video_status="processing", active=True)
            return next(job_ids)

        def fail_batch(*_args, **_kwargs):
            state.update(video_status="failed", active=False)

        async def process(_update, _context, url, index, *, queue_job, **_kwargs):
            received.append((url, index, queue_job["job_id"]))
            if index == 34:
                raise RuntimeError("one failed video")
            state.update(video_status="completed", active=True)
            return {"status": "success"}

        with ExitStack() as stack:
            stack.enter_context(patch.object(bot, "global_queue", recovered))
            stack.enter_context(patch.object(bot, "process_single_url", side_effect=process))
            tracker = bot.job_tracker
            stack.enter_context(patch.object(tracker, "start_batch", side_effect=start_batch))
            stack.enter_context(patch.object(tracker, "get_status", return_value=state))
            finish = stack.enter_context(patch.object(tracker, "finish_batch"))
            fail = stack.enter_context(patch.object(tracker, "fail_batch", side_effect=fail_batch))
            release = stack.enter_context(patch.object(tracker, "release_batch"))
            stack.enter_context(patch("gc.collect"))
            stack.enter_context(patch("torch.cuda.is_available", return_value=False))
            worker = asyncio.create_task(bot.video_worker())
            try:
                await asyncio.wait_for(recovered.join(), timeout=60)
            finally:
                worker.cancel()
                await worker
        self.assertEqual(received, list(zip(urls, range(1, 101), ids)))
        fail.assert_called_once_with("one failed video", job_id=ids[33])
        self.assertEqual(finish.call_count, 99)
        self.assertEqual(release.call_count, 100)
        final = self.queue_snapshot()
        self.assertEqual(final["items"], [])
        self.assertIsNone(final["active_job"])

    async def test_64_uploads_keep_positions_when_first_video_is_active(self):
        def video_update(index):
            file = SimpleNamespace(file_id=f"file_{index}", file_name=f"video_{index}.mp4")
            return self.update(video=file if index % 2 == 0 else None,
                               document=file if index % 2 else None)

        await bot.handle_video(video_update(0), None)
        first = await self.queue.get()
        await asyncio.gather(*(bot.handle_video(video_update(i), None) for i in range(1, 64)))
        saved = self.queue_snapshot()
        self.assertEqual(saved["active_job"]["job_id"], first["job_id"])
        items = saved["items"]
        self.assertEqual([item["file_id"] for item in items], [f"file_{i}" for i in range(1, 64)])
        self.assertEqual([item["telegram_position"] for item in items], list(range(2, 65)))
        self.assertEqual(len({item["job_id"] for item in items}), 63)

    async def test_new_links_follow_a_restored_network_retry(self):
        retry = bot.prepare_telegram_job({
            "type": "url", "url": "https://www.douyin.com/video/retry",
        })
        record = bot.schedule_network_retry(retry)
        self.assertIsNotNone(record)
        bot._network_retry_jobs.clear()
        self.assertEqual(await bot.process_due_network_retries(record["next_attempt_at"]), 1)
        await self.queue.get()

        update = self.update(text="https://www.douyin.com/video/new")
        await bot.handle_message(update, None)

        saved = self.queue_snapshot()
        self.assertEqual(saved["active_job"]["job_id"], retry["job_id"])
        self.assertEqual(saved["active_job"]["network_retry_count"], 1)
        self.assertEqual(saved["items"][0]["telegram_position"], 2)
