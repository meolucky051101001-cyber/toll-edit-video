"""Telegram Cloud dispatch regression tests, with all work/provider calls mocked."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests import test_v1_telegram_orchestrator as fixtures


class TelegramCloudDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.TelegramV1Tests.setUp(self)
        patcher = patch.dict("sys.modules", {"batch_processor": SimpleNamespace(BatchStopRequested=type("BatchStopRequested", (Exception,), {}))})
        patcher.start()
        self.addCleanup(patcher.stop)
    async def test_local_job_uses_cloud_pipeline_and_keeps_source_fingerprint(self):
        from backend import telegram_bot as bot
        job = dict(self.job, type="local_file", url="", filename="input.mp4",
                   video_path=str(self.source), force_cloud=True, cloud_gpu="L4", voice="fixture")
        result = {"status": "success", "final_video": str(self.root / "final.mp4"), "mode": "cloud"}
        with patch.object(self.media, "probe_downloaded_video"), \
             patch.object(self.checkpoint, "fingerprint", return_value="source-hash"), \
             patch.object(bot, "run_pipeline_cloud_for_telegram", AsyncMock(return_value=result)) as cloud, \
             patch.object(bot, "run_pipeline_v1_for_telegram", AsyncMock()) as local, \
             patch.object(bot.job_tracker, "start_video"), \
             patch.object(bot, "safe_edit_status", AsyncMock()):
            await bot._process_telegram_job(job)
        cloud.assert_awaited_once()
        local.assert_not_awaited()
        assert job["resume_state"]["source_sha256"] == "source-hash"

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
            await bot.video_worker()
        dispatched.assert_awaited_once_with(job)
        assert bot.global_queue._unfinished_tasks == 0

    async def test_local_cloud_job_survives_snapshot_and_restart(self):
        from backend import telegram_bot as bot, telegram_queue_monitor as monitor
        job = dict(self.job, type="local_file", url="", video_path=str(self.source),
                   filename="input.mp4", force_cloud=True, cloud_gpu="L4", voice="fixture")
        await bot.global_queue.put(job)
        restored = monitor.MonitoredQueue()
        with patch.object(bot, "global_queue", restored), \
             patch.object(bot, "worker_task", SimpleNamespace(done=lambda: False)):
            assert await bot.enqueue_pending_queue_jobs() == 1
        recovered = await restored.get()
        for field in ("video_path", "force_cloud", "cloud_gpu", "voice"):
            assert recovered[field] == job[field]
        restored.task_done()

    async def test_cloud_stop_failure_is_reported_to_telegram(self):
        import sys
        from backend import telegram_bot as bot
        store = Mock()
        store.list_jobs.return_value = [SimpleNamespace(job_id="owned-cloud-job")]
        bridge = SimpleNamespace(stop_all_sessions=Mock(return_value=(False, "GPU vẫn chạy")))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.dict(sys.modules, {
            "cloud.colab_bridge": bridge,
            "cloud.cloud_job_store": SimpleNamespace(CloudJobStore=lambda: store),
        }):
            await bot.cmd_cloud(update, SimpleNamespace(args=["stop"]))
        store.set_rollout.assert_called_once_with(arm=False)
        store.request_cancel.assert_called_once_with("owned-cloud-job", reason="Telegram yêu cầu dừng Cloud")
        store.record_cleanup_result.assert_not_called()
        assert "GPU vẫn chạy" in update.message.reply_text.call_args.args[0]
        assert "an toàn" not in update.message.reply_text.call_args.args[0]

    async def test_cloud_stop_timeout_reports_pending_without_claiming_cleanup(self):
        import sys
        from backend import telegram_bot as bot
        store = Mock()
        store.list_jobs.return_value = [SimpleNamespace(job_id="owned-cloud-job")]
        update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        action = Mock(return_value={"completed": False, "timed_out": True})
        with patch.dict(sys.modules, {
            "cloud.cloud_contract": SimpleNamespace(run_provider_action_nonblocking=action),
            "cloud.colab_bridge": SimpleNamespace(stop_all_sessions=Mock()),
            "cloud.cloud_job_store": SimpleNamespace(CloudJobStore=lambda: store),
        }):
            await bot.cmd_cloud(update, SimpleNamespace(args=["stop"]))
        store.set_rollout.assert_called_once_with(arm=False)
        store.request_cancel.assert_called_once_with("owned-cloud-job", reason="Telegram yêu cầu dừng Cloud")
        store.record_cleanup_result.assert_not_called()
        assert "phản hồi chậm" in update.message.reply_text.call_args.args[0]
        assert "an toàn" not in update.message.reply_text.call_args.args[0]
