import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from ai import translation as tr
from ai import v1_translation_cache as cache

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict('os.environ', {'AUTODUB_WORKSPACE': self.tmp.name})
        self.env.start()
        tr._gemini_cooldown.clear()
        tr._gemini_last_good.clear()
        self.frames = patch.object(tr, 'extract_video_frames_base64', return_value=[])
        self.frames.start()

    def tearDown(self):
        self.frames.stop()
        self.env.stop()
        self.tmp.cleanup()

    def success(self):
        response = Mock(status_code=200)
        response.json.return_value = {'candidates': [{'content': {'parts': [{'text': '["Xin chào"]'}]}}]}
        return response

    def test_failed_model_skipped_next_video_and_recovers(self):
        with patch.object(tr.requests, 'post', side_effect=[Mock(status_code=503), self.success()]) as post:
            self.assertEqual(tr.translate_with_gemini(['你好'], api_key='test-key'), ['Xin chào'])
            self.assertIn('gemini-3.7-flash', post.call_args_list[0].args[0])
            self.assertIn('gemini-3.5-flash', post.call_args_list[1].args[0])
        with patch.object(tr.requests, 'post', return_value=self.success()) as post:
            tr.translate_with_gemini(['您好'], api_key='test-key')
            self.assertEqual(post.call_count, 1)
            self.assertIn('gemini-3.5-flash', post.call_args.args[0])
        for key in tr._gemini_cooldown:
            tr._gemini_cooldown[key] = 0
        tr._gemini_last_good.clear()
        with patch.object(tr.requests, 'post', return_value=self.success()) as post:
            tr.translate_with_gemini(['三'], api_key='test-key')
            self.assertIn('gemini-3.7-flash', post.call_args.args[0])

    def test_cache_reuses_success_not_network(self):
        with patch.object(tr.requests, 'post', return_value=self.success()) as post:
            tr.translate_with_gemini(['你好'], api_key='test-key')
            self.assertEqual(tr.translate_with_gemini(['你好'], api_key='test-key'), ['Xin chào'])
            self.assertEqual(post.call_count, 1)

    def test_all_failed_fail_fast_during_cooldown(self):
        with patch.object(tr.requests, 'post', return_value=Mock(status_code=429)) as post:
            self.assertIsNone(tr.translate_with_gemini(['你好'], api_key='test-key'))
            self.assertIsNone(tr.translate_with_gemini(['你好'], api_key='test-key'))
            self.assertEqual(post.call_count, 7)

    def test_context_and_account_invalidate_cache(self):
        with patch.object(tr.requests, 'post', return_value=self.success()) as post:
            tr.translate_with_gemini(['你好'], api_key='test-key')
            tr.translate_with_gemini(['你好'], api_key='other-key')
            tr.translate_with_gemini(['你好'], api_key='test-key', prior_context=['different scene'])
            self.assertEqual(post.call_count, 3)

    def test_invalid_cache_rejected(self):
        cache.write_cache('a', ['中文'], 'model')
        self.assertIsNone(cache.read_cache('a', 1, 'vi'))
        cache.write_cache('b', ['Xin chào'], 'model')
        self.assertIsNone(cache.read_cache('b', 2, 'vi'))

    def test_transport_failure_does_not_poison_next_job(self):
        with patch.object(tr.requests, 'post', side_effect=tr.requests.ConnectionError('network')) as post:
            self.assertIsNone(tr.translate_with_gemini(['你好'], api_key='test-key'))
            self.assertEqual(post.call_count, 7)
        with patch.object(tr.requests, 'post', return_value=self.success()) as post:
            self.assertEqual(tr.translate_with_gemini(['您好'], api_key='test-key'), ['Xin chào'])
            self.assertEqual(post.call_count, 1)
            self.assertIn('gemini-3.7-flash', post.call_args.args[0])

if __name__ == '__main__':
    unittest.main()
