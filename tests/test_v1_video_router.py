import os
import unittest
from unittest import mock
from pathlib import Path
import tempfile
import json

from backend.v1_video_router import (
    VideoMode,
    VideoMetadata,
    get_router_thresholds,
    is_router_enabled,
    route_video,
    create_router_snapshot,
    extract_media_metadata,
)
from backend import job_config_service
from backend.v1_feature_flags import set_feature_flags


class VideoRouterTests(unittest.TestCase):

    def test_workspace_scoped_asr_feature_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            whisper_workspace = root / "whisper"
            qwen_workspace = root / "qwen"
            with mock.patch.dict(os.environ, {"ENABLE_QWEN_ASR": "false", "V1_ASR_MODEL": "whisper_turbo"}, clear=False), \
                 mock.patch("backend.v1_video_router.extract_media_metadata") as mock_extract:
                mock_extract.return_value = VideoMetadata(duration_s=30.0, width=1080, height=1920, fps=30.0)
                set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"}, whisper_workspace)
                set_feature_flags({"V1_ASR_MODEL": "qwen3_asr"}, qwen_workspace)

                whisper = route_video("sample.mp4", workspace_path=whisper_workspace)
                qwen = route_video("sample.mp4", workspace_path=qwen_workspace)

            self.assertEqual(whisper.planned_pipeline["asr_model"], "whisper_turbo")
            self.assertEqual(qwen.planned_pipeline["asr_model"], "qwen3_asr")

    def test_thresholds_configuration(self):
        # Default thresholds: <= 7m (420s) is SHORT, > 7m is LONG
        thresh = get_router_thresholds()
        self.assertEqual(thresh["short_max_seconds"], 420.0)
        self.assertEqual(thresh["medium_max_seconds"], 420.0)
        self.assertEqual(thresh["max_video_minutes"], 60.0)
        self.assertEqual(thresh["max_video_seconds"], 3600.0)

        # Custom env overrides
        with mock.patch.dict(os.environ, {
            "V1_SHORT_MAX_SECONDS": "120.0",
            "V1_MEDIUM_MAX_SECONDS": "480.0",
            "V1_MAX_VIDEO_DURATION_MINUTES": "30.0",
        }):
            custom = get_router_thresholds()
            self.assertEqual(custom["short_max_seconds"], 120.0)
            self.assertEqual(custom["medium_max_seconds"], 480.0)
            self.assertEqual(custom["max_video_minutes"], 30.0)
            self.assertEqual(custom["max_video_seconds"], 1800.0)

    def test_feature_flag(self):
        with mock.patch.dict(os.environ, {"ENABLE_VIDEO_ROUTER": "true"}):
            self.assertTrue(is_router_enabled())
        with mock.patch.dict(os.environ, {"ENABLE_VIDEO_ROUTER": "false"}):
            self.assertFalse(is_router_enabled())
        with mock.patch.dict(os.environ, {"ENABLE_VIDEO_ROUTER": "0"}):
            self.assertFalse(is_router_enabled())

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_route_by_duration(self, mock_extract):
        # Short video (300s ~ 5m < 7m): Uses default short models (BS-RoFormer, Direct ASR, Full OCR)
        mock_extract.return_value = VideoMetadata(duration_s=300.0, width=1280, height=720, fps=30.0)
        dec_short = route_video("dummy.mp4", requested_mode="auto")
        self.assertEqual(dec_short.resolved_mode, VideoMode.SHORT)
        self.assertFalse(dec_short.is_escalated)
        self.assertFalse(dec_short.planned_pipeline["asr_chunking"])
        self.assertEqual(dec_short.planned_pipeline["separation_model"], "roformer")
        self.assertEqual(dec_short.planned_pipeline["ocr_strategy"], "full")
        self.assertEqual(dec_short.planned_pipeline["mixer_mode"], "direct")

        # Long video (480s ~ 8m > 7m): Automatically switches to long models (Demucs v4, Safe Chunking, Smart Skip OCR, Hierarchical Mixer)
        mock_extract.return_value = VideoMetadata(duration_s=480.0, width=1920, height=1080, fps=30.0)
        dec_long = route_video("dummy.mp4", requested_mode="auto")
        self.assertEqual(dec_long.resolved_mode, VideoMode.LONG)
        self.assertTrue(dec_long.planned_pipeline["asr_chunking"])
        self.assertEqual(dec_long.planned_pipeline["asr_chunk_size_s"], 240.0)
        self.assertEqual(dec_long.planned_pipeline["separation_model"], "demucs")
        self.assertEqual(dec_long.planned_pipeline["ocr_strategy"], "smart_skip")
        self.assertEqual(dec_long.planned_pipeline["mixer_mode"], "hierarchical")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_escalation_rules(self, mock_extract):
        # 120s video but 4K resolution (3840x2160) -> Escalated to MEDIUM
        mock_extract.return_value = VideoMetadata(
            duration_s=120.0, width=3840, height=2160, fps=30.0, bitrate_kbps=5000.0
        )
        dec_4k = route_video("dummy.mp4", requested_mode="auto")
        self.assertEqual(dec_4k.resolved_mode, VideoMode.MEDIUM)
        self.assertTrue(dec_4k.is_escalated)
        self.assertTrue(any("4K UHD" in r for r in dec_4k.escalation_reasons))

        # 120s video but 60 fps -> Escalated to MEDIUM
        mock_extract.return_value = VideoMetadata(
            duration_s=120.0, width=1920, height=1080, fps=60.0, bitrate_kbps=5000.0
        )
        dec_fps = route_video("dummy.mp4", requested_mode="auto")
        self.assertEqual(dec_fps.resolved_mode, VideoMode.MEDIUM)
        self.assertTrue(dec_fps.is_escalated)
        self.assertTrue(any("khung hình cao" in r for r in dec_fps.escalation_reasons))

        # 120s video but massive bitrate (20 Mbps) -> Escalated to MEDIUM
        mock_extract.return_value = VideoMetadata(
            duration_s=120.0, width=1920, height=1080, fps=30.0, bitrate_kbps=22000.0
        )
        dec_br = route_video("dummy.mp4", requested_mode="auto")
        self.assertEqual(dec_br.resolved_mode, VideoMode.MEDIUM)
        self.assertTrue(dec_br.is_escalated)

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_manual_mode_override(self, mock_extract):
        # Video is 900s (naturally LONG), but user requests SHORT
        mock_extract.return_value = VideoMetadata(duration_s=900.0, width=1920, height=1080, fps=30.0)
        dec_manual = route_video("dummy.mp4", requested_mode="short")
        self.assertEqual(dec_manual.resolved_mode, VideoMode.SHORT)
        self.assertEqual(dec_manual.requested_mode, "SHORT")
        self.assertFalse(dec_manual.is_escalated)

        # User requests CUSTOM mode
        dec_custom = route_video("dummy.mp4", requested_mode="custom", user_overrides={"asr_chunking": True})
        self.assertEqual(dec_custom.resolved_mode, VideoMode.CUSTOM)
        self.assertTrue(dec_custom.planned_pipeline["asr_chunking"])

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_max_duration_warning(self, mock_extract):
        # Video exceeds 3600s (e.g. 4200s ~ 70m)
        mock_extract.return_value = VideoMetadata(duration_s=4200.0, width=1920, height=1080, fps=30.0)
        dec = route_video("dummy.mp4", requested_mode="auto")
        self.assertTrue(len(dec.warnings) > 0)
        self.assertTrue(any("vượt quá ngưỡng trần" in w for w in dec.warnings))

    def test_immutable_snapshot_in_frozen_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            control_dir = tmp_path / "control"
            control_dir.mkdir(parents=True, exist_ok=True)
            frozen_file = control_dir / "job_frozen_configs.json"

            with mock.patch.object(job_config_service, "CONTROL_DIR", control_dir), \
                 mock.patch.object(job_config_service, "FROZEN_FILE", frozen_file), \
                 mock.patch.object(job_config_service, "WORKSPACE_DIR", tmp_path), \
                 mock.patch("backend.v1_video_router.extract_media_metadata") as mock_extract:

                mock_extract.return_value = VideoMetadata(duration_s=500.0, width=1920, height=1080, fps=30.0)

                # Freeze a job with video_mode="auto"
                snap = job_config_service.freeze_job_config(
                    video_name="test_presentation.mp4",
                    job_id="job_abc123",
                    overrides={"video_mode": "auto"}
                )

                self.assertIsNotNone(snap)
                self.assertIn("routing", snap)
                routing = snap["routing"]
                self.assertIsNotNone(routing)
                self.assertEqual(routing["resolved_mode"], "LONG")
                self.assertTrue(routing["is_immutable"])
                self.assertEqual(snap["effective_config"]["video_mode"], "LONG")

                # Verify lookup by job_id or video_name
                resolved = job_config_service.resolve_job_frozen_config("job_abc123")
                self.assertIsNotNone(resolved)
                self.assertEqual(resolved["snapshot_id"], "job_abc123")
                self.assertEqual(resolved["routing"]["resolved_mode"], "LONG")

                # Verify lookup by video_name
                resolved_by_name = job_config_service.resolve_job_frozen_config("test_presentation.mp4")
                self.assertIsNotNone(resolved_by_name)
                self.assertEqual(resolved_by_name["job_id"], "job_abc123")


if __name__ == "__main__":
    unittest.main()
