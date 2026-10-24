import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend.telegram_bot as telegram_bot


class TelegramLegacyDeliveryTests(unittest.TestCase):
    def test_publishes_before_tracker_marks_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "render.mp4"
            source.write_bytes(b"rendered video bytes")

            def verify_destination(video_name, output_path, duration):
                output = Path(output_path)
                self.assertEqual(video_name, "input.mp4")
                self.assertTrue(output.is_file())
                self.assertEqual(output.read_bytes(), source.read_bytes())
                self.assertEqual(duration, 12.5)

            with patch.object(telegram_bot, "OUTPUT_DIR", str(root / "delivery")), \
                 patch.object(telegram_bot.job_tracker, "finish_video", side_effect=verify_destination) as finish:
                result = telegram_bot.publish_legacy_telegram_output(
                    source, "input", "input.mp4", 12.5
                )

            self.assertEqual(result, root / "delivery" / "Dubbed_input.mp4")
            finish.assert_called_once()

    def test_copy_failure_never_marks_job_complete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "render.mp4"
            source.write_bytes(b"rendered video bytes")

            with patch.object(telegram_bot, "OUTPUT_DIR", str(root / "delivery")), \
                 patch.object(telegram_bot, "atomic_copy_file", side_effect=OSError("disk unavailable")), \
                 patch.object(telegram_bot.job_tracker, "finish_video") as finish:
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    telegram_bot.publish_legacy_telegram_output(
                        source, "input", "input.mp4", 12.5
                    )

            finish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
