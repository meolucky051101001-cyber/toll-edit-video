import sys
import tempfile
import types
import unittest
from unittest import mock
from backend.ai import voice_cloning

class DeadlineTests(unittest.TestCase):
    def test_pending_task_stops_at_elapsed_deadline(self):
        clock = [0.0]
        client = mock.Mock()
        client.create_tts_task.return_value = {"data": {"tasks": [{"id": "id", "token": "test"}]}}
        client.query_tts_task.return_value = {"data": {"tasks": [{"status": "pending"}]}}
        module = types.SimpleNamespace(CapCutClient=lambda: client)
        def sleep(seconds):
            clock[0] += seconds
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.dict(sys.modules, {"capcut_tts_api": module}), \
             mock.patch("time.monotonic", side_effect=lambda: clock[0]), \
             mock.patch("time.sleep", side_effect=sleep):
            with self.assertRaises(TimeoutError):
                voice_cloning._run_capcut_tts_once("Xin chao", folder + "/audio.mp3")
        self.assertEqual(clock[0], 60.0)
        self.assertLessEqual(client.query_tts_task.call_count, 20)

if __name__ == "__main__":
    unittest.main()
