"""Offline regression cases: no bot, network, GPU or media rendering."""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

from backend.durable_jobs import JobStore
from backend.pipeline_v2.cover_qc import inspect_covers


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'queue.sqlite3'
        self.store = JobStore(self.path)

    def test_restart_recovers_inflight_before_queued_and_keeps_checkpoint(self):
        first = self.store.enqueue('a', {'url': 'a'})
        second = self.store.enqueue('b', {'url': 'b'})
        self.assertEqual(self.store.claim()[0], first)
        self.store.checkpoint(first, {'url': 'a', 'video_path': 'download.mp4'})
        restarted = JobStore(self.path)
        restarted.recover()
        job_id, data = restarted.claim()
        self.assertEqual(job_id, first)
        self.assertEqual(data['video_path'], 'download.mp4')
        restarted.finish(first, 'completed')
        self.assertEqual(restarted.claim()[0], second)

    def test_cancel_is_terminal_even_if_late_worker_finishes(self):
        first = self.store.enqueue('a', {})
        self.store.enqueue('b', {})
        self.store.claim()
        self.store.cancel()
        self.store.finish(first, 'completed')
        self.store.recover()
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.store.counts(), {'cancelled': 2})

    def test_replayed_update_deduplicates_but_new_submission_can_retry(self):
        first = self.store.enqueue('update-1', {})
        self.assertIsNone(self.store.enqueue('update-1', {}))
        self.store.claim()
        self.store.finish(first, 'failed', 'test failure')
        self.store.recover()
        self.assertIsNone(self.store.claim())
        self.assertIsNotNone(self.store.enqueue('update-2', {}))

    def test_connection_is_closed_and_transaction_rolls_back(self):
        with self.assertRaises(RuntimeError):
            with self.store.connect() as db:
                db.execute("INSERT INTO jobs(dedupe,payload,state,updated) VALUES('x','{}','queued',0)")
                raise RuntimeError('rollback')
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute('SELECT 1')
        self.assertEqual(self.store.counts(), {})


class TelegramSerializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_datetime_roundtrip_and_bad_payload_does_not_stop_consumer(self):
        import asyncio
        from telegram import Update, Message, Chat, User, Bot
        from backend.durable_adapter import DurableQueue
        with tempfile.TemporaryDirectory() as directory:
            queue = DurableQueue(Path(directory) / 'queue.sqlite3')
            app = MagicMock()
            app.bot = Bot('123456:test')
            queue.initialize(app)
            bad = queue.store.enqueue('bad', {'type': 'url', 'update': {}})
            original = Update(10, message=Message(1, datetime.now(timezone.utc),
                Chat(123, 'private'), from_user=User(123, 'Test', False), text='test'))
            accepted = await queue.put({'type': 'url', 'url': 'https://example.invalid/video', 'update': original})
            self.assertIsNotNone(accepted)
            self.assertIsNone(await queue.put({'type': 'url', 'url': 'https://example.invalid/video', 'update': original}))
            with patch('backend.durable_adapter.CallbackContext.from_update', return_value=object()):
                restored = await asyncio.wait_for(queue.get(), timeout=3)
            self.assertEqual(restored['update'].message.chat.id, 123)
            self.assertIsInstance(restored['update'].message.date, datetime)
            self.assertEqual(queue.store.counts(), {'failed': 1, 'running': 1})
            queue.task_done()
            self.assertEqual(queue.store.counts(), {'failed': 1, 'completed': 1})


class CoverTests(unittest.TestCase):
    def segment(self):
        return {'index': 1, 'start': 0, 'end': 2, 'tracking_blocks': [
            dict(start=0, end=2, x_pct=.1, max_x_pct=.9, y_pct=.8, max_y_pct=.9)]}

    def ass(self, end='0:00:02.00', width=80):
        return ('[Script Info]\nPlayResX: 100\nPlayResY: 100\n[Events]\n'
                'Dialogue: 0,0:00:00.00,' + end + ',BgStyle,,0,0,0,,'
                r'{\an7\pos(10,80)}{\p1}m 0 0 l ' + str(width) +
                ' 0 l ' + str(width) + r' 10 l 0 10{\p0}' + '\n')

    def test_missing_white_box_is_detected_even_when_text_anchor_is_safe(self):
        ass = self.ass().replace('BgStyle', 'TextStyle')
        result = inspect_covers([self.segment()], ass)
        self.assertEqual(result['failures'][0]['uncovered_seconds'], 2)

    def test_too_narrow_and_early_ending_cover_fail(self):
        self.assertTrue(inspect_covers([self.segment()], self.ass(width=40))['failures'])
        result = inspect_covers([self.segment()], self.ass(end='0:00:01.00'))
        self.assertEqual(result['failures'][0]['uncovered_seconds'], 1)

    def test_complete_cover_passes_and_no_source_geometry_is_unverified(self):
        self.assertFalse(inspect_covers([self.segment()], self.ass())['failures'])
        result = inspect_covers([dict(index=2, start=0, end=1)], self.ass())
        self.assertEqual(result['unverified_segments'], [2])


class OCRReuseTests(unittest.TestCase):
    def test_session_roundtrip_reuses_process_and_cleans_up(self):
        import sys
        from types import SimpleNamespace
        from backend.ai.ocr_session import OCRSession
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'fake.py'
            fake.write_text('import sys,json,time\nfrom pathlib import Path\nr=Path(sys.argv[2])\nwhile r.exists():\n for p in r.glob("request-*.json"):\n  data=json.loads(p.read_text())\n  out=r/p.name.replace("request-","response-")\n  tmp=out.with_suffix(".tmp")\n  tmp.write_text(json.dumps({"success":True,"result":data}))\n  p.unlink()\n  tmp.replace(out)\n time.sleep(.01)\n', encoding='utf-8')
            policy = SimpleNamespace(model_cache_directory=root, runtime_python_path=lambda: Path(sys.executable))
            with patch('backend.ai.ocr_session._worker_path', return_value=fake):
                session = OCRSession(policy)
            try:
                pid = session.process.pid
                self.assertEqual(session.run({'batch':1}, 5), {'batch':1})
                self.assertEqual(session.run({'batch':2}, 5), {'batch':2})
                self.assertEqual(session.process.pid, pid)
            finally:
                session.close()
            self.assertIsNotNone(session.process.poll())
            self.assertFalse(session.root.exists())

    def test_same_configuration_reuses_model_and_changed_configuration_reloads(self):
        from backend.model_workers import model_runtime_worker as worker
        constructor = MagicMock()
        with patch.dict('sys.modules', {'paddleocr': MagicMock(PaddleOCR=constructor)}), patch.object(worker, '_ocr_models', {}):
            worker._run_paddle_ocr({'images': [], 'engine': 'onnxruntime'})
            worker._run_paddle_ocr({'images': [], 'engine': 'onnxruntime'})
            self.assertEqual(constructor.call_count, 1)
            worker._run_paddle_ocr({'images': [], 'engine': 'paddle'})
            self.assertEqual(constructor.call_count, 2)


if __name__ == '__main__':
    unittest.main()
