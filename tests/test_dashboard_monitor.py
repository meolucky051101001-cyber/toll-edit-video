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
            with patch.object(
                d,
                "verify_dashboard_delivery",
                return_value={"valid": True, "pending": False, "reason": "Đã xác minh."},
            ):
                self.assertEqual(d.read_status()["percent"], 100)

    def test_running_is_not_claimed_live(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, "WORKSPACE", Path(temp)):
            target = Path(temp) / "video" / "pipeline_v2" / "job_manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({"stages": {"ocr": {"status": "running"}}}))
            self.assertIn("chưa xác minh", d.read_status()["message"])

    def test_translation_model_uses_job_checkpoint_provenance(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(d, "WORKSPACE", Path(temp)):
            pipeline = Path(temp) / "video" / "pipeline_v2"
            target = pipeline / "job_manifest.json"
            target.parent.mkdir(parents=True)
            target.write_text(json.dumps({
                "job_id": "video",
                "metadata": {"model_policy": {"gemini_model": "gemini-3.8-flash"}},
                "stages": {"translate": {"status": "completed"}},
            }), encoding="utf-8")
            batches = pipeline / "artifacts" / "translation" / "batches"
            batches.mkdir(parents=True)
            (batches / "00001.json").write_text(json.dumps({
                "quality": {"provider": "gemini", "model": "gemini-2.5-flash", "models": ["gemini-2.5-flash"]}
            }), encoding="utf-8")

            state = d.read_status()
            self.assertEqual(state["configured_gemini_model"], "gemini-3.8-flash")
            self.assertEqual(state["translation_models"], ["gemini-2.5-flash"])
            self.assertEqual(state["active_translation_model"], "gemini-2.5-flash")

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

