import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import social_downloader as download


class DownloadDeadlineTests(unittest.TestCase):
    def test_empty_zero_or_invalid_timeout_is_always_finite(self):
        for value in ("", "0", "-1", "nan", "inf", "bad"):
            with self.subTest(value=value), patch.dict(os.environ, {"SOCIAL_DOWNLOAD_TIMEOUT_SECONDS": value}):
                self.assertEqual(download._download_timeout(), 120)
        with patch.dict(os.environ, {"SOCIAL_DOWNLOAD_TIMEOUT_SECONDS": "300"}):
            self.assertEqual(download._download_timeout(), 300)

    def test_timeout_removes_staged_fragments_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "prefix_old.mp4"
            existing.write_bytes(b"old video")
            def stalled(command, **kwargs):
                template = command[command.index("-o") + 1]
                Path(template.replace("%(title).30s", "new").replace("%(ext)s", "mp4") + ".part").write_bytes(b"partial")
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            with patch.object(download, "run_download", side_effect=stalled):
                result = download.download_social_video("https://example.com/video", folder, "prefix")
            self.assertFalse(result[0])
            self.assertEqual(list(Path(folder).iterdir()), [existing])
            self.assertEqual(existing.read_bytes(), b"old video")

    def test_success_publishes_only_the_verified_new_download(self):
        with tempfile.TemporaryDirectory() as folder:
            def finished(command, **kwargs):
                template = command[command.index("-o") + 1]
                Path(template.replace("%(title).30s", "new").replace("%(ext)s", "mp4")).write_bytes(b"new video")
                return SimpleNamespace(returncode=0, stderr="")
            with patch.object(download, "run_download", side_effect=finished), \
                 patch.object(download, "probe_downloaded_video") as probe:
                result = download.download_social_video("https://example.com/video", folder, "prefix")
            self.assertTrue(result[0])
            self.assertEqual(Path(result[1]).read_bytes(), b"new video")
            probe.assert_called_once()
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_owned_subprocess_is_terminated_on_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pid.txt"
            code = "import os,time,pathlib; pathlib.Path(os.environ['AUDIT_PID_FILE']).write_text(str(os.getpid())); time.sleep(30)"
            with self.assertRaises(subprocess.TimeoutExpired):
                download.run_download([sys.executable, "-c", code], timeout=1.0,
                                      env={**os.environ, "AUDIT_PID_FILE": str(pid_file)}, capture_output=True)
            self.assertTrue(pid_file.exists())
            import psutil
            self.assertFalse(psutil.pid_exists(int(pid_file.read_text())))
