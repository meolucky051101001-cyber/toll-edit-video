"""Deterministic tests with no model downloads, network or real media processing."""
import asyncio
import datetime
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'backend')]
import v1_resource_control as rc
from backend.ai import v1_voice_cache as cache
from backend import ocr_utils as ocr


class OptimizationTests(unittest.TestCase):
    def test_rolling_refills_before_slow_cue_finishes(self):
        async def scenario():
            third = asyncio.Event()
            async def cue(i):
                if i == 0:
                    await asyncio.wait_for(third.wait(), 1)
                if i == 2:
                    third.set()
                return i
            with patch.object(rc, 'speech_parallelism', return_value=2):
                self.assertEqual(await rc.bounded_speech_map(cue, range(4)), [0, 1, 2, 3])
        asyncio.run(scenario())

    def test_ocr_budget(self):
        for memory, expected in [(800, 2), (3000, 6), (6000, 12), (None, 2)]:
            with patch.object(rc, 'memory_snapshot', return_value={'available_mb': memory}):
                self.assertEqual(rc.ocr_batch_limit(4 * 1048576), expected)

    def test_exact_ocr_reuse_never_merges_changed_pixels(self):
        import numpy as np
        a = np.zeros((4, 4, 3), dtype=np.uint8)
        b = a.copy()
        b[1, 1, 1] = 1
        ocr._frame_cache.clear()
        try:
            with patch.object(ocr, '_recognize_batch', return_value=[['a'], ['b']]) as recognize:
                result = ocr._readtext_batch([a, a.copy(), b])
                self.assertEqual(result, [['a'], ['a'], ['b']])
                self.assertEqual(len(recognize.call_args.args[0]), 2)
                result[0].append('mutated')
                self.assertEqual(ocr._readtext_batch([a]), [['a']])
                self.assertEqual(recognize.call_count, 1)
        finally:
            ocr._frame_cache.clear()

    def test_cache_duration_not_rounded(self):
        segment = SimpleNamespace(content='Xin chào', start=datetime.timedelta(0), end=datetime.timedelta(seconds=1.01))
        first = cache.voice_cache_key(segment, 'edge', 'voice')
        segment.end = datetime.timedelta(seconds=1.04)
        self.assertNotEqual(first, cache.voice_cache_key(segment, 'edge', 'voice'))

    def test_shared_cache_and_corruption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, target = root/'source.mp3', root/'target.mp3'
            source.write_bytes(b'a'*256)
            with patch.object(cache, 'GLOBAL_CACHE_DIR', root/'cache'):
                cache.write_voice_cache(source, 'key', 1.5)
                self.assertEqual(cache.read_voice_cache(target, 'key'), 1.5)
                self.assertEqual(target.read_bytes(), source.read_bytes())
                target.write_bytes(b'b'*256)
                (root/'cache'/'key.mp3').write_bytes(b'b'*256)
                self.assertIsNone(cache.read_voice_cache(target, 'key'))

    def test_cache_failure_does_not_fail_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'audio.mp3'
            path.write_bytes(b'a'*256)
            with patch.object(cache, '_atomic_meta', side_effect=OSError('read-only')):
                cache.write_voice_cache(path, 'key', 1.5)
            self.assertEqual(path.stat().st_size, 256)

    def test_invalid_duration_is_never_cached(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'audio.mp3'
            path.write_bytes(b'a'*256)
            with patch.object(cache, '_atomic_meta') as write:
                cache.write_voice_cache(path, 'key', float('nan'))
                write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
