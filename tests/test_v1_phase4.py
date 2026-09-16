import os
import time
import json
import uuid
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import backend.social_downloader as sd
from backend.render_history import record_render_duration, get_all_render_durations, HISTORY_FILE, LOCK_FILE

class Phase4Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = Path(self.tmp.name)
        
        # Patch history paths
        self.patcher1 = patch('backend.render_history.HISTORY_FILE', self.tmp_dir / '.render_history.json')
        self.patcher2 = patch('backend.render_history.LOCK_FILE', self.tmp_dir / '.render_history.lock')
        self.patcher1.start()
        self.patcher2.start()

    def tearDown(self):
        self.patcher1.stop()
        self.patcher2.stop()
        self.tmp.cleanup()

    def test_history_concurrency(self):
        # Simulate writer A and writer B
        import backend.render_history as rh
        
        def writer_a():
            rh.record_render_duration("videoA.mp4", 100)
            
        def writer_b():
            rh.record_render_duration("videoB.mp4", 200)
            
        t1 = threading.Thread(target=writer_a)
        t2 = threading.Thread(target=writer_b)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        
        data = json.loads(rh.HISTORY_FILE.read_text(encoding="utf-8"))
        self.assertIn("videoA.mp4", data)
        self.assertIn("videoB.mp4", data)
        self.assertEqual(data["videoA.mp4"], 100)
        self.assertEqual(data["videoB.mp4"], 200)

    def test_history_crash_safety(self):
        import backend.render_history as rh
        rh.HISTORY_FILE.write_text("{ corrupt json }")
        
        rh.record_render_duration("videoC.mp4", 300)
        
        # Should not crash, and should have backed up the corrupt file
        data = json.loads(rh.HISTORY_FILE.read_text(encoding="utf-8"))
        self.assertIn("videoC.mp4", data)
        self.assertEqual(data["videoC.mp4"], 300)
        
        # Check backup exists
        backups = list(self.tmp_dir.glob(".render_history.corrupt.*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{ corrupt json }")

    @patch('backend.social_downloader.subprocess.run')
    def test_download_timeout(self, mock_run):
        # Setting SOCIAL_DOWNLOAD_TIMEOUT_SECONDS=1 and pretending subprocess hangs
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="yt-dlp", timeout=1)
        
        # Make a dummy partial file to ensure it's cleaned up
        os.makedirs(self.tmp_dir / "output", exist_ok=True)
        partial = self.tmp_dir / "output" / "prefix_video.part"
        partial.write_text("partial data")
        
        with patch.dict(os.environ, {"SOCIAL_DOWNLOAD_TIMEOUT_SECONDS": "1"}):
            success, final, raw, err = sd.download_social_video("http://test", str(self.tmp_dir / "output"), "prefix")
            
            self.assertFalse(success)
            self.assertIn("Tải video quá lâu (>1.0s)", err)
            self.assertFalse(partial.exists(), "Partial file was not cleaned up!")

    def test_partial_file_safety(self):
        # Check that partial files are not marked as completed
        # Assuming we just ensure the downloader doesn't return True on timeout or partial file
        # The atomic replace in download_file_stream handles this.
        # This tests that if download_file_stream times out, atomic_replace is never called, and temporary_path is deleted
        with patch('backend.social_downloader.requests.get') as mock_get:
            mock_resp = Mock()
            mock_resp.status_code = 200
            mock_resp.headers = {"Content-Length": "1000"}
            
            def slow_iter(chunk_size):
                time.sleep(2.0) # exceed timeout
                yield b"test"
                
            mock_resp.iter_content = slow_iter
            mock_get.return_value.__enter__.return_value = mock_resp
            
            with patch.dict(os.environ, {"SOCIAL_DOWNLOAD_TIMEOUT_SECONDS": "1"}):
                success = sd.download_file_stream("http://test", str(self.tmp_dir / "dest.mp4"), timeout=(1, 1))
                self.assertFalse(success)
                
                # Check no partial file exists
                partials = list(self.tmp_dir.glob("*.downloading"))
                self.assertEqual(len(partials), 0)
                self.assertFalse((self.tmp_dir / "dest.mp4").exists())

if __name__ == '__main__':
    unittest.main()
