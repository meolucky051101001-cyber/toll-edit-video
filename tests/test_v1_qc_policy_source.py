import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.v1_feature_flags import get_feature_flags, set_feature_flags
from backend.v1_orchestrator import V1Orchestrator, resolve_qc_policy
from backend.v1_video_router import VideoMetadata


class QCPolicySourceTests(unittest.TestCase):
    def test_default_is_block_and_persisted_setting_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(get_feature_flags(tmp)["V1_QC_POLICY"], "BLOCK")
            self.assertEqual(resolve_qc_policy(workspace_path=tmp), "BLOCK")

            set_feature_flags({"V1_QC_POLICY": "WARN"}, tmp)
            self.assertEqual(resolve_qc_policy(workspace_path=tmp), "WARN")

            with self.assertRaises(ValueError):
                set_feature_flags({"V1_QC_POLICY": "NOT_A_POLICY"}, tmp)

    def test_job_plan_freezes_only_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            video = tmp_path / "clip.mp4"
            video.write_bytes(b"dummy clip data")
            orchestrator = V1Orchestrator(workspace_path=tmp_path)
            router_globals = orchestrator.plan_job.__globals__["create_router_snapshot"].__globals__
            with mock.patch.dict(router_globals, {"extract_media_metadata": mock.Mock(
                    return_value=VideoMetadata(duration_s=45.0, width=1920, height=1080, fps=30.0))}):
                default_plan = orchestrator.plan_job(video, "job-default", user_mode="auto")
                explicit_plan = orchestrator.plan_job(
                    video, "job-explicit", user_mode="auto",
                    overrides={"qc_policy": "REPORT_ONLY"},
                )

            self.assertNotIn("qc_policy", default_plan["effective_config"])
            self.assertEqual(
                resolve_qc_policy(default_plan["effective_config"], workspace_path=tmp_path),
                "BLOCK",
            )
            self.assertEqual(explicit_plan["effective_config"]["qc_policy"], "REPORT_ONLY")
            self.assertEqual(explicit_plan["effective_config"]["qc_policy_source"], "job")
            self.assertEqual(
                resolve_qc_policy(explicit_plan["effective_config"], workspace_path=tmp_path),
                "REPORT_ONLY",
            )

    def test_legacy_snapshot_pin_is_ignored_in_favor_of_live_setting(self):
        legacy_snapshot = {"qc_policy": "REPORT_ONLY"}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_qc_policy(legacy_snapshot, workspace_path=tmp), "BLOCK")
            set_feature_flags({"V1_QC_POLICY": "WARN"}, tmp)
            self.assertEqual(resolve_qc_policy(legacy_snapshot, workspace_path=tmp), "WARN")

    def test_runtime_override_wins_over_snapshot_and_live_setting(self):
        snapshot = {"qc_policy": "REPORT_ONLY", "qc_policy_source": "job"}
        with tempfile.TemporaryDirectory() as tmp:
            resolved = resolve_qc_policy(
                snapshot, {"qc_policy": "WARN"}, workspace_path=tmp
            )
            self.assertEqual(resolved, "WARN")

    def test_invalid_stored_policy_falls_back_to_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            flags_file = Path(tmp) / "bot_system" / "v1_feature_flags.json"
            flags_file.parent.mkdir(parents=True)
            flags_file.write_text(json.dumps({"V1_QC_POLICY": "SOMETHING"}), encoding="utf-8")
            self.assertEqual(resolve_qc_policy(workspace_path=tmp), "BLOCK")


if __name__ == "__main__":
    unittest.main()
