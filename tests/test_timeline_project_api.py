"""Test API lưu/đọc project.json timeline (P6.1: autosave + khôi phục sau crash)."""
import json
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


class TestTimelineProjectApi(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        patcher = patch.object(workflow_api, "WORKSPACE", self.workspace)
        patcher.start()
        self.addCleanup(patcher.stop)
        # POST qua middleware kiểm soát token (giống test_dashboard_hardening)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})

    def test_get_missing_returns_exists_false(self):
        res = self.client.get("/api/workflow/timeline/project", params={"stem": "chua_co"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["exists"], False)

    def test_save_and_roundtrip_with_stem_sanitized(self):
        project = {
            "version": 1,
            "fps": 30,
            "canvas": {"w": 1080, "h": 1920},
            "tracks": [{
                "id": "v1", "type": "video",
                "clips": [{"id": "c1", "start": 0.0, "end": 5.0, "at": 0.0, "speed": 2}],
            }],
        }
        res = self.client.post(
            "/api/workflow/timeline/project",
            json={"stem": "vid one/x", "project": project},
        )
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["saved"])
        self.assertTrue(body["saved_at"])
        self.assertNotIn(" ", body["path"])

        saved = json.loads((self.workspace / "vid_one_x" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["tracks"][0]["clips"][0]["speed"], 2)
        self.assertEqual(saved["stem"], "vid_one_x")

        res2 = self.client.get("/api/workflow/timeline/project", params={"stem": "vid one/x"})
        self.assertEqual(res2.status_code, 200)
        data = res2.json()
        self.assertEqual(data["exists"], True)
        self.assertEqual(data["project"]["tracks"][0]["clips"][0]["id"], "c1")

    def test_save_is_atomic_no_tmp_file_left(self):
        project = {"version": 1, "tracks": [{"id": "v1", "clips": [{"id": "c1", "start": 0, "end": 1}]}]}
        res = self.client.post("/api/workflow/timeline/project", json={"stem": "atom", "project": project})
        self.assertEqual(res.status_code, 200)
        self.assertFalse((self.workspace / "atom" / "project.json.tmp").exists())
        self.assertTrue((self.workspace / "atom" / "project.json").is_file())

    def test_save_rejects_invalid_payload(self):
        res = self.client.post("/api/workflow/timeline/project", json={"stem": "x", "project": {"foo": 1}})
        self.assertEqual(res.status_code, 400)
        res2 = self.client.post("/api/workflow/timeline/project", json={"stem": "", "project": {"tracks": [1]}})
        self.assertEqual(res2.status_code, 400)

    def test_get_corrupt_json_returns_exists_false(self):
        d = self.workspace / "broken"
        d.mkdir()
        (d / "project.json").write_text("{khong phai json", encoding="utf-8")
        res = self.client.get("/api/workflow/timeline/project", params={"stem": "broken"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["exists"], False)


if __name__ == "__main__":
    unittest.main()
