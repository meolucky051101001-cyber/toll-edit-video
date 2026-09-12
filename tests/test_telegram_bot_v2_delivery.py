"""Tests for Telegram Bot V2 video delivery via send_video_safely."""

import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from backend.telegram_bot import process_v2_telegram_job, TelegramJobPaths


class TestTelegramBotV2Delivery(unittest.IsolatedAsyncioTestCase):
    async def test_process_v2_telegram_job_calls_send_video_safely_when_context_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video_path = root / "input.mp4"
            video_path.write_bytes(b"input video content")

            paths = TelegramJobPaths.create(str(root / "workspace"), str(root / "output"), "test_job")
            paths.prepare_directories()

            # Mock pipeline runner so it produces the final video
            async def fake_run_pipeline(*args, **kwargs):
                paths.final_video.write_bytes(b"dummy final video")

            mock_send_video_safely = AsyncMock()
            mock_status = AsyncMock()
            mock_context = SimpleNamespace(bot=AsyncMock())

            with patch("backend.telegram_bot.run_pipeline_v2_for_telegram", side_effect=fake_run_pipeline), \
                 patch("backend.telegram_bot.send_video_safely", mock_send_video_safely):
                await process_v2_telegram_job(
                    str(video_path),
                    paths,
                    "test_title",
                    mock_status,
                    context=mock_context,
                    chat_id=123456789,
                    url_or_filename="https://example.com/video",
                )

            self.assertEqual(mock_send_video_safely.call_count, 1)
            call_args = mock_send_video_safely.call_args[0]
            self.assertEqual(call_args[0], mock_context)
            self.assertEqual(call_args[1], 123456789)
            self.assertEqual(call_args[2], str(paths.final_video))
            self.assertIn("test_title", call_args[3])
            self.assertEqual(call_args[4], mock_status)
            self.assertEqual(call_args[5], "https://example.com/video")

    async def test_process_v2_telegram_job_falls_back_to_safe_edit_when_no_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video_path = root / "input.mp4"
            video_path.write_bytes(b"input video content")

            paths = TelegramJobPaths.create(str(root / "workspace"), str(root / "output"), "test_job_headless")
            paths.prepare_directories()

            async def fake_run_pipeline(*args, **kwargs):
                paths.final_video.write_bytes(b"dummy final video")

            mock_safe_edit = AsyncMock()
            mock_status = AsyncMock()

            with patch("backend.telegram_bot.run_pipeline_v2_for_telegram", side_effect=fake_run_pipeline), \
                 patch("backend.telegram_bot.safe_edit_status", mock_safe_edit):
                await process_v2_telegram_job(
                    str(video_path),
                    paths,
                    "test_headless",
                    mock_status,
                    context=None,
                    chat_id=None,
                )

            self.assertEqual(mock_safe_edit.call_count, 1)
            call_args = mock_safe_edit.call_args[0]
            self.assertEqual(call_args[0], mock_status)
            self.assertIn("test_headless", call_args[1])


if __name__ == "__main__":
    unittest.main()
