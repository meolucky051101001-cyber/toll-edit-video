"""No models, network or production writes."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import v1_resource_control as rc
import job_tracker as tracker
from v1_stage_metrics import stage


class ResourceTests(unittest.TestCase):
    def tearDown(self):
        rc.end_video()

    def test_parallelism(self):
        for available, expected in [(None, 1), (850, 1), (3000, 2), (5000, 4)]:
            with patch.object(rc, 'memory_snapshot', return_value={'available_mb': available}):
                self.assertEqual(rc.speech_parallelism(), expected)

    def test_bounded_order_and_errors(self):
        async def run():
            active = peak = 0
            async def cue(i):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.001)
                active -= 1
                if i == 3:
                    raise ValueError('test')
                return i
            with patch.object(rc, 'speech_parallelism', return_value=2):
                results = await rc.bounded_speech_map(cue, range(100))
            self.assertEqual(peak, 2)
            self.assertEqual(len(results), 100)
            self.assertIsInstance(results[3], ValueError)
            self.assertEqual(results[-1], 99)
        asyncio.run(run())

    def test_cancel_drains_cues(self):
        async def run():
            entered = asyncio.Event()
            active = 0
            async def cue(i):
                nonlocal active
                active += 1
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    active -= 1
            with patch.object(rc, 'speech_parallelism', return_value=2):
                task = asyncio.create_task(rc.bounded_speech_map(cue, range(100)))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual(active, 0)
        asyncio.run(run())

    def test_memory_wait_is_bounded_and_cancellable(self):
        from batch_control import stop_check
        with patch.object(rc, 'memory_snapshot', return_value={'available_mb': 100}):
            with self.assertRaisesRegex(RuntimeError, '768 MB'):
                rc.wait_for_memory(timeout=0)
            token = stop_check.set(lambda: True)
            try:
                with self.assertRaisesRegex(RuntimeError, 'stop requested'):
                    rc.wait_for_memory()
            finally:
                stop_check.reset(token)

    def test_video_sampler_lifecycle(self):
        rc.begin_video('first')
        old = rc.video_context.get()
        rc.begin_video('second')
        self.assertFalse(old.thread.is_alive())
        summary = rc.end_video()
        self.assertEqual(summary['video_id'], 'second')
        self.assertGreaterEqual(summary['wall_seconds'], 0)
        self.assertIsNone(rc.end_video())

    def test_same_step_does_not_reset_clock(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(tracker, 'STATUS_FILE', Path(folder)/'status.json'), patch.object(tracker, '_CURRENT_STATE', {}):
                tracker.start_video('test.mp4')
                tracker.update_step(2, 'start')
                start = tracker._CURRENT_STATE['current_step_start']
                tracker.update_step(2, 'still working')
                self.assertEqual(tracker._CURRENT_STATE['current_step_start'], start)

    def test_stage_context_and_failure(self):
        @stage('test_stage')
        def work():
            raise ValueError('test')
        rc.begin_video('abc123')
        with self.assertLogs('v1.performance') as logs:
            with self.assertRaises(ValueError):
                work()
        self.assertIn('success=False', logs.output[-1])
        self.assertIn('video_id=abc123', logs.output[-1])

    def test_worker_environment_correlation(self):
        import batch_control
        rc.begin_video('worker123')
        with patch.object(batch_control.subprocess, 'run') as run:
            batch_control.run(['unused'], env={'CUSTOM': 'preserved'})
        env = run.call_args.kwargs['env']
        self.assertEqual(env['V1_METRICS_VIDEO_ID'], 'worker123')
        self.assertEqual(env['CUSTOM'], 'preserved')


if __name__ == '__main__':
    unittest.main()
