import tempfile
import unittest
from pathlib import Path

from backend.pipeline_v2.gpu_executor import GPUStageError, GPUStageExecutor
from backend.pipeline_v2.gpu_lock import GPULockTimeout, InterProcessGPULock
from backend.pipeline_v2.segments import RuntimeSegment, segment_from_dict, segment_to_dict
from datetime import timedelta


class GPULockTests(unittest.TestCase):
    def test_lock_blocks_second_owner_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gpu.lock"
            first = InterProcessGPULock(path, timeout_seconds=1.0)
            first.acquire()
            try:
                with self.assertRaises(GPULockTimeout):
                    InterProcessGPULock(
                        path, timeout_seconds=0.05, poll_seconds=0.01
                    ).acquire()
            finally:
                first.release()
            with InterProcessGPULock(path, timeout_seconds=0.2):
                self.assertTrue(path.is_file())


class GPUExecutorTests(unittest.TestCase):
    def test_health_stage_runs_in_child_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = GPUStageExecutor(root / "control", root / "gpu.lock")
            result = executor.run("health", {}, timeout_seconds=30)
            self.assertEqual(result["worker"], "pipeline_v2")

    def test_unknown_stage_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executor = GPUStageExecutor(root / "control", root / "gpu.lock")
            with self.assertRaises(GPUStageError):
                executor.run("unknown", {}, timeout_seconds=30)


class SegmentSerializationTests(unittest.TestCase):
    def test_round_trip_preserves_source_identity(self):
        segment = RuntimeSegment(
            index=2,
            start=timedelta(seconds=1.25),
            end=timedelta(seconds=2.5),
            content="Xin chào",
            source_segment_id=9,
            y_pct=0.8,
        )
        restored = segment_from_dict(segment_to_dict(segment))
        self.assertEqual(restored.index, 2)
        self.assertEqual(restored.source_segment_id, 9)
        self.assertEqual(restored.start.total_seconds(), 1.25)
        self.assertEqual(restored.y_pct, 0.8)


if __name__ == "__main__":
    unittest.main()
