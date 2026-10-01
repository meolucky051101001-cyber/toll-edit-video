import os
import unittest
from unittest import mock
from pathlib import Path

from backend.v1_video_router import route_video, VideoMode, VideoMetadata, get_router_thresholds
from backend.v1_model_registry import resolve_effective_model, ModelStage


class SevenMinuteRouterSwitchingTests(unittest.TestCase):

    def test_threshold_defaults_to_7_minutes(self):
        thresh = get_router_thresholds()
        self.assertEqual(thresh["short_max_seconds"], 420.0, "Ngưỡng video ngắn phải mặc định là 420s (7 phút)")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_under_7_minutes_uses_short_video_default_models(self, mock_extract):
        # Video 5 phút (300s) <= 7 phút: Phải dùng mô hình mặc định video ngắn ở Tool V1
        mock_extract.return_value = VideoMetadata(
            duration_s=300.0,
            width=1920,
            height=1080,
            fps=30.0,
            bitrate_kbps=4000.0,
            file_size_bytes=150_000_000,
        )
        decision = route_video("video_5m.mp4", requested_mode="auto")
        self.assertEqual(decision.resolved_mode, VideoMode.SHORT)

        pipeline = decision.planned_pipeline
        # 1. Tách vocal: Mô hình BS-RoFormer chất lượng phòng thu SDR 12.97dB
        self.assertEqual(pipeline["separation_model"], "roformer")
        self.assertEqual(pipeline["separator_engine"], "roformer")
        # 2. ASR: Direct single-pass, không chunking
        self.assertFalse(pipeline["asr_chunking"])
        # 3. OCR: Quét toàn bộ (full scan)
        self.assertEqual(pipeline["ocr_strategy"], "full")
        # 4. Mixer: Direct dynamic ducking
        self.assertEqual(pipeline["mixer_mode"], "direct")

        # Kiểm tra Model Registry cấp phát đúng BS-RoFormer
        spec_sep = resolve_effective_model(ModelStage.SEPARATION, video_mode=decision.resolved_mode.value)
        self.assertEqual(spec_sep.model_id, "bs_roformer_sdr12")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_over_7_minutes_automatically_switches_to_long_video_models(self, mock_extract):
        # Video 8 phút (480s) > 7 phút: Tự động chuyển mô hình và cơ chế tối ưu cho video dài
        mock_extract.return_value = VideoMetadata(
            duration_s=480.0,
            width=1920,
            height=1080,
            fps=30.0,
            bitrate_kbps=4000.0,
            file_size_bytes=240_000_000,
        )
        decision = route_video("video_8m.mp4", requested_mode="auto")
        self.assertEqual(decision.resolved_mode, VideoMode.LONG)

        pipeline = decision.planned_pipeline
        # 1. Tách vocal: Tự chuyển sang Demucs v4 Fast GPU (8x nhanh hơn, VRAM cố định, không timeout)
        self.assertEqual(pipeline["separation_model"], "demucs")
        self.assertEqual(pipeline["separator_engine"], "demucs")
        # 2. ASR: Tự kích hoạt Safe ASR Chunking 240s kèm 0.75s overlap chống trôi timestamp & hallucination
        self.assertTrue(pipeline["asr_chunking"])
        self.assertEqual(pipeline["asr_chunk_size_s"], 240.0)
        self.assertEqual(pipeline["asr_overlap_s"], 0.75)
        # 3. OCR: Tự kích hoạt Smart Skip OCR tiết kiệm 60-80% GPU
        self.assertEqual(pipeline["ocr_strategy"], "smart_skip")
        # 4. Mixer: Tự kích hoạt Hierarchical Audio Mixer phân cụm 300s
        self.assertEqual(pipeline["mixer_mode"], "hierarchical")

        # Kiểm tra Model Registry cấp phát đúng Demucs v4 Fast GPU
        spec_sep = resolve_effective_model(ModelStage.SEPARATION, video_mode=decision.resolved_mode.value)
        self.assertEqual(spec_sep.model_id, "demucs_htdemucs")

    @mock.patch("backend.v1_video_router.extract_media_metadata")
    def test_user_manual_override_respected(self, mock_extract):
        # Video 10 phút nhưng người dùng chỉ định thủ công muốn dùng roformer
        mock_extract.return_value = VideoMetadata(duration_s=600.0, width=1920, height=1080, fps=30.0)
        decision = route_video("video_10m.mp4", requested_mode="auto", user_overrides={"separation_mode": "roformer"})
        # Pipeline tôn trọng override của người dùng
        self.assertEqual(decision.planned_pipeline["separation_model"], "roformer")


if __name__ == "__main__":
    unittest.main()
