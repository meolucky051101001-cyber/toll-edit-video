"""
Unit tests verifying the 5 priority fixes in Tool V2:
1. Path Traversal protection in workflow_api.is_safe_stream_path
2. FFmpeg concat forward-slash path formatting in mixer._render_scalable_voice_bus
3. WhisperModel in-memory caching and release in ai.transcription
4. Graceful shutdown and worker resurrection in telegram_bot.cmd_stop
5. Concurrent file locking and atomic updates in render_history.record_render_duration
"""
import os
import sys
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class TestPathTraversalFix(unittest.TestCase):
    def test_is_safe_stream_path_accepts_workspace_paths(self):
        from workflow_api import is_safe_stream_path, WORKSPACE, INPUT_DIR

        safe_ws_file = WORKSPACE / "test_video.mp4"
        self.assertTrue(is_safe_stream_path(safe_ws_file))

        safe_input_file = INPUT_DIR / "sample.mp4"
        self.assertTrue(is_safe_stream_path(safe_input_file))

    def test_is_safe_stream_path_rejects_arbitrary_system_paths(self):
        from workflow_api import is_safe_stream_path

        # Outside paths should be rejected
        dangerous_paths = [
            Path(r"C:\Windows\System32\drivers\etc\hosts"),
            Path(r"C:\Users\admin\Desktop\private.jpg"),
            Path(r"C:\test_arbitrary.mp4"),
        ]
        for p in dangerous_paths:
            self.assertFalse(is_safe_stream_path(p))


class TestFFmpegConcatPathFormatting(unittest.TestCase):
    def test_concat_script_uses_forward_slashes_on_windows(self):
        import time
        from pipeline_v2.mixer import _render_scalable_voice_bus, FFmpegMixSettings

        with tempfile.TemporaryDirectory() as tmp_dir:
            directory = Path(tmp_dir)
            chunk1 = directory / "chunk_01.flac"
            chunk1.touch()
            dubs = [{"audio_file": str(chunk1), "start": 0.0, "end": 2.0}]
            settings = FFmpegMixSettings(voice_chunk_seconds=10.0)

            def fake_render_group(group, start, dur, out_path, *args, **kwargs):
                out_path.touch()

            with patch("pipeline_v2.mixer._run_ffmpeg"), patch("pipeline_v2.mixer._render_voice_group", side_effect=fake_render_group):
                _render_scalable_voice_bus(dubs, 5.0, directory, settings, "ffmpeg", deadline=time.time() + 10)

            concat_file = directory / "voice-chunks.ffconcat"
            self.assertTrue(concat_file.is_file())
            content = concat_file.read_text(encoding="utf-8")
            self.assertIn("ffconcat version 1.0", content)
            # Ensure backslashes are not present in the file paths
            for line in content.splitlines():
                if line.startswith("file "):
                    self.assertNotIn("\\", line, f"Found backslash in concat line: {line}")
                    self.assertIn("/", line, f"Expected forward slash in concat line: {line}")


class TestWhisperModelCache(unittest.TestCase):
    def test_whisper_model_cached_and_released(self):
        from ai import transcription

        mock_instance = MagicMock()
        with patch("faster_whisper.WhisperModel", return_value=mock_instance) as mock_cls:
            # First load
            model1 = transcription.get_or_load_whisper_model("large-v3", num_workers=1)
            self.assertEqual(model1, mock_instance)
            self.assertEqual(mock_cls.call_count, 1)

            # Second load should return cached instance without re-instantiating
            model2 = transcription.get_or_load_whisper_model("large-v3", num_workers=1)
            self.assertEqual(model2, mock_instance)
            self.assertEqual(mock_cls.call_count, 1)

            # Release model
            transcription.release_whisper_model()
            self.assertIsNone(transcription._cached_whisper_model)

            # Next load should re-instantiate
            model3 = transcription.get_or_load_whisper_model("large-v3", num_workers=1)
            self.assertEqual(mock_cls.call_count, 2)

            # Cleanup
            transcription.release_whisper_model()


class TestTelegramGracefulShutdown(unittest.TestCase):
    def test_cmd_stop_graceful_sequence(self):
        import asyncio
        import shared_state
        from telegram_bot import cmd_stop

        update = MagicMock()
        update.message.reply_text = MagicMock()
        # Make reply_text an async mock
        async def async_reply(*args, **kwargs):
            return None
        update.message.reply_text.side_effect = async_reply

        context = MagicMock()

        async def run_test():
            with patch("psutil.Process") as mock_proc, patch("telegram_bot.ensure_worker") as mock_ensure:
                mock_child = MagicMock()
                mock_proc.return_value.children.return_value = [mock_child]

                with patch("psutil.wait_procs", return_value=([], [])):
                    await cmd_stop(update, context)

                # Verified stop_requested was reset in finally
                self.assertFalse(shared_state.stop_requested)
                # Verified child.terminate was called before kill
                mock_child.terminate.assert_called_once()
                # Verified ensure_worker was called to restore the worker for next jobs
                mock_ensure.assert_called_once()

        asyncio.run(run_test())


class TestRenderHistoryConcurrency(unittest.TestCase):
    def test_concurrent_writes_do_not_corrupt_history(self):
        import render_history

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_history_file = Path(tmp_dir) / ".render_history_test.json"
            with patch.object(render_history, "HISTORY_FILE", test_history_file):
                num_threads = 10
                records_per_thread = 5

                def worker(thread_idx):
                    for i in range(records_per_thread):
                        name = f"video_{thread_idx}_{i}.mp4"
                        render_history.record_render_duration(name, 100 + thread_idx * 10 + i)

                threads = [threading.Thread(target=worker, args=(t,)) for t in range(num_threads)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertTrue(test_history_file.is_file())
                data = json.loads(test_history_file.read_text(encoding="utf-8"))
                # Check that all videos were recorded without loss
                for thread_idx in range(num_threads):
                    for i in range(records_per_thread):
                        name = f"video_{thread_idx}_{i}.mp4"
                        self.assertIn(name, data, f"Missing record: {name}")


if __name__ == "__main__":
    unittest.main()
