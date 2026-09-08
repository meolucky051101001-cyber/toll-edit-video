import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from backend.ai import v1_ocr_session as session
import subprocess

class OCRSessionTests(unittest.TestCase):
    def tearDown(self):
        session.close_session()

    def test_batches_share_process_and_close(self):
        with tempfile.TemporaryDirectory() as d:
            req, res = Path(d)/"request.json", Path(d)/"response.json"
            process = Mock()
            process.poll.return_value = None
            def reply(line):
                target = Path(json.loads(line)["response"])
                target.write_text('{"success":true}', encoding="utf-8")
            process.stdin.write.side_effect = reply
            args=["python", "worker", "--request", str(req), "--response", str(res)]
            with patch.object(session.subprocess, "Popen", return_value=process) as spawn:
                session.run_request(args,timeout=2,env={})
                res.unlink()
                session.run_request(args,timeout=2,env={})
                self.assertEqual(spawn.call_count,1)
                session.close_session()
                process.terminate.assert_called_once()

    def test_timeout_kills_worker(self):
        with tempfile.TemporaryDirectory() as d:
            process=Mock()
            process.poll.return_value=None
            args=["python","worker","--request",str(Path(d)/"req"),"--response",str(Path(d)/"res")]
            with patch.object(session.subprocess,"Popen",return_value=process):
                with patch.object(session.time,"monotonic",side_effect=[0,2]):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        session.run_request(args,timeout=1,env={})
            process.terminate.assert_called_once()
            self.assertIsNone(session._process)
