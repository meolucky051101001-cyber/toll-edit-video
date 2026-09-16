import asyncio
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import srt
import batch_control
from batch_checkpoint import Checkpoints


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_checkpoint_reuses_output_and_rebuilds_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            source.write_bytes(b"input")
            output = root / "subs.srt"
            cache = Checkpoints(root, source)
            produce = AsyncMock(return_value=[
                srt.Subtitle(1, timedelta(), timedelta(seconds=2), "Xin chao")])
            await cache.subtitles("transcribe", source, produce, output)
            await cache.subtitles("transcribe", source, produce, output)
            self.assertEqual(produce.await_count, 1)
            output.write_text("corrupted")
            await cache.subtitles("transcribe", source, produce, output)
            self.assertEqual(produce.await_count, 2)
            source.write_bytes(b"new-input")
            await cache.subtitles("transcribe", source, produce, output)
            self.assertEqual(produce.await_count, 3)

    async def test_stop_terminates_owned_process(self):
        import time
        start = time.monotonic()
        token = batch_control.stop_check.set(lambda: time.monotonic() - start > 0.5)
        try:
            with self.assertRaisesRegex(RuntimeError, "stop requested"):
                await asyncio.to_thread(batch_control.run,
                    [sys.executable, "-c", "import time; time.sleep(30)"])
            self.assertLess(time.monotonic() - start, 15)
        finally:
            batch_control.stop_check.reset(token)
