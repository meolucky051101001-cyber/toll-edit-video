import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import dashboard_monitor as d

class MonitorTests(unittest.TestCase):
    def test_empty(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, "WORKSPACE", Path(temp)):
            self.assertEqual(d.read_status()["status"], "idle")

    def test_qc_failure_never_completed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, "WORKSPACE", Path(temp)):
            target = Path(temp) / "video" / "pipeline_v2" / "job_manifest.json"
            target.parent.mkdir(parents=True)
            records = {key: {"status": "completed"} for key, _ in d.STAGES}
            records["qc"]["status"] = "failed"
            records["deliver"]["status"] = "pending"
            target.write_text(json.dumps({"stages": records, "metadata": {"source_path": "example.mp4"}}))
            state = d.read_status()
            self.assertEqual(state["status"], "error")
            self.assertLess(state["percent"], 100)
            self.assertIn("QC", state["message"])
            records["qc"]["status"] = "completed"
            records["deliver"]["status"] = "completed"
            target.write_text(json.dumps({"stages": records}))
            self.assertEqual(d.read_status()["percent"], 100)

    def test_running_is_not_claimed_live(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, "WORKSPACE", Path(temp)):
            target = Path(temp) / "video" / "pipeline_v2" / "job_manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"stages": {"ocr": {"status": "running"}}}))
            self.assertIn("chưa xác minh", d.read_status()["message"])

    def test_template(self):
        page = d.dashboard()
        self.assertIn("TOOL V2", page)
        self.assertIn("127.0.0.1:8088/", page)
        self.assertIn("127.0.0.1:8089/", page)
        self.assertNotIn('onclick="startBatch()"', page)
        self.assertIn("QC", page)
        self.assertIn("logSearch", page)

if __name__ == "__main__":
    unittest.main()

