import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from backend.v1_checkpoint import Checkpoint, ManifestManager
from backend.ai.v1_asr_isolated import extract_subtitles_isolated

class CheckpointTests(unittest.TestCase):
    def test_stage_metrics_keep_json_scalars_and_scalar_dicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.write_bytes(b'input')
            manager = ManifestManager(root)
            manager.get_or_create_manifest('metrics-job', source)

            manager.record_stage_metrics('metrics-job', 'visual_ocr', {
                'calls': 4,
                'ratio': 0.5,
                'enabled': True,
                'label': 'ocr',
                'missing': None,
                'summary': {'frames': 3, 'strategy': 'adaptive'},
                'detected_watermarks': [{'text': 'drop me'}],
                'nested_list': {'items': [1, 2]},
                'not_json': object(),
                'not_finite': float('nan'),
            })

            metrics = manager.load_manifest('metrics-job').stages['visual_ocr']['metrics']
            self.assertEqual(metrics, {
                'calls': 4,
                'ratio': 0.5,
                'enabled': True,
                'label': 'ocr',
                'missing': None,
                'summary': {'frames': 3, 'strategy': 'adaptive'},
            })

    def test_stage_start_resets_metrics_and_old_manifest_loads_without_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'input'
            source.write_bytes(b'input')
            manager = ManifestManager(root)
            manager.get_or_create_manifest('metrics-job', source)
            manager.record_stage_metrics('metrics-job', 'visual_ocr', {'calls': 4})
            manager.record_stage_start('metrics-job', 'visual_ocr')
            self.assertEqual(
                manager.load_manifest('metrics-job').stages['visual_ocr']['metrics'], {}
            )

            manifest_path = manager._manifest_file('metrics-job')
            data = json.loads(manifest_path.read_text(encoding='utf-8'))
            data['stages']['visual_ocr'].pop('metrics')
            manifest_path.write_text(json.dumps(data), encoding='utf-8')
            old_manifest = manager.load_manifest('metrics-job')
            self.assertIsNotNone(old_manifest)
            self.assertNotIn('metrics', old_manifest.stages['visual_ocr'])
            manager.record_stage_metrics('metrics-job', 'visual_ocr', {'calls': 2})
            self.assertEqual(
                manager.load_manifest('metrics-job').stages['visual_ocr']['metrics'],
                {'calls': 2},
            )

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
