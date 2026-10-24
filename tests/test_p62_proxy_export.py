"""Test proxy 540p (P6.2): endpoint generate/status với file nguồn thật nhỏ."""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

BASE_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BASE_DIR))

import workflow_api
import main as main_app
from main import app
from fastapi.testclient import TestClient


class TestProxy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = Path(self._tmp.name)
        self.input_dir = self.workspace / "input"
        self.input_dir.mkdir(parents=True, exist_ok=True)
        for name, value in {
            "WORKSPACE": self.workspace,
            "INPUT_DIR": self.input_dir,
            "DOWNLOADS_DIR": self.workspace / "downloads",
        }.items():
            patcher = patch.object(workflow_api, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, headers={"X-Local-Control-Token": main_app.LOCAL_TOKEN})
        workflow_api._PROXY_GENERATING.clear()

    def _make_tiny_video(self, name="vid.mp4"):
        p = self.input_dir / name
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=0.3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(p),
        ], check=True)
        return p

    def test_generate_missing_source_404(self):
        res = self.client.post("/api/workflow/proxy/generate", json={"stem": "khong_co", "version": "final"})
        self.assertEqual(res.status_code, 404)

    def test_status_before_generate(self):
        self._make_tiny_video()
        res = self.client.get("/api/workflow/proxy/status", params={"stem": "vid", "version": "final"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["ready"], False)

    def test_generate_then_ready(self):
        self._make_tiny_video()
        res = self.client.post("/api/workflow/proxy/generate", json={"stem": "vid", "version": "final"})
        self.assertEqual(res.status_code, 200)
        # đợi thread generate (video 0.3s → rất nhanh)
        ready = False
        for _ in range(40):
            time.sleep(0.25)
            st = self.client.get("/api/workflow/proxy/status", params={"stem": "vid", "version": "final"}).json()
            if st.get("ready"):
                ready = True
                break
        self.assertTrue(ready, "proxy chưa sẵn sàng sau 10s")
        self.assertIn("proxy_final", st["url"])
        proxy = self.workspace / "vid" / "proxy_final.mp4"
        self.assertTrue(proxy.is_file())
        # kích thước phải nhỏ hơn nguồn (540p proxy)
        self.assertLess(proxy.stat().st_size, (self.input_dir / "vid.mp4").stat().st_size)


if __name__ == "__main__":
    unittest.main()
