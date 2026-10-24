"""Test _run_ffmpeg_ctl (P6.3): huỷ job render + hành vi thường."""
import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

from workflow_api import _run_ffmpeg_ctl  # noqa: E402


class TestRunFfmpegCtl(unittest.TestCase):
    def test_cancelled_before_start_returns_99_fast(self):
        ctl = {"cancelled": True, "proc": None, "total_dur": 0.0, "progress": None}
        res = _run_ffmpeg_ctl(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=64x64:duration=10", "-f", "null", "-"],
            ctl,
        )
        self.assertEqual(res.returncode, 99)

    def test_without_ctl_runs_normally(self):
        res = _run_ffmpeg_ctl(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=64x64:duration=0.3", "-f", "null", "-"],
            None,
        )
        self.assertEqual(res.returncode, 0)

    def test_with_ctl_runs_and_reports_proc(self):
        ctl = {"cancelled": False, "proc": None, "total_dur": 0.0, "progress": None}
        res = _run_ffmpeg_ctl(
            ["ffmpeg", "-f", "lavfi", "-i", "testsrc=size=64x64:duration=0.3", "-f", "null", "-"],
            ctl,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIsNotNone(ctl.get("proc"))


if __name__ == "__main__":
    unittest.main()
