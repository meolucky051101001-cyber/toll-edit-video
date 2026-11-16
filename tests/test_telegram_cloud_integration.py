"""
tests/test_telegram_cloud_integration.py - Kiểm thử tích hợp Cloud GPU cho Telegram Bot.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend import telegram_bot as bot
from backend import telegram_queue_monitor as monitor
from backend.cloud import cloud_job_store
from backend.v1_feature_flags import get_feature_flags, set_feature_flags


class TelegramCloudIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cloud_store = cloud_job_store.CloudJobStore(db_path=self.root / "cloud.db")
        p = patch.object(cloud_job_store, "CloudJobStore", return_value=self.cloud_store)
        p.start()
        self.addCleanup(p.stop)

        # Mock WORKSPACE and OUTPUT_DIR
        for target, value in [
            ("WORKSPACE", str(self.root)),
            ("OUTPUT_DIR", str(self.root / "delivery")),
            ("global_queue", monitor.MonitoredQueue()),
        ]:
            p = patch.object(bot, target, value)
            p.start()
            self.addCleanup(p.stop)

        p = patch.object(monitor, "SNAPSHOT", self.root / "telegram_queue.json")
        p.start()
        self.addCleanup(p.stop)

        state = SimpleNamespace(stop_requested=False)
        p = patch.object(bot, "shared_state", state)
        p.start()
        self.addCleanup(p.stop)

    async def test_cmd_cloud_toggle(self):
        """Kiểm tra bật/tắt Cloud GPU qua lệnh /cloud on và /cloud off."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        # 1. /cloud on
        context = MagicMock()
        context.args = ["on"]
        await bot.cmd_cloud(update, context)
        flags = get_feature_flags(self.root)
        self.assertFalse(flags.get("V1_CLOUD_MODE"))
        update.message.reply_text.assert_called()
        self.assertIn("Mở một lượt Cloud", update.message.reply_text.call_args[0][0])
        self.assertEqual(self.cloud_store.get_rollout()["phase"], "paused")

        # 2. /cloud off
        self.cloud_store.set_rollout(arm=True)
        context.args = ["off"]
        await bot.cmd_cloud(update, context)
        flags = get_feature_flags(self.root)
        self.assertFalse(flags.get("V1_CLOUD_MODE"))
        self.assertIn("Đã khóa nhận và khởi chạy job Cloud", update.message.reply_text.call_args[0][0])
        self.assertEqual(self.cloud_store.get_rollout()["phase"], "paused")

    async def test_cmd_cloud_gpu_switch(self):
        """Kiểm tra chuyển đổi GPU Cloud qua /cloud T4 hoặc /cloud L4."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.args = ["L4"]
        await bot.cmd_cloud(update, context)
        flags = get_feature_flags(self.root)
        self.assertEqual(flags.get("V1_CLOUD_GPU"), "L4")
        self.assertIn("NVIDIA L4", update.message.reply_text.call_args[0][0])

        context.args = ["T4"]
        await bot.cmd_cloud(update, context)
        flags = get_feature_flags(self.root)
        self.assertEqual(flags.get("V1_CLOUD_GPU"), "T4")
        self.assertIn("NVIDIA T4", update.message.reply_text.call_args[0][0])

    async def test_cmd_cloud_enqueue_url(self):
        """Kiểm tra đưa link video vào hàng đợi qua /cloud <url>."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        test_url = "https://v.douyin.com/iABCxyz/"
        context.args = [test_url]
        await bot.cmd_cloud(update, context)
        self.assertTrue(bot.global_queue.empty())
        permit = self.cloud_store.set_rollout(arm=True)["permit_id"]

        def close_unscheduled_worker(coroutine):
            coroutine.close()
            return MagicMock()

        with patch.object(bot, "worker_task", None), patch(
            "asyncio.create_task", side_effect=close_unscheduled_worker
        ) as create_task:
            await bot.cmd_cloud(update, context)
            create_task.assert_called_once()

        self.assertEqual(bot.global_queue.qsize(), 1)
        queued_job = await bot.global_queue.get()
        self.assertTrue(queued_job.get("force_cloud"))
        self.assertEqual(queued_job["cloud_rollout_permit"], permit)
        self.assertEqual(queued_job.get("url"), test_url)
        self.assertIn("ĐÃ THÊM VÀO HÀNG ĐỢI CLOUD GPU", update.message.reply_text.call_args[0][0])

    async def test_cmd_cloud_status(self):
        """Kiểm tra tra cứu trạng thái Cloud qua /cloud_status."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        mock_usage = {"balance": 199.88, "usage_rate": 0.0, "active_assignments": 0, "ok": True}
        with patch("backend.cloud.colab_bridge.get_usage", return_value=mock_usage):
            await bot.cmd_cloud_status(update, context)

        update.message.reply_text.assert_called()
        reply_msg = update.message.reply_text.call_args[0][0]
        self.assertIn("199.88 CU", reply_msg)
        self.assertIn("TRẠNG THÁI CLOUD GPU", reply_msg)

    async def test_cmd_cloud_budget(self):
        """Kiểm tra xem và cập nhật hạn mức CU qua /cloud budget <val>."""
        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        # 1. Tra cứu hạn mức hiện tại
        context.args = ["budget"]
        await bot.cmd_cloud(update, context)
        update.message.reply_text.assert_called()
        self.assertIn("HẠN MỨC SỬ DỤNG CU HÔM NAY", update.message.reply_text.call_args[0][0])

        # 2. Cài đặt hạn mức mới
        context.args = ["budget", "15.5"]
        await bot.cmd_cloud(update, context)
        reply_msg = update.message.reply_text.call_args[0][0]
        self.assertIn("ĐÃ CẬP NHẬT HẠN MỨC CU HÔM NAY", reply_msg)
        self.assertIn("15.50 CU/ngày", reply_msg)
