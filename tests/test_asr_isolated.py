import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.ai.v1_asr_isolated import extract_subtitles_isolated

class IsolatedTests(unittest.TestCase):
    def test_result_and_no_reuse(self):
        def fake_run(args, **kw):
            self.assertEqual(kw['env']['V1_ASR_REUSE'], '0')
            self.assertEqual(kw['timeout'], 600)
            Path(args[-1]).write_text('1\n00:00:00,000 --> 00:00:01,000\nhello\n', encoding='utf-8')
        with tempfile.TemporaryDirectory() as folder, patch('backend.batch_control.run', side_effect=fake_run):
            output = Path(folder)/'out.srt'
            self.assertEqual(extract_subtitles_isolated('audio.wav', output)[0].content, 'hello')
            self.assertTrue(output.is_file())
    def test_failure_does_not_publish(self):
        with tempfile.TemporaryDirectory() as folder, patch('backend.batch_control.run', side_effect=RuntimeError('cancelled')):
            output = Path(folder)/'out.srt'
            with self.assertRaises(RuntimeError):
                extract_subtitles_isolated('audio.wav', output)
            self.assertFalse(output.exists())
