import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.v1_checkpoint import Checkpoint
from backend.ai.v1_asr_isolated import extract_subtitles_isolated

class CheckpointTests(unittest.TestCase):
    def test_invalidation(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d)/'in', Path(d)/'out'
            source.write_bytes(b'input'); output.write_bytes(b'output')
            cp = Checkpoint(source, [output], {'model': 'a'})
            self.assertFalse(cp.hit())
            cp.save(); self.assertTrue(cp.hit())
            self.assertFalse(Checkpoint(source, [output], {'model': 'b'}).hit())
            output.write_bytes(b'corrupt'); self.assertFalse(cp.hit())
            output.write_bytes(b'output'); source.write_bytes(b'other')
            self.assertFalse(Checkpoint(source, [output], {'model': 'a'}).hit())

    def test_missing_and_empty_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            source, output = Path(d)/'in', Path(d)/'out'
            source.write_bytes(b'input')
            cp = Checkpoint(source, [output], {})
            cp.save(); self.assertFalse(cp.hit())
            output.write_bytes(b''); cp.save(); self.assertFalse(cp.hit())

    def test_asr_reuse_and_audio_change(self):
        def fake_run(args, **kw):
            Path(args[-1]).write_text('1\n00:00:00,000 --> 00:00:01,000\nhello\n', encoding='utf-8')
        with tempfile.TemporaryDirectory() as d, patch('backend.batch_control.run', side_effect=fake_run) as run:
            source, output = Path(d)/'in.wav', Path(d)/'out.srt'
            source.write_bytes(b'fixture')
            extract_subtitles_isolated(source, output)
            self.assertEqual(extract_subtitles_isolated(source, output)[0].content, 'hello')
            self.assertEqual(run.call_count, 1)
            source.write_bytes(b'new audio')
            extract_subtitles_isolated(source, output)
            self.assertEqual(run.call_count, 2)

if __name__ == '__main__':
    unittest.main()

