import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from backend.ai.ocr_session import OCRSession
from backend.ai.model_runtime import ModelRuntimeError


class OCRSessionLifecycleTests(unittest.TestCase):
    def test_ocr_session_closes_log_and_terminates_descendants(self):
        with tempfile.TemporaryDirectory() as cache_dir:
            policy = SimpleNamespace(
                model_cache_directory=cache_dir,
                runtime_python_path=lambda: sys.executable,
            )
            session = OCRSession.__new__(OCRSession)
            session.temp = tempfile.TemporaryDirectory(prefix="v2-test-ocr-", ignore_cleanup_errors=True)
            session.root = Path(session.temp.name)
            session.number = 0
            session.closed = False
            session.log = (session.root / "worker.log").open("wb")

            cmd = [
                sys.executable,
                "-c",
                "import subprocess, sys, time; p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)",
            ]
            import subprocess
            from backend.ai.model_runtime import _creation_flags
            session.process = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=session.log,
                stderr=session.log,
                creationflags=_creation_flags(),
            )

            self.assertFalse(session.log.closed)
            self.assertIsNone(session.process.poll())

            session.close()

            self.assertTrue(session.closed)
            self.assertTrue(session.log.closed)
            self.assertIsNotNone(session.process.poll())

            with self.assertRaises(ModelRuntimeError):
                session.run({}, timeout=1)

            session.close()


if __name__ == "__main__":
    unittest.main()
