import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import srt
from pydub import AudioSegment
from pydub.generators import Sine

from backend.v1_audio_mixer import AdaptiveDuckingSettings, apply_adaptive_ducking, mix_adaptive_audio
from backend.ai.v1_conditional_asr import detect_suspicious_gaps, run_conditional_asr
from backend.ocr_utils import recover_missing_subtitles_from_ocr
from types import SimpleNamespace


class AudioRefactorTests(unittest.TestCase):
    def test_ocr_recovery_keeps_different_sentences_separate(self):
        rows = [dict(text=text, sample_time=t, y_pct=.82, prob=.95)
                for text, times in [('今天我们学习做饭', (3., 3.2)), ('然后放入锅里翻炒', (4., 4.2))]
                for t in times]
        rows.extend([dict(text='产品包装参数', sample_time=t, y_pct=.82, prob=.99,
                          is_packaging=True) for t in (7., 7.2)])
        result = recover_missing_subtitles_from_ocr(rows, [], SimpleNamespace(
            support=10, mode='asr_match', top=.8, bottom=.85), duration=10)
        self.assertEqual([r['text'] for r in result], ['今天我们学习做饭', '然后放入锅里翻炒'])

    def test_float_mix_does_not_flatten_overlapping_voice_peaks(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            bgm = Path(directory) / 'bgm.wav'
            voice = Path(directory) / 'voice.wav'
            output = Path(directory) / 'mixed.wav'
            AudioSegment.silent(duration=1000, frame_rate=44100).export(bgm, format='wav').close()
            original = Sine(440).to_audio_segment(duration=1000, volume=-1)
            original.export(voice, format='wav').close()
            mix_adaptive_audio(str(bgm), [dict(path=str(voice), start=0, duration=1)] * 2, str(output))
            result = AudioSegment.from_file(str(output))
            correlation = np.corrcoef(original.get_array_of_samples(), result.get_array_of_samples())[0, 1]
            self.assertGreater(correlation, .999)
            self.assertLessEqual(result.max_dBFS, -1.)

    def test_ducking_preserves_late_audio_marker(self):
        source = Sine(220).to_audio_segment(duration=6000, volume=-18)
        source = source.overlay(Sine(880).to_audio_segment(duration=100, volume=-5), position=5000)
        settings = AdaptiveDuckingSettings(base_bgm_gain_db=0)
        result = apply_adaptive_ducking(source, [
            dict(start=1, actual_audio_duration=1),
            dict(start=3, actual_audio_duration=1),
        ], settings)
        self.assertEqual(len(result), len(source))
        self.assertEqual(result[4800:5500].raw_data, source[4800:5500].raw_data)
        self.assertLess(result[1500:1700].dBFS, source[1500:1700].dBFS - 5)

    def test_nested_unsorted_cues_do_not_create_false_gaps(self):
        def cue(start, end):
            return srt.Subtitle(1, timedelta(seconds=start), timedelta(seconds=end), 'text')
        self.assertEqual(detect_suspicious_gaps([cue(4, 5), cue(1, 10), cue(12, 13)], 14), [])

    def test_retry_audio_is_bounded_and_decoded_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'original.wav'
            Sine(220).to_audio_segment(duration=60000, volume=-15).export(path, format='wav')
            with patch('backend.ai.v1_conditional_asr.retranscribe_gap_snippet', return_value=[]) as retry:
                run_conditional_asr(str(path), [], object(), max_retry_audio_seconds=18, max_snippet_seconds=8)
            windows = [(c.args[2], c.args[3]) for c in retry.call_args_list]
            self.assertEqual(windows, [(0, 8), (8, 16), (16, 18)])

    def test_missing_voice_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'bgm.wav'
            AudioSegment.silent(duration=1000).export(source, format='wav')
            output = Path(directory) / 'output.wav'
            with self.assertRaises(FileNotFoundError):
                mix_adaptive_audio(str(source), [dict(path=str(Path(directory)/'missing.wav'), start=0)], str(output))
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
