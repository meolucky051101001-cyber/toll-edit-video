"""Test tích hợp Concat (P7): tìm exe + endpoint open/import (AGPL: chỉ tiến trình ngoài)."""
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


class TestConcatIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        for name, value in {
            "WORKSPACE": self.workspace,
            "INPUT_DIR": self.workspace / "input",
        }.items():
            patcher = patch.object(workflow_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})

    def test_find_concat_exe_detects_real_binary(self):
        # Máy dev có concat.exe đã giải nén (P0 đã xác nhận)
        exe = workflow_api._find_concat_exe()
        if exe is not None:
            self.assertTrue(exe.is_file())
            self.assertIn("concat.exe", str(exe).lower())

    def test_open_without_exe_returns_404(self):
        with patch.object(workflow_api, "CONCAT_EXE_CANDIDATES", [self.workspace / "khong_co.exe"]):
            res = self.client.post("/api/workflow/concat/open", json={"stem": "vid"})
            self.assertEqual(res.status_code, 404)

    def test_open_spawns_detached_process(self):
        fake_exe = self.workspace / "fake_concat.exe"
        fake_exe.write_bytes(b"MZ fake")
        launched = []

        class FakeProc:
            pid = 4321

        def fake_popen(cmd, **kw):
            launched.append(cmd)
            return FakeProc()

        with patch.object(workflow_api, "CONCAT_EXE_CANDIDATES", [fake_exe]), \
             patch.object(workflow_api.subprocess, "Popen", side_effect=fake_popen):
            res = self.client.post("/api/workflow/concat/open", json={"stem": "vid 123/x"})
            self.assertEqual(res.status_code, 200)
            body = res.json()
            self.assertEqual(body["pid"], 4321)
            # stem được chuẩn hóa + project dir nằm trong workspace
            self.assertIn("vid_123_x", body["project_dir"])
            self.assertEqual(launched[0][0], str(fake_exe))
            self.assertEqual(launched[0][1], "--project")

    def test_open_requires_stem(self):
        res = self.client.post("/api/workflow/concat/open", json={"stem": ""})
        self.assertEqual(res.status_code, 400)

    def test_import_validates_size(self):
        res = self.client.post(
            "/api/workflow/concat/import",
            data={"stem": "vid"},
            files={"file": ("out.mp4", b"tiny", "video/mp4")},
        )
        self.assertEqual(res.status_code, 400)

    def test_import_saves_to_input_dir_as_new_library_item(self):
        payload = b"\x00\x00\x00\x18ftypmp42" + b"x" * 200_000
        res = self.client.post(
            "/api/workflow/concat/import",
            data={"stem": "vid 9/x"},
            files={"file": ("export.mp4", payload, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["filename"].startswith("concat_vid_9_x_"))
        saved = self.workspace / "input" / body["filename"]
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.stat().st_size, len(payload))


if __name__ == "__main__":
    unittest.main()
