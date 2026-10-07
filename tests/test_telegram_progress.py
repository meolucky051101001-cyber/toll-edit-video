import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import telegram_progress as progress


class TelegramProgressTests(unittest.TestCase):
    def test_actual_telegram_messages_match_dashboard_steps(self):
        token = progress.current_job.set("test-job")
        try:
            cases = [
                ("🎧 *Bước 2/6:* Đang trích xuất âm thanh gốc...", 1),
                ("🧠 *Bước 2.5/6:* AI Demucs đang tách giọng...", 2),
                ("🤖 Whisper AI đang nhận dạng từ Vocal sạch...", 3),
                ("👀 *Bước 3.5/6:* Đang quét vùng phụ đề cố định (OCR)...", 3.5),
                ("🌐 *Bước 4/6:* Đang dùng Gemini AI để dịch...", 4),
                ("🗣️ *Bước 5/6:* Đang lồng tiếng AI...", 5),
                ("🎬 *Bước 6/6:* Đang render video (NVENC)...", 6),
            ]
            for message, step in cases:
                with self.subTest(step=step), patch.object(progress.job_tracker, "update_step") as update:
                    progress.report(message)
                    self.assertEqual(update.call_args.args[0], step)
        finally:
            progress.current_job.reset(token)

    def test_other_telegram_messages_do_not_change_job(self):
        with patch.object(progress.job_tracker, "update_step") as update:
            progress.report("Đang render video")
            update.assert_not_called()

    def test_tracker_failure_does_not_break_processing(self):
        token = progress.current_job.set("test-job")
        try:
            with patch.object(progress.job_tracker, "update_step", side_effect=OSError("disk")):
                progress.report("Đang render video")
        finally:
            progress.current_job.reset(token)
