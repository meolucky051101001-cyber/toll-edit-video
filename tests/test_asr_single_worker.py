import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager
from backend.ai import transcription as t

class WorkerTests(unittest.TestCase):
    def test_serial_gpu_uses_one_worker_without_changing_quality(self):
        model = Mock()
        model.transcribe.return_value = (iter([]), None)
        @contextmanager
        def lease(key, factory):
            yield factory()
        with patch.object(t, '_cached_model_path', return_value='local-model'), patch('torch.cuda.is_available', return_value=True), patch.object(t.asr_cache, 'lease', side_effect=lease), patch.object(t, 'WhisperModel', return_value=model) as ctor:
            t._transcribe_once('audio.wav', 'large-v3-turbo', 2)
        self.assertEqual(ctor.call_args.kwargs['num_workers'], 1)
        self.assertEqual(ctor.call_args.kwargs['device'], 'cuda')
        self.assertEqual(ctor.call_args.kwargs['compute_type'], 'int8_float16')
        self.assertEqual(model.transcribe.call_args.kwargs['beam_size'], 5)
        self.assertTrue(model.transcribe.call_args.kwargs['word_timestamps'])
