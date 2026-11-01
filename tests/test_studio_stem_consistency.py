"""Test thư mục job thống nhất giữa pipeline (stem thô) và Studio (artifact_stem)."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import workflow_api
import main as main_app
from main import app
from fastapi.testclient import TestClient


class ResolveJobDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        patcher = patch.object(workflow_api, "WORKSPACE", self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_raw_dir_when_neither_exists(self):
        self.assertEqual(workflow_api.resolve_job_dir("vid one"), self.workspace / "vid one")

    def test_prefers_raw_pipeline_dir_over_legacy_clean_dir(self):
        (self.workspace / "vid one").mkdir()
        (self.workspace / "vid_one").mkdir()
        self.assertEqual(workflow_api.resolve_job_dir("vid one"), self.workspace / "vid one")

    def test_falls_back_to_legacy_clean_dir(self):
        (self.workspace / "vid_one").mkdir()
        self.assertEqual(workflow_api.resolve_job_dir("vid one"), self.workspace / "vid_one")

    def test_plain_stem_maps_to_same_dir(self):
        self.assertEqual(workflow_api.resolve_job_dir("vid"), self.workspace / "vid")


class StemConsistencyApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.workspace = root / "workspace"
        self.input_dir = root / "input"
        self.downloads_dir = root / "downloads"
        for d in (self.workspace, self.input_dir, self.downloads_dir):
            d.mkdir()
        for name, val in (("WORKSPACE", self.workspace), ("INPUT_DIR", self.input_dir),
                          ("DOWNLOADS_DIR", self.downloads_dir)):
            patcher = patch.object(workflow_api, name, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})

    def test_save_subtitles_and_download_srt_share_raw_dir(self):
        subs = [{"start": 0.0, "end": 1.5, "text": "Xin chào"}]
        res = self.client.post(
            "/api/workflow/save-subtitles",
            json={"stem": "vid one", "subtitles": subs, "video_version": "raw"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        raw_srt = self.workspace / "vid one" / "translated.srt"
        self.assertTrue(raw_srt.is_file())
        self.assertFalse((self.workspace / "vid_one").exists())

        dl = self.client.get("/api/workflow/download-srt", params={"stem": "vid one", "type": "translated"})
        self.assertEqual(dl.status_code, 200, dl.text)
        self.assertIn("Xin chào", dl.content.decode("utf-8-sig"))

    def test_apply_edits_writes_final_into_raw_dir_only(self):
        (self.input_dir / "vid one.mp4").write_bytes(b"fake source")

        def fake_apply(src, clips, dest, **kwargs):
            Path(dest).write_bytes(b"FINAL")
            return True

        payload = {
            "stem": "vid one",
            "clips": [{"start": 0.0, "end": 1.0}],
            "target_version": "raw",
        }
        with patch.object(workflow_api, "_apply_timeline_edits_file", side_effect=fake_apply), \
                patch.object(workflow_api.subprocess, "run"):
            result = workflow_api._execute_apply_edits(payload)

        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.workspace / "vid one" / "final_vid one.mp4").is_file())
        self.assertFalse((self.workspace / "vid_one").exists())

    def test_payload_video_version_finds_final_in_legacy_dir(self):
        legacy_dir = self.workspace / "vid_one"
        legacy_dir.mkdir()
        (legacy_dir / "final_vid one.mp4").write_bytes(b"FINAL")
        self.assertEqual(
            workflow_api._payload_video_version({}, workflow_api.resolve_job_dir("vid one"), "vid one"),
            "final",
        )


if __name__ == "__main__":
    unittest.main()
