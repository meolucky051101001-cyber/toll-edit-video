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

    async def test_end_to_end_queue_to_delivery_preserves_chat_id(self):
        from backend.durable_adapter import DurableQueue

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "queue.sqlite3"
            queue = DurableQueue(db_path)
            fake_app = SimpleNamespace(bot=AsyncMock())
            queue.initialize(fake_app)

            mock_chat = SimpleNamespace(id=987654321)
            mock_message = SimpleNamespace(
                message_id=42,
                chat_id=987654321,
                chat=mock_chat,
                reply_text=AsyncMock(),
            )
            mock_update = SimpleNamespace(
                update_id=1001,
                effective_chat=mock_chat,
                message=mock_message,
                to_json=lambda: '{"update_id": 1001, "message": {"message_id": 42, "chat": {"id": 987654321}, "text": "http://example.com/vid.mp4"}}',
            )

            job_input = {
                'type': 'url',
                'url': 'http://example.com/vid.mp4',
                'update': mock_update,
                'chat_id': 987654321,
                'pos': 1,
            }
            await queue.put(job_input)

            # Claim from queue (deserializes from SQLite)
            with patch("backend.durable_adapter.Update.de_json", return_value=mock_update), \
                 patch("backend.durable_adapter.CallbackContext.from_update", return_value=SimpleNamespace()):
                claimed_job = await queue.get()
            self.assertEqual(claimed_job.get('chat_id'), 987654321)

            # Test run_durable_video flow
            with patch("backend.telegram_bot.global_queue", queue), \
                 patch("backend.telegram_bot.process_v2_telegram_job", new_callable=AsyncMock) as mock_process:
                from backend.telegram_bot import run_durable_video
                dummy_vid = Path(td) / "dummy.mp4"
                dummy_vid.write_bytes(b"dummy")
                claimed_job['video_path'] = str(dummy_vid)

                await run_durable_video(claimed_job)

                self.assertEqual(mock_process.call_count, 1)
                self.assertEqual(mock_process.call_args[1].get('chat_id'), 987654321)


if __name__ == "__main__":
    unittest.main()
