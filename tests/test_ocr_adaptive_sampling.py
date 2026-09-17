"""Exercise adaptive acquisition with timed frames; only OCR inference is fake."""
import importlib
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from backend.ocr_adaptive import collect_adaptive_samples
from backend.pipeline_v2.segments import RuntimeSegment


TEXT = "今天我们学习做饭"


def cue(index, start, end):
    return RuntimeSegment(index, timedelta(seconds=start), timedelta(seconds=end), TEXT)


class TimedCapture:
    def __init__(self, duration, state):
        self.duration, self.state = duration, state
        self.index = 0
        self.released = False
        self.seeks = []

    def isOpened(self):
        return True

    def get(self, prop):
        return {cv2.CAP_PROP_FPS: 30, cv2.CAP_PROP_FRAME_WIDTH: 360,
                cv2.CAP_PROP_FRAME_HEIGHT: 640,
                cv2.CAP_PROP_FRAME_COUNT: int(self.duration * 30),
                cv2.CAP_PROP_POS_MSEC: self.index * 1000 / 30}.get(prop, 0)

    def set(self, prop, value):
        self.index = round(value * 30 / 1000)
        self.seeks.append(self.index)
        return True

    def grab(self):
        self.index += 1
        return self.index <= int(self.duration * 30)

    def read(self):
        if self.index >= int(self.duration * 30):
            return False, None
        top = self.state(self.index / 30)
        self.index += 1
        frame = np.zeros((640, 360, 3), np.uint8)
        if top is not None:
            for x in range(100, 260, 12):
                frame[top:top + 20, x:x + 5] = 255
        return True, frame

    def release(self):
        self.released = True


def recognize(images):
    result = []
    for image in images:
        y, x = np.where(image[:, :, 0] > 0)
        if not len(x):
            result.append([])
        else:
            result.append([([[int(x.min()), int(y.min())], [int(x.max()), int(y.min())],
                             [int(x.max()), int(y.max())], [int(x.min()), int(y.max())]], TEXT, .95)])
    return result


