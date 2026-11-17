"""Telegram Cloud dispatch regression tests, with all work/provider calls mocked."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import test_v1_telegram_orchestrator as fixtures


class TelegramCloudDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TelegramV1Tests.setUp(self)
        from batch_plan_service import fingerprint
        self.source_sha = fingerprint(self.source)["sha256"]
        self.job["resume_state"]["source_sha256"] = self.source_sha
        patcher = patch.dict("sys.modules", {"batch_processor": SimpleNamespace(BatchStopRequested=type("BatchStopRequested", (Exception,), {}))})
        patcher.start()
        self.addCleanup(patcher.stop)
    async def test_local_job_uses_cloud_pipeline_and_keeps_source_fingerprint(self):
        from backend import telegram_bot as bot
        job = dict(self.job, type="local_file", url="", filename="input.mp4",
                   video_path=str(self.source), force_cloud=True, cloud_gpu="L4", voice="fixture")
        result = {"status": "success", "final_video": str(self.root / "final.mp4"), "mode": "cloud"}
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(bot, "run_pipeline_cloud_for_telegram", AsyncMock(return_value=result)) as cloud, \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock()) as local, \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            await asyncio.wait_for(bot._process_telegram_job(job), timeout=5)
        cloud.assert_awaited_once()
        local.assert_not_awaited()
        assert job["resume_state"]["source_sha256"] == self.source_sha

    async def test_local_file_is_dispatched_by_worker(self):
        from backend import telegram_bot as bot
        job = dict(self.job, type="local_file", video_path=str(self.source), force_cloud=True)
        await bot.global_queue.put(job)
        dispatched = AsyncMock(side_effect=asyncio.CancelledError)
        with patch.object(bot, "_process_telegram_job", dispatched), \
             patch.object(bot.job_tracker, "start_batch", return_value="test-job"), \
             patch.object(bot.job_tracker, "mark_stopped"), \
             patch.object(bot.job_tracker, "get_status", return_value={"active": False}), \
             patch.object(bot.job_tracker, "release_batch"), \
             patch.dict("sys.modules", {"torch": SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))}):
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(bot.video_worker(), timeout=3)
        dispatched.assert_awaited_once_with(job)
        assert bot.global_queue._unfinished_tasks == 1
        assert bot.global_queue.active_jobs == [job]
        from backend import telegram_queue_monitor as monitor
        restored = monitor.MonitoredQueue()
        assert restored.restore()["recovery_state"] == "needs_review"
        assert restored.active_job["resume_state"] == job["resume_state"]

    async def test_local_cloud_job_survives_snapshot_and_restart(self):
        from backend import telegram_bot as bot, telegram_queue_monitor as monitor
        job = dict(self.job, type="local_file", url="", video_path=str(self.source),
                   filename="input.mp4", force_cloud=True, cloud_gpu="L4", voice="fixture")
        await bot.global_queue.put(job)
        restored = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", restored), \
             patch.object(bot, "_ensure_video_worker"), \
             patch.object(bot, "_ensure_network_retry_scheduler"):
            assert await asyncio.wait_for(bot.enqueue_pending_queue_jobs(), timeout=3) == 1
        recovered = await asyncio.wait_for(restored.get(), timeout=2)
        for field in ("video_path", "force_cloud", "cloud_gpu", "voice"):
            assert recovered[field] == job[field]
        assert recovered["resume_state"]["source_sha256"] == self.source_sha
        restored.task_done(recovered["job_id"])
        await asyncio.wait_for(restored.join(), timeout=2)

    async def test_cloud_stop_failure_is_reported_to_telegram(self):
        import sys
        from backend import telegram_bot as bot
        store = Mock()
        store.list_jobs.return_value = [SimpleNamespace(job_id="owned-job", state="processing")]
        store.request_cancel.side_effect = OSError("cancel intent write failed")
        bridge = SimpleNamespace(stop_all_sessions=Mock(return_value=(False, "GPU vẫn chạy")))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.dict(sys.modules, {
            "cloud.colab_bridge": bridge,
            "cloud.cloud_job_store": SimpleNamespace(CloudJobStore=lambda: store),
        }):
            await asyncio.wait_for(bot.cmd_cloud(update, SimpleNamespace(args=["stop"])), timeout=3)
        store.request_cancel.assert_called_once_with("owned-job")
        bridge.stop_all_sessions.assert_not_called()
        store.cancel_all.assert_not_called()
        store.acknowledge_all_sessions_stopped.assert_not_called()
        assert "Cloud" in update.message.reply_text.call_args.args[0]
        assert "an toàn" not in update.message.reply_text.call_args.args[0]
