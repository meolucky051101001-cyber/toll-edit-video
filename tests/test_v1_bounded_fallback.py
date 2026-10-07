import threading
import time
import unittest
from backend.ai.v1_bounded_fallback import run_fallback

class FallbackTests(unittest.TestCase):
    def test_success(self):
        self.assertEqual(run_fallback(lambda: ["xin chao"]), ["xin chao"])

    def test_error(self):
        with self.assertRaises(ValueError):
            run_fallback(lambda: int("bad"))

    def test_timeout_does_not_wait_or_spawn_another_worker(self):
        release = threading.Event()
        finished = threading.Event()
        def blocked():
            try:
                release.wait(2)
            finally:
                finished.set()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                run_fallback(blocked, timeout=0.02)
            self.assertLess(time.monotonic() - started, 0.5)
            with self.assertRaises(TimeoutError):
                run_fallback(lambda: self.fail("must not start"), timeout=0.02)
        finally:
            release.set()
            finished.wait(2)

if __name__ == "__main__":
    unittest.main()