class AdaptiveSamplingTests(unittest.TestCase):
    def acquire(self, duration, state, segments=None, inference=recognize, stopped=lambda: False):
        cap = TimedCapture(duration, state)
        metrics = {}
        samples = collect_adaptive_samples(cap, segments or [cue(1, 0, duration)],
            360, 640, 30, duration, inference, stopped, metrics)
        return samples, metrics, cap

    def test_stable_cues_reuse_coarse_results_and_reduce_seeks(self):
        samples, metrics, cap = self.acquire(6, lambda t: 480,
            [cue(1, 0, 2), cue(2, 2, 4), cue(3, 4, 6)])
        self.assertEqual(metrics["coarse_frames"], 6)
        self.assertEqual(metrics["refinement_frames"], 0)
        self.assertGreater(metrics["visual_reused"], 20)
        self.assertLessEqual(metrics["seeks"], 6)
        self.assertTrue(cap.released)
        self.assertTrue(all(item[4]["rows"] for item in samples))

    def test_coarse_miss_triggers_half_second_refinement(self):
        samples, metrics, _ = self.acquire(2, lambda t: 480 if .85 <= t <= 1.15 else None)
        self.assertEqual(metrics["locked_segments"], 0)
        self.assertEqual(metrics["refined_segments"], 1)
        self.assertGreater(metrics["refinement_frames"], 0)
        self.assertTrue(any(item[4]["rows"] for item in samples))

    def test_absence_and_jump_outside_old_band_invalidate_reuse(self):
        def state(t):
            return 480 if t < 2 else (None if t < 3 else 240)
        samples, metrics, _ = self.acquire(6, state)
        absent = [item for item in samples if 2.1 < item[0] < 2.9]
        moved = [item for item in samples if item[0] > 3.1]
        self.assertTrue(absent)
        self.assertTrue(all(not item[4]["rows"] for item in absent))
        self.assertTrue(all(item[4]["rows"][0][0][0][1] == 208 for item in moved))
        self.assertGreater(metrics["refinement_frames"], 0)

    def test_long_stable_cue_periodically_refreshes_recognition(self):
        _, metrics, _ = self.acquire(12, lambda t: 480)
        self.assertGreater(metrics["refinement_frames"], 2)
        self.assertLess(metrics["recognized_frames"], 20)
        self.assertLessEqual(metrics["peak_batch_frames"], 12)

    def test_incomplete_results_fail_and_release_capture(self):
        cap = TimedCapture(2, lambda t: 480)
        with self.assertRaisesRegex(RuntimeError, "incomplete adaptive"):
            collect_adaptive_samples(cap, [cue(1, 0, 2)], 360, 640, 30, 2,
                                     lambda images: [], lambda: False, {})
        self.assertTrue(cap.released)

    def test_cancel_and_decode_failure_release_capture(self):
        for cancelled in (False, True):
            cap = TimedCapture(2, lambda t: 480)
            if not cancelled:
                cap.read = lambda: (False, None)
            with self.subTest(cancelled=cancelled), self.assertRaises(RuntimeError):
                collect_adaptive_samples(cap, [cue(1, 0, 2)], 360, 640, 30, 2,
                                         recognize, lambda: cancelled, {})
            self.assertTrue(cap.released)

    def test_real_video_decoder_keeps_timestamps_and_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "timed.avi")
            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 24, (360, 640))
            self.assertTrue(writer.isOpened())
            try:
                for index in range(72):
                    frame = np.zeros((640, 360, 3), np.uint8)
                    if index < 24 or index >= 48:
                        top = 480 if index < 24 else 240
                        for x in range(100, 260, 12):
                            frame[top:top + 20, x:x + 5] = 255
                    writer.write(frame)
            finally:
                writer.release()
            metrics = {}
            cap = cv2.VideoCapture(path)
            self.assertTrue(cap.isOpened())
            samples = collect_adaptive_samples(cap, [cue(1, 0, 3)],
                360, 640, 24, 3, recognize, lambda: False, metrics)
            absent = [item for item in samples if 1.1 < item[0] < 1.9]
            moved = [item for item in samples if item[0] > 2.1]
            self.assertTrue(absent)
            self.assertTrue(all(not item[4]["rows"] for item in absent))
            self.assertTrue(all(item[4]["rows"][0][0][0][1] < 250 for item in moved))
            self.assertLessEqual(metrics["seeks"], 2)

    def test_single_frame_video_does_not_seek_past_eof(self):
        samples, _, cap = self.acquire(1 / 30, lambda t: 480)
        self.assertTrue(samples)
        self.assertTrue(cap.released)

    def test_production_path_keeps_continuous_tracking_without_stretching_absence(self):
        with patch.object(sys, "path", [str(Path(__file__).resolve().parents[1] / "backend")] + sys.path):
            module = importlib.import_module("backend.ocr_utils")
            for disappear in (False, True):
                segments = [cue(1, 0, 3)]
                cap = TimedCapture(3, lambda t: None if disappear and t >= 1.8 else 480)
                metrics = {}
                with self.subTest(disappear=disappear), patch.object(module.cv2, "VideoCapture", return_value=cap), patch.object(module, "_readtext_batch", side_effect=recognize):
                    module.perform_video_ocr("fixture.mp4", srt_segments=segments,
                                             adaptive=True, metrics=metrics)
                tracks = segments[0].tracking_blocks
                self.assertTrue(tracks)
                self.assertLess(tracks[0].start, .2)
                if disappear:
                    self.assertLessEqual(tracks[-1].end, 2.2)
                else:
                    self.assertAlmostEqual(tracks[-1].end, 3)
                    self.assertTrue(all(b.start <= a.end + .001 for a, b in zip(tracks, tracks[1:])))
                self.assertEqual(metrics["mode"], "adaptive")


if __name__ == "__main__":
    unittest.main()
