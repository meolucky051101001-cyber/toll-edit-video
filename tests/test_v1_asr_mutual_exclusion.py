"""
tests/test_v1_asr_mutual_exclusion.py - Kiểm thử lựa chọn loại trừ lẫn nhau giữa Faster-Whisper và Qwen3-ASR.
Đảm bảo:
1. Mặc định luôn là Faster-Whisper Large-v3 Turbo (siêu nhanh, tiết kiệm VRAM).
2. Khi chọn Qwen3-ASR: Tự động tắt Faster-Whisper, chuyển cờ ENABLE_QWEN_ASR = True và V1_ASR_MODEL = qwen3_asr.
3. Khi chọn Faster-Whisper: Tự động tắt Qwen3-ASR, chuyển cờ ENABLE_QWEN_ASR = False và V1_ASR_MODEL = whisper_turbo.
4. Model Registry giải quyết đúng mô hình ASR hiệu lực tương ứng.
5. Job Config Service và Video Router đóng băng đúng asr_model vào snapshot.
"""

import os
import unittest
from unittest import mock
import srt
from datetime import timedelta

import pytest

from backend.v1_feature_flags import get_feature_flags, set_feature_flags, DEFAULT_FLAGS
from backend.ai.v1_qwen_asr_adapter import is_qwen_asr_enabled, check_qwen_readiness, group_aligned_words
from backend.v1_model_registry import resolve_effective_model, ModelStage
from backend.v1_video_router import resolve_planned_pipeline, VideoMode, VideoMetadata
from backend import job_config_service


class TestAsrMutualExclusion(unittest.TestCase):

    def setUp(self):
        # Reset về mặc định
        set_feature_flags({"ENABLE_QWEN_ASR": False, "V1_ASR_MODEL": "whisper_turbo"})

    def tearDown(self):
        set_feature_flags({"ENABLE_QWEN_ASR": False, "V1_ASR_MODEL": "whisper_turbo"})

    def test_default_is_whisper_turbo(self):
        """Kiểm tra mặc định luôn là Faster-Whisper Turbo."""
        flags = get_feature_flags()
        self.assertFalse(flags.get("ENABLE_QWEN_ASR", False))
        self.assertEqual(flags.get("V1_ASR_MODEL"), "whisper_turbo")
        self.assertFalse(is_qwen_asr_enabled())

        eff_model = resolve_effective_model(ModelStage.ASR)
        self.assertEqual(eff_model.model_id, "whisper_large_v3_turbo")

    def test_selecting_qwen_disables_whisper(self):
        """Khi chọn Qwen3-ASR: Bật Qwen, tắt Whisper."""
        updated = set_feature_flags({"V1_ASR_MODEL": "qwen3_asr"})
        self.assertTrue(updated["ENABLE_QWEN_ASR"])
        self.assertEqual(updated["V1_ASR_MODEL"], "qwen3_asr")
        self.assertTrue(is_qwen_asr_enabled())

        eff_model = resolve_effective_model(ModelStage.ASR)
        self.assertEqual(eff_model.model_id, "qwen3_asr_preview")

    def test_selecting_whisper_disables_qwen(self):
        """Khi chuyển lại về Whisper: Bật Whisper, tắt Qwen."""
        # Đầu tiên bật Qwen
        set_feature_flags({"V1_ASR_MODEL": "qwen3_asr"})
        self.assertTrue(is_qwen_asr_enabled())

        # Sau đó chọn lại Whisper
        updated = set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"})
        self.assertFalse(updated["ENABLE_QWEN_ASR"])
        self.assertEqual(updated["V1_ASR_MODEL"], "whisper_turbo")
        self.assertFalse(is_qwen_asr_enabled())

        eff_model = resolve_effective_model(ModelStage.ASR)
        self.assertEqual(eff_model.model_id, "whisper_large_v3_turbo")

    def test_video_router_planned_pipeline_contains_asr_model(self):
        """Video Router ghi nhận đúng asr_model vào planned_pipeline."""
        # 1. Khi đang dùng mặc định Whisper
        set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"})
        pipe_whisper = resolve_planned_pipeline(VideoMode.SHORT, VideoMetadata(duration_s=120))
        self.assertEqual(pipe_whisper.get("asr_model"), "whisper_turbo")

        # 2. Khi chọn Qwen
        set_feature_flags({"V1_ASR_MODEL": "qwen3_asr"})
        pipe_qwen = resolve_planned_pipeline(VideoMode.SHORT, VideoMetadata(duration_s=120))
        self.assertEqual(pipe_qwen.get("asr_model"), "qwen3_asr")

    def test_group_aligned_words_into_subtitles(self):
        """Kiểm tra thuật toán gom nhóm từ có timestamp thành câu phụ đề chuẩn."""
        class MockWord:
            def __init__(self, text, start, end):
                self.text = text
                self.start_time = start
                self.end_time = end

        words = [
            MockWord("Xin", 0.0, 0.2),
            MockWord("chào", 0.25, 0.5),
            MockWord("các", 0.55, 0.8),
            MockWord("bạn,", 0.85, 1.1),
            MockWord("hôm", 2.0, 2.3),
            MockWord("nay", 2.35, 2.6),
            MockWord("video", 2.65, 3.0),
            MockWord("rất", 3.05, 3.3),
            MockWord("hay.", 3.35, 3.7),
        ]
        segments = group_aligned_words(words, max_chars=30, max_gap=0.8)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "Xin chào các bạn,")
        self.assertAlmostEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[1]["text"], "hôm nay video rất hay.")
        self.assertAlmostEqual(segments[1]["start"], 2.0)
