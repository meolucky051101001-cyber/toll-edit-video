import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor
from backend.pipeline_v2.download_validation import DownloadValidationError
from backend.v1_retry_policy import is_transient_network_error


class TelegramNetworkRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "bot_system").mkdir()
        self.source = self.root / "downloads" / "input.mp4"
        self.source.parent.mkdir()
        self.source.write_bytes(b"source fixture")
        self.job = {
            "type": "url", "url": "https://example.com/video", "pos": 1,
            "job_id": "telegram_network_retry", "voice_mode": "manual", "retry_count": 1,
            "job_overrides": {"voice_mode": "manual", "voice_config": {"manual_voice": {"id": "chosen"}},
                              "feature_flags": {"V1_VIDEO_MODE": "SHORT"}, "bgm_volume_db": -17},
            "resume_state": {"source_path": str(self.source), "source_sha256": "source-hash"},
        }
        for target, value in [
            ("WORKSPACE", str(self.root)), ("OUTPUT_DIR", str(self.root / "delivery")),
            ("global_queue", monitor.MonitoredQueue()), ("_network_retry_task", None),
        ]:
            p = patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)
        bot._network_retry_jobs.clear()
        self.addCleanup(bot._network_retry_jobs.clear)
        p = patch.object(monitor, "SNAPSHOT", self.root / "telegram_queue.json")
        p.start()
        self.addCleanup(p.stop)
        state = SimpleNamespace(stop_requested=False)
        p = patch.object(bot, "shared_state", state)
        p.start()
        self.addCleanup(p.stop)
        p = patch.dict("sys.modules", {"shared_state": state})
        p.start()
        self.addCleanup(p.stop)
        batch_module = ModuleType("batch_processor")
        batch_module.BatchStopRequested = type("BatchStopRequested", (Exception,), {})
        p = patch.dict("sys.modules", {"batch_processor": batch_module})
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bot, "_ensure_network_retry_scheduler", MagicMock())
        p.start()
        self.addCleanup(p.stop)

    def _retry_state(self):
        return json.loads(bot.network_retry_state_path().read_text(encoding="utf-8"))

    def _make_due(self):
        path = bot.network_retry_state_path()
        state = json.loads(path.read_text(encoding="utf-8"))
        for record in state["jobs"].values():
            record["next_attempt_at"] = 0
        path.write_text(json.dumps(state), encoding="utf-8")

    async def _run_failing_pipeline(self, error):
        from pipeline_v2 import download_validation

        with patch.object(download_validation, "probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=error), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error") as set_error, \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock) as status:
            result = await bot._process_telegram_job(copy.deepcopy(self.job))
        return result, set_error, status

    async def test_connect_error_is_persisted_with_backoff_and_not_failed(self):
        result, set_error, status = await self._run_failing_pipeline(
            httpx.ConnectError("All connection attempts failed")
        )
        self.assertEqual(result["status"], "retry_scheduled")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_not_called()
        state = self._retry_state()
        record = state["jobs"]["telegram_network_retry"]
        self.assertEqual(record["attempt"], 1)
        self.assertEqual(record["delay_seconds"], 60)
        self.assertAlmostEqual(record["next_attempt_at"] - time.time(), 60, delta=5)
        self.assertEqual(record["job"]["job_id"], "telegram_network_retry")
        self.assertEqual(record["job"]["resume_state"]["source_sha256"], "source-hash")
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Đang chờ mạng" in message and "thử lại" in message for message in messages))

    async def test_backoff_is_one_five_fifteen_minutes_and_stops_after_three(self):
        expected = (60, 300, 900)
        for attempt, delay in enumerate(expected, 1):
            scheduled = bot.schedule_network_retry(
                dict(copy.deepcopy(self.job), network_retry_count=attempt - 1)
            )
            self.assertEqual(scheduled["attempt"], attempt)
            self.assertEqual(scheduled["delay_seconds"], delay)
            self.assertAlmostEqual(
                self._retry_state()["jobs"]["telegram_network_retry"]["next_attempt_at"] - time.time(),
                delay, delta=5,
            )
        self.assertIsNone(
            bot.schedule_network_retry(dict(copy.deepcopy(self.job), network_retry_count=3))
        )
        self.assertEqual(len(self._retry_state()["jobs"]), 1)

    async def test_due_retry_is_enqueued_once_with_same_job_id(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        self._make_due()
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 1)
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 0)
        self.assertEqual(bot.global_queue.qsize(), 1)
        queued = await bot.global_queue.get()
        self.assertEqual(queued["job_id"], "telegram_network_retry")
        self.assertEqual(queued["network_retry_count"], 1)
        self.assertEqual(queued["job_overrides"], self.job["job_overrides"])
        self.assertEqual(queued["resume_state"], self.job["resume_state"])
        self.assertEqual(self._retry_state()["jobs"], {})
        bot.global_queue.task_done()

    async def test_job_already_in_queue_is_not_duplicated(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        await bot.global_queue.put(dict(copy.deepcopy(self.job), network_retry_count=1))
        self._make_due()
        self.assertEqual(await bot.process_due_network_retries(now=time.time()), 0)
        self.assertEqual(bot.global_queue.qsize(), 1)
        self.assertEqual(self._retry_state()["jobs"], {})
        await bot.global_queue.get()
        bot.global_queue.task_done()

    async def test_persisted_retry_survives_restart_with_identity_settings(self):
        bot.schedule_network_retry(copy.deepcopy(self.job))
        self._make_due()
        bot._network_retry_jobs.clear()
        restarted = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", restarted):
            self.assertEqual(await bot.process_due_network_retries(now=time.time()), 1)
            restored = await restarted.get()
        self.assertEqual(restored["job_id"], "telegram_network_retry")
        self.assertEqual(restored["network_retry_count"], 1)
        self.assertEqual(restored["resume_state"], self.job["resume_state"])
        self.assertEqual(restored["job_overrides"], self.job["job_overrides"])
        restarted.task_done()

    async def test_network_retries_are_capped_and_then_fail(self):
        job = copy.deepcopy(self.job)
        job["network_retry_count"] = 3
        from pipeline_v2 import download_validation

        with patch.object(download_validation, "probe_downloaded_video"), \
             patch("v1_checkpoint.fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_v1_for_telegram", side_effect=httpx.ConnectError("offline")), \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot.job_tracker, "set_error") as set_error, \
             patch.object(bot, "safe_edit_status", new_callable=AsyncMock) as status:
            result = await bot._process_telegram_job(job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_called_once()
        path = bot.network_retry_state_path()
        retried = json.loads(path.read_text(encoding="utf-8"))["jobs"] if path.exists() else {}
        self.assertFalse(retried)
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Không hoàn tất video" in message for message in messages))

    async def test_corrupt_video_fails_immediately_without_retry(self):
        result, set_error, status = await self._run_failing_pipeline(
            DownloadValidationError("ffprobe rejected the downloaded container")
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(bot.global_queue.qsize(), 0)
        set_error.assert_called_once()
        self.assertFalse(bot.network_retry_state_path().exists())
        messages = [call.args[1] for call in status.call_args_list if len(call.args) > 1]
        self.assertTrue(any("Không hoàn tất video" in message for message in messages))

    def test_downloader_connection_strings_are_network_errors(self):
        self.assertTrue(is_transient_network_error(httpx.ConnectError("refused")))
        self.assertTrue(is_transient_network_error(
            RuntimeError("Không thể kết nối đến máy chủ Tiểu Hồng Thư (Hết thời gian chờ hoặc bị chặn)")
        ))
        self.assertTrue(is_transient_network_error(
            RuntimeError("Bóc tách Douyin thất bại: Direct: Kết nối API Douyin thất bại: ConnectError")
        ))
        wrapped = RuntimeError("worker failed")
        wrapped.__cause__ = ConnectionError("getaddrinfo failed")
        self.assertTrue(is_transient_network_error(wrapped))
        self.assertFalse(is_transient_network_error(RuntimeError("Downloaded file is missing or empty")))
        self.assertFalse(is_transient_network_error(
            RuntimeError("Bài viết trên Tiểu Hồng Thư này đã bị tác giả xóa (Trang không tồn tại)")
        ))
