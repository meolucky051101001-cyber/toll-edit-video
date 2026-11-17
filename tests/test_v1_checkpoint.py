import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.v1_checkpoint import Checkpoint, ManifestManager
from backend.ai.v1_asr_isolated import extract_subtitles_isolated

class CheckpointTests(unittest.TestCase):
    def test_publish_updates_artifact_without_counting_qc_as_render_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, rendered, final = (root / name for name in ('input', 'temp', 'final'))
            source.write_bytes(b'input')
            rendered.write_bytes(b'rendered')
            manager = ManifestManager(root)
            manager.get_or_create_manifest('job', source)
            with patch('backend.v1_checkpoint.time.time', return_value=100):
                manager.record_stage_start('job', 'video_rendering')
            with patch('backend.v1_checkpoint.time.time', return_value=120):
                manager.record_stage_complete('job', 'video_rendering', {'temp_render': rendered})
            rendered.replace(final)
            with patch('backend.v1_checkpoint.time.time', return_value=180):
                manager.record_stage_complete('job', 'video_rendering', {'temp_render': final})
            stage = manager.load_manifest('job').stages['video_rendering']
            self.assertEqual(stage['duration_s'], 20)
            self.assertEqual(stage['completed_at'], 120)
            self.assertEqual(stage['artifacts']['temp_render']['path'], str(final))

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

