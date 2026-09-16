import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'backend')]
from ai import v1_translation_budget as budget
import job_tracker
import v1_resource_control as rc


class TranslationFinalTests(unittest.TestCase):
    def tearDown(self):
        rc.end_video()

    def test_reject_bad_payloads(self):
        for value in ({'a': 'b'}, [''], [123], ['中文'], ['ok', 'extra'], None):
            self.assertFalse(budget.valid_translation(value, 1))
        self.assertTrue(budget.valid_translation(['Xin chào'], 1))

    def test_deadline_prevents_new_call(self):
        token = budget.deadline.set(time.monotonic()-1)
        try:
            with patch.object(budget, 'run_fallback') as run:
                with self.assertRaises(TimeoutError):
                    budget.bounded_call(lambda: 'unused')
                run.assert_not_called()
        finally:
            budget.deadline.reset(token)

    def test_failure_restores_subtitles(self):
        segment = SimpleNamespace(content='original')
        @budget.translation_budget
        def translate(segments):
            segments[0].content = 'partial'
            segments[0].orig_content = 'original'
            raise TimeoutError('test')
        with self.assertRaises(TimeoutError):
            translate([segment])
        self.assertEqual(segment.content, 'original')
        self.assertFalse(hasattr(segment, 'orig_content'))
        self.assertIsNone(budget.deadline.get())

    def test_classify_and_redact(self):
        with patch.object(budget, 'run_fallback', return_value=SimpleNamespace(status_code=503)):
            with self.assertLogs('ai.translation') as logs:
                budget.bounded_call(lambda: None, 'https://example.test/models/model-a:generate?key=SECRET')
        output = ' '.join(logs.output)
        self.assertIn('temporary_service', output)
        self.assertNotIn('SECRET', output)

    def test_old_video_cannot_record_model(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(job_tracker, 'STATUS_FILE', Path(folder)/'status.json'), patch.object(job_tracker, '_CURRENT_STATE', {'video_id':'new'}):
                rc.begin_video('old')
                job_tracker.record_translation_model('wrong')
                job_tracker.record_translation_attempt('wrong', 'response')
                self.assertNotIn('translation_models', job_tracker._CURRENT_STATE)
                self.assertNotIn('translation_attempts', job_tracker._CURRENT_STATE)

    def test_ten_synthetic_video_lifecycles(self):
        # Lifecycle test only: does not claim ten real video renders.
        for index in range(10):
            rc.begin_video(str(index))
            sampler = rc.video_context.get()
            result = rc.end_video()
            self.assertEqual(result['video_id'], str(index))
            self.assertFalse(sampler.thread.is_alive())


if __name__ == '__main__':
    unittest.main()
