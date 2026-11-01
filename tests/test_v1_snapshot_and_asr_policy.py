import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import job_config_service
from backend.v1_checkpoint import ManifestManager
from backend.v1_feature_flags import set_feature_flags
from backend.v1_orchestrator import V1Orchestrator
from backend.ai import transcription, v1_sensevoice_adapter


class SnapshotAndAsrPolicyTests(unittest.TestCase):
    def test_freeze_without_overrides_still_creates_router_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            control = workspace / "control"
            frozen_file = control / "job_frozen_configs.json"
            with patch.object(job_config_service, "WORKSPACE_DIR", workspace), \
                 patch.object(job_config_service, "CONTROL_DIR", control), \
                 patch.object(job_config_service, "FROZEN_FILE", frozen_file), \
                 patch("backend.v1_video_router.extract_media_metadata") as probe:
                from backend.v1_video_router import VideoMetadata

                probe.return_value = VideoMetadata(duration_s=30.0, width=1080, height=1920, fps=30.0)
                snapshot = job_config_service.freeze_job_config(
                    "sample.mp4", job_id="job-1", workspace_path=workspace
                )

            self.assertIsNotNone(snapshot["routing"])
            self.assertEqual(snapshot["effective_config"]["video_mode"], "SHORT")
            self.assertTrue(frozen_file.is_file())

    def test_explicit_asr_model_skips_sensevoice_only_path(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(transcription, "_transcribe_once", return_value=[]) as whisper, \
             patch.object(v1_sensevoice_adapter, "transcribe_audio_sensevoice") as sv_small, \
             patch.object(v1_sensevoice_adapter, "check_sensevoice_readiness", return_value=(False, "off")):
            result = transcription.extract_subtitles_whisper(
                "audio.wav", str(Path(tmp) / "out.srt"), asr_model="whisper_turbo"
            )

        self.assertEqual(result, [])
        whisper.assert_called_once()
        sv_small.assert_not_called()

    def test_legacy_qwen_frozen_snapshot_is_migrated_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            control = workspace / "control"
            control.mkdir()
            (control / "job_frozen_configs.json").write_text(
                json.dumps(
                    {
                        "job-legacy": {
                            "effective_config": {
                                "asr_model": "qwen3_asr",
                                "planned_pipeline": {"asr_model": "qwen3_asr"},
                            },
                            "overrides": {"asr_model": "qwen3_asr"},
                            "routing": {"planned_pipeline": {"asr_model": "qwen3_asr"}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            snapshot = job_config_service.get_frozen_config(
                "job-legacy", workspace_path=workspace
            )

        self.assertEqual(snapshot["effective_config"]["asr_model"], "whisper_sensevoice")
        self.assertEqual(
            snapshot["effective_config"]["planned_pipeline"]["asr_model"],
            "whisper_sensevoice",
        )
        self.assertEqual(snapshot["overrides"]["asr_model"], "whisper_sensevoice")
        self.assertEqual(
            snapshot["routing"]["planned_pipeline"]["asr_model"], "whisper_sensevoice"
        )

    def test_frozen_snapshots_are_isolated_by_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ):
            root = Path(tmp)
            whisper_workspace = root / "whisper"
            sensevoice_workspace = root / "sensevoice"
            set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"}, whisper_workspace)
            set_feature_flags({"V1_ASR_MODEL": "sensevoice_small"}, sensevoice_workspace)

            with patch("backend.v1_video_router.is_router_enabled", return_value=False):
                whisper_snapshot = job_config_service.freeze_job_config(
                    "same.mp4", job_id="same-job", workspace_path=whisper_workspace
                )
                sv_snapshot = job_config_service.freeze_job_config(
                    "same.mp4", job_id="same-job", workspace_path=sensevoice_workspace
                )

            self.assertEqual(whisper_snapshot["effective_config"]["asr_model"], "whisper_turbo")
            self.assertEqual(sv_snapshot["effective_config"]["asr_model"], "sensevoice_small")
            self.assertNotEqual(
                whisper_workspace / "control" / "job_frozen_configs.json",
                sensevoice_workspace / "control" / "job_frozen_configs.json",
            )
            self.assertEqual(
                job_config_service.get_frozen_config("same-job", workspace_path=whisper_workspace)["effective_config"]["asr_model"],
                "whisper_turbo",
            )
            self.assertEqual(
                job_config_service.get_frozen_config("same-job", workspace_path=sensevoice_workspace)["effective_config"]["asr_model"],
                "sensevoice_small",
            )
            self.assertEqual(
                job_config_service.get_frozen_config(str(sensevoice_workspace / "same.mp4"))["effective_config"]["asr_model"],
                "sensevoice_small",
            )

    def test_failed_feature_flag_write_does_not_update_environment_or_leave_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"V1_ASR_MODEL": "unchanged"},
        ):
            with patch("backend.v1_feature_flags.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(RuntimeError):
                    set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"}, tmp)

            self.assertEqual(os.environ["V1_ASR_MODEL"], "unchanged")
            flag_dir = Path(tmp) / "bot_system"
            self.assertEqual(list(flag_dir.glob("*.tmp.*")), [])

    def test_existing_manifest_plan_is_reused_without_rerouting(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            video_path = workspace / "sample.mp4"
            video_path.write_bytes(b"frozen video")
            orchestrator = V1Orchestrator(workspace_path=workspace)
            manager = ManifestManager(workspace)
            routing = {"planned_pipeline": {"asr_model": "whisper_sensevoice"}, "resolved_mode": "SHORT"}
            manager.get_or_create_manifest(
                job_id="job-frozen",
                video_path=video_path,
                video_mode="SHORT",
                effective_config={
                    "planned_pipeline": {"asr_model": "whisper_sensevoice"},
                    "planned_models": {"asr": {"model_id": "whisper_large_v3_turbo"}},
                    "asr_model": "whisper_turbo",
                },
                routing_snapshot=routing,
            )

            with patch("backend.v1_orchestrator.create_router_snapshot") as router, \
                 patch("backend.v1_orchestrator.get_feature_flags", side_effect=AssertionError("read current flags")), \
                 patch("backend.job_config_service.freeze_job_config") as freeze:
                plan = orchestrator.plan_job(video_path, "job-frozen")

            router.assert_not_called()
            freeze.assert_not_called()
            self.assertEqual(plan["planned_pipeline"]["asr_model"], "whisper_sensevoice")
            self.assertEqual(plan["planned_models"]["asr"]["model_id"], "whisper_large_v3_turbo")

            model_only_video = workspace / "model-only.mp4"
            model_only_video.write_bytes(b"another frozen video")
            manager.get_or_create_manifest(
                job_id="job-model-only",
                video_path=model_only_video,
                video_mode="SHORT",
                effective_config={
                    "planned_models": {"asr": {"model_id": "qwen3_asr_preview"}},
                    "asr_model": "whisper_turbo",
                },
            )
            model_only_plan = orchestrator.plan_job(model_only_video, "job-model-only")
            self.assertEqual(model_only_plan["planned_pipeline"]["asr_model"], "whisper_sensevoice")


if __name__ == "__main__":
    unittest.main()
