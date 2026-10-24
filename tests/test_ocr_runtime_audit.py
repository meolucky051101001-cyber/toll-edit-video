"""Offline regressions for OCR timestamps, worker ownership and sample completeness."""
import bisect
import importlib.util
import json
import math
import subprocess
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from backend import ocr_adaptive as adaptive
from backend import video_sampling
from batch_control import stop_check


def isolated_session():
    path = Path(__file__).resolve().parents[1] / "backend" / "ai" / "v1_ocr_session.py"
    spec = importlib.util.spec_from_file_location("ocr_runtime_test_session", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TimestampCapture:
    def __init__(self, times):
        self.times = times
        self.index = -1
        self.grabs = 0
        self.released = False

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_POS_FRAMES:
            self.index = int(value) - 1
        else:
            self.index = bisect.bisect_left(self.times, value / 1000) - 1
        return True

    def get(self, prop):
        return self.times[self.index] * 1000

    def grab(self):
        self.index += 1
        self.grabs += 1
        return self.index < len(self.times)

    def retrieve(self):
        return True, np.full((100, 100, 3), self.index, np.uint8)

    def release(self):
        self.released = True


def reader(capture, duration=2):
    return adaptive.OrderedFrameReader(capture, 10, 100, 100, lambda: False,
        dict(seeks=0, grabbed_frames=0, decoded_samples=0), duration)


class OCRTimestampTests(unittest.TestCase):
    def test_vfr_selects_frame_displayed_at_request_then_can_rewind(self):
        cap = TimestampCapture([0., .1, .6, .8])
        frames = reader(cap, .9)
        image, _ = frames.read(.4)
        self.assertEqual(int(image[0, 0, 0]), 1)
        self.assertAlmostEqual(frames.actual_time, .1)
        image, _ = frames.read(.65)
        self.assertEqual(int(image[0, 0, 0]), 2)
        self.assertAlmostEqual(frames.actual_time, .6)
        image, _ = frames.read(.02)
        self.assertEqual(int(image[0, 0, 0]), 0)

    def test_eof_returns_latest_decoded_frame_not_previous_request(self):
        frames = reader(TimestampCapture([i / 10 for i in range(11)]), 1.1)
        self.assertEqual(int(frames.read(.2)[0][0, 0, 0]), 2)
        self.assertEqual(int(frames.read(1.02)[0][0, 0, 0]), 10)
        self.assertAlmostEqual(frames.actual_time, 1.)

    def test_truncated_tail_does_not_stretch_last_frame_across_gap(self):
        frames = reader(TimestampCapture([i / 10 for i in range(11)]), 2.)
        with self.assertRaisesRegex(RuntimeError, "final PTS"):
            frames.read(1.5)

    def test_invalid_or_stalled_pts_fail_before_scanning_entire_video(self):
        for times in ([0., 0., .1, .2], [0., math.nan], [0., math.inf],
                      [0., -.1], [0., .2, .1]):
            cap = TimestampCapture(times)
            # The test begins at the first frame; it does not rely on seeking
            # through an invalid timestamp sequence with bisect.
            cap.set = lambda *args: setattr(cap, "index", -1)
            with self.subTest(times=times), self.assertRaisesRegex(RuntimeError, "timestamp"):
                reader(cap).read(.3)
            self.assertLessEqual(cap.grabs, 3)

    def test_invalid_request_timestamp_never_decodes(self):
        for timestamp in (math.nan, math.inf, -1.):
            cap = TimestampCapture([0., .1])
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                reader(cap).read(timestamp)
            self.assertEqual(cap.grabs, 0)

    def test_frame_edges_remain_available_to_ocr(self):
        cap = TimestampCapture([0., .1])
        image = np.zeros((100, 100, 3), np.uint8)
        image[:3] = image[-3:] = 255
        cap.retrieve = lambda: (True, image)
        decoded, _ = reader(cap).read(.05)
        self.assertEqual(decoded.shape, image.shape)
        self.assertTrue(decoded[:3].all())
        self.assertTrue(decoded[-3:].all())

    def test_nearby_vfr_requests_do_not_share_rounded_nominal_key(self):
        calls = []

        class FrameReader:
            def __init__(self, *args, **kwargs):
                pass

            def read(self, timestamp):
                calls.append(timestamp)
                return np.full((10, 10, 3), round(timestamp * 100), np.uint8), 1.

        segment = SimpleNamespace(index=1, start=timedelta(0),
            end=timedelta(seconds=.12), content="speech")

        def recognize(images):
            return [[([[2, 5], [8, 5], [8, 7], [2, 7]],
                      str(image[0, 0, 0]), .95)] for image in images]

        with patch.object(adaptive, "OrderedFrameReader", FrameReader):
            samples = adaptive.collect_adaptive_samples(SimpleNamespace(release=lambda: None),
                [segment], 10, 10, 10, .14, recognize, lambda: False, {})
        by_time = {sample[0]: sample[-1]["rows"][0][1] for sample in samples}
        self.assertEqual(by_time[.06], "6")
        self.assertEqual(by_time[.07], "7")
        self.assertIn(.07, calls)


class OCRWorkerDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.session = isolated_session()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.session.close_session)
        directory = Path(self.temporary.name)
        self.request, self.response = directory / "request", directory / "response"
        self.args = ["python", "worker", "--request", str(self.request),
                     "--response", str(self.response)]

    def owner_holds_lock(self):
        entered, release = threading.Event(), threading.Event()

        def owner():
            with self.session._lock:
                entered.set()
                release.wait(2)

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.addCleanup(thread.join, 1)
        self.addCleanup(release.set)
        return release

    def fake_worker(self):
        worker = Mock()
        worker.poll.return_value = None
        self.session._process = worker
        self.session._key = ("python", "worker", None, None)
        return worker

    def test_queue_timeout_is_bounded_and_does_not_kill_owner(self):
        worker = self.fake_worker()
        release = self.owner_holds_lock()
        started = time.monotonic()
        with patch.object(self.session.subprocess, "Popen") as spawn:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.session.run_request(self.args, timeout=.05, env={})
        self.assertLess(time.monotonic() - started, .4)
        spawn.assert_not_called()
        worker.terminate.assert_not_called()
        worker.stdin.write.assert_not_called()
        release.set()

    def test_queue_cancellation_does_not_kill_owner(self):
        worker = self.fake_worker()
        release = self.owner_holds_lock()
        checks = 0

        def cancel_queued_request():
            nonlocal checks
            checks += 1
            return checks >= 3

        token = stop_check.set(cancel_queued_request)
        try:
            with patch.object(self.session.subprocess, "Popen") as spawn:
                with self.assertRaisesRegex(RuntimeError, "stop requested"):
                    self.session.run_request(self.args, timeout=1, env={})
            spawn.assert_not_called()
            worker.terminate.assert_not_called()
            worker.stdin.write.assert_not_called()
        finally:
            stop_check.reset(token)
            release.set()

    def test_expired_deadline_after_lock_does_not_launch_worker(self):
        with patch.object(self.session.time, "monotonic", side_effect=[0., 2.]), \
             patch.object(self.session.subprocess, "Popen") as spawn:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.session.run_request(self.args, timeout=1., env={})
        spawn.assert_not_called()

    def test_startup_uses_request_budget_and_does_not_write_after_expiry(self):
        worker = Mock()
        worker.poll.return_value = None
        with patch.object(self.session.time, "monotonic", side_effect=[0., .1, .2, 2.]), \
             patch.object(self.session.subprocess, "Popen", return_value=worker):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.session.run_request(self.args, timeout=1., env={})
        worker.stdin.write.assert_not_called()
        worker.terminate.assert_called_once()

    def test_active_timeout_terminates_owned_worker(self):
        worker = self.fake_worker()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.session.run_request(self.args, timeout=.025, env={})
        worker.stdin.write.assert_called_once()
        worker.terminate.assert_called_once()
        self.assertIsNone(self.session._process)

    def test_successful_request_can_reuse_worker(self):
        worker = self.fake_worker()

        def reply(line):
            Path(json.loads(line)["response"]).write_text('{"success":true}', encoding="utf-8")

        worker.stdin.write.side_effect = reply
        with patch.object(self.session.subprocess, "Popen") as spawn:
            self.session.run_request(self.args, timeout=1, env={})
            self.response.unlink()
            self.session.run_request(self.args, timeout=1, env={})
        spawn.assert_not_called()
        self.assertEqual(worker.stdin.write.call_count, 2)
        worker.terminate.assert_not_called()

    def test_invalid_deadline_never_starts_worker(self):
        for timeout in (0., -1., math.nan, math.inf):
            with self.subTest(timeout=timeout), patch.object(self.session.subprocess, "Popen") as spawn:
                with self.assertRaises(ValueError):
                    self.session.run_request(self.args, timeout=timeout, env={})
            spawn.assert_not_called()


class OCRFrameCompletenessTests(unittest.TestCase):
    def test_successful_encoder_with_missing_samples_is_rejected(self):
        def partial_frames(command, **kwargs):
            image = np.zeros((10, 10, 3), np.uint8)
            cv2.imwrite(command[-1].replace("%03d", "001"), image)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(video_sampling.subprocess, "run", side_effect=partial_frames):
            with self.assertRaisesRegex(RuntimeError, "expected 3, received 1"):
                video_sampling.sample_video_frames("fixture.mp4", timestamps=[.1, .2, .3])

    def test_complete_samples_preserve_order_and_count(self):
        def complete_frames(command, **kwargs):
            for index in range(1, 4):
                image = np.full((10, 10, 3), index, np.uint8)
                cv2.imwrite(command[-1].replace("%03d", f"{index:03d}"), image)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(video_sampling.subprocess, "run", side_effect=complete_frames):
            frames = video_sampling.sample_video_frames("fixture.mp4", timestamps=[.1, .2, .3])
        self.assertEqual([int(image[0, 0, 0]) for image in frames], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
