"""Regression tests for the streaming audio and pipeline refactor."""
import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import soundfile as sf

from backend.ai import audio_enhancer as audio
from backend.pipeline_v2.tts import resolve_mapped_voice
from backend.pipeline_v2.config import PipelineSettings
from backend.pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner


class AudioContracts(unittest.TestCase):
    def inputs(self, root, sample_rate=32000, separated_rate=None):
        separated_rate = separated_rate or sample_rate
        original, separated = root / 'original.wav', root / 'separated.wav'
        t = np.arange(sample_rate) / sample_rate
        s = np.arange(separated_rate) / separated_rate
        sf.write(original, np.column_stack((0.1 + 0.2*np.sin(t*2000), 0.2*np.cos(t*1500))), sample_rate)
        sf.write(separated, 0.1*np.cos(s*3000), separated_rate)
        return original, separated

    def test_chunk_sizes_preserve_filter_state_and_crossfade(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, separated = self.inputs(root)
            segments = [{'start': timedelta(seconds=.1), 'end': timedelta(seconds=.9)}]
            a, b = root / 'a.wav', root / 'b.wav'
            audio.preserve_pristine_background(original, separated, segments, a, chunk_seconds=.07)
            audio.preserve_pristine_background(original, separated, segments, b, chunk_seconds=2)
            np.testing.assert_allclose(sf.read(a)[0], sf.read(b)[0], atol=2e-7)

    def test_mismatched_rates_stream_without_full_file_read(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, separated = self.inputs(root, 48000, 44100)
            with mock.patch.object(audio.sf, 'read', side_effect=AssertionError('unbounded read')):
                result = audio.preserve_pristine_background(
                    original, separated, [{'start': .1, 'end': .9}], root/'out.wav',
                    restore_high_freq=False, chunk_seconds=.15)
            data, sr = sf.read(result)
            self.assertEqual(sr, 48000)
            self.assertEqual(data.shape, (48000, 2))
            reference = audio.resample_audio(sf.read(separated, dtype='float32')[0], 44100, 48000)
            np.testing.assert_allclose(data[12000:36000, 0], reference[12000:36000], atol=5e-4)

    def test_failure_preserves_output_and_does_not_retry_in_memory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, separated = self.inputs(root)
            target = root / 'existing.wav'
            target.write_bytes(b'previous result')
            with mock.patch.object(audio, 'preserve_pristine_background_chunked', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    audio.preserve_pristine_background(original, separated, [{'start': 0, 'end': 1}], target)
            self.assertEqual(target.read_bytes(), b'previous result')
            self.assertFalse(list(root.glob('enhance-*')))

    def test_output_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, separated = self.inputs(root)
            before = original.read_bytes()
            with self.assertRaises(ValueError):
                audio.preserve_pristine_background(original, separated, [], original)
            self.assertEqual(original.read_bytes(), before)

    def test_chunk_duration_must_be_positive_and_finite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            original, separated = self.inputs(root)
            for size in (0, -1, float('nan'), float('inf')):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    audio.preserve_pristine_background(original, separated, [], root/'out.wav', chunk_seconds=size)


class VoiceContracts(unittest.TestCase):
    def test_mappings_cannot_cross_providers(self):
        for provider, valid, invalid in (
            ('edge', 'vi-VN-NamMinhNeural', 'BV075_streaming'),
            ('capcut', 'BV075_streaming', 'vi-VN-NamMinhNeural'),
            ('fpt', 'leminh', 'BV075_streaming'),
        ):
            with self.subTest(provider=provider):
                self.assertEqual(resolve_mapped_voice(provider, {'male': valid}, '', 'male'), valid)
                self.assertIsNone(resolve_mapped_voice(provider, {'male': invalid}, '', 'male'))
        self.assertIsNone(resolve_mapped_voice('rvc', {'male': 'BV075_streaming'}, '', 'male'))


class BackgroundContracts(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_times_and_enhancement_failure_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = VideoPipelineRunner(VideoPipelineRequest(
                video_path=root/'source.mp4', job_directory=root/'job', output_path=root/'out.mp4',
                settings=PipelineSettings()))
            original, separated = root/'original.wav', root/'background.wav'
            original.write_bytes(b'original')
            separated.write_bytes(b'background')
            segment = SimpleNamespace(start=timedelta(seconds=.25), end=timedelta(seconds=1.5))
            with mock.patch.object(runner, '_background_audio', return_value=separated), \
                 mock.patch.object(runner, '_artifact_path', return_value=original), \
                 mock.patch.object(runner, '_load_segments', return_value=[segment]), \
                 mock.patch('backend.ai.audio_enhancer.preserve_pristine_background', side_effect=OSError('fixture')) as enhance:
                with self.assertLogs('backend.pipeline_v2.video_pipeline', level='WARNING'):
                    result = await runner._prepare_mix_background(root)
                self.assertEqual(result, separated)
                self.assertEqual(enhance.call_args.args[2], [{'start': .25, 'end': 1.5}])


if __name__ == '__main__':
    unittest.main()
