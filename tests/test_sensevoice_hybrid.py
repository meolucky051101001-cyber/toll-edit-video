"""
Kiểm thử toàn diện cho SenseVoice-Small (INT8 ONNX) và Bộ Hợp Nhất Hybrid (Faster-Whisper + SenseVoice).
Bao gồm:
1. Kiểm tra readiness & trọng số ONNX thực tế.
2. Kiểm tra suy luận ONNX thật trên file âm thanh tiếng Trung (zh.wav).
3. Kiểm tra 4 lớp của Bộ Hợp Nhất Hybrid (refine_whisper_with_sensevoice):
   - Lớp 1: Lọc câu ảo giác khi SenseVoice báo không có tiếng người nói (<|BGM|> / <|nospeech|>).
   - Lớp 2: Nắn mốc thời gian đầu/cuối câu (CTC 60ms boundary snapping) bám sát khẩu hình.
   - Lớp 3: Tách câu quá dài tại khoảng nghỉ âm học thực tế.
   - Lớp 4: Khôi phục câu thoại ngắn bị Whisper bỏ sót (Gap Rescue).
4. Kiểm tra tích hợp với Feature Flags, Job Policy, Video Router, Model Registry và Job Config Service.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from ai.v1_sensevoice_adapter import (
    DEFAULT_SENSEVOICE_DIR,
    analyze_audio_sensevoice,
    check_sensevoice_readiness,
    group_sensevoice_tokens_to_segments,
    refine_whisper_with_sensevoice,
    transcribe_audio_sensevoice,
)
from v1_feature_flags import get_feature_flags, set_feature_flags
from v1_job_policy import apply_job_policy
from v1_model_registry import ModelStage, resolve_effective_model
from v1_video_router import VideoMetadata, VideoMode, resolve_planned_pipeline


class TestSenseVoiceHybridASR(unittest.TestCase):
    def test_sensevoice_readiness_and_real_onnx_inference(self):
        """Kiểm tra readiness và chạy suy luận thực tế trên zh.wav bằng SenseVoice ONNX INT8."""
        ready, reason = check_sensevoice_readiness(ignore_flag=True)
        self.assertTrue(ready, f"SenseVoice phải sẵn sàng: {reason}")

        sample_wav = DEFAULT_SENSEVOICE_DIR / "test_wavs" / "zh.wav"
        if sample_wav.is_file():
            analysis = analyze_audio_sensevoice(sample_wav)
            self.assertGreater(len(analysis["tokens"]), 5)
            self.assertEqual(analysis["windows"][0]["lang_tag"], "<|zh|>")
            self.assertEqual(analysis["windows"][0]["event_tag"], "<|Speech|>")

            with tempfile.TemporaryDirectory() as tmp:
                srt_out = Path(tmp) / "sv_out.srt"
                subs = transcribe_audio_sensevoice(sample_wav, srt_out)
                self.assertGreaterEqual(len(subs), 1)
                self.assertTrue(srt_out.is_file())
                joined = "".join(s.content for s in subs)
                self.assertIn("开放时间", joined)

    def test_hybrid_refiner_4_layers_with_mocked_sv_analysis(self):
        """Kiểm tra cả 4 lớp tinh chỉnh của refine_whisper_with_sensevoice."""
        fake_sv_data = {
            "duration_s": 16.0,
            "elapsed_s": 0.12,
            "windows": [
                {
                    "offset_s": 0.0,
                    "duration_s": 16.0,
                    "lang_tag": "<|zh|>",
                    "emotion_tag": "<|NEUTRAL|>",
                    "event_tag": "<|Speech|>",
                }
            ],
            "tokens": [
                # Câu 1 thực tế nói từ 0.72s -> 2.10s (Whisper bị mở sớm ở 0.30s và kéo dài tới 2.55s)
                {"text": "今", "start": 0.72, "end": 0.90, "prob": 0.96},
                {"text": "天", "start": 0.90, "end": 1.14, "prob": 0.95},
                {"text": "天", "start": 1.14, "end": 1.38, "prob": 0.94},
                {"text": "气", "start": 1.38, "end": 1.62, "prob": 0.97},
                {"text": "真", "start": 1.62, "end": 1.86, "prob": 0.95},
                {"text": "好", "start": 1.86, "end": 2.10, "prob": 0.98},
                # Khoảng 2.6s -> 4.2s hoàn toàn không có token (nhạc dạo), nhưng Whisper bị hallucinate "谢谢观看"
                # Khoảng 5.0s -> 6.1s: Câu ngắn bị Whisper bỏ sót (Gap Rescue)
                {"text": "太", "start": 5.10, "end": 5.34, "prob": 0.92},
                {"text": "棒", "start": 5.34, "end": 5.64, "prob": 0.91},
                {"text": "了", "start": 5.64, "end": 5.94, "prob": 0.93},
            ],
        }

        whisper_input = [
            # Câu 1 bị lệch biên (0.30 -> 2.55 thay vì ~0.68 -> ~2.16)
            {"start": 0.30, "end": 2.55, "text": "今天天气真好。"},
            # Câu 2 là hallucination trong lúc nhạc nền không có lời
            {"start": 2.80, "end": 4.10, "text": "谢谢观看"},
        ]

        target_mod = sys.modules[refine_whisper_with_sensevoice.__module__]
        with mock.patch.object(target_mod, "analyze_audio_sensevoice", return_value=fake_sv_data):
            refined = refine_whisper_with_sensevoice(whisper_input, "dummy.wav")

        # Câu "谢谢观看" phải bị lọc bỏ, câu "太棒了" phải được cứu, tổng cộng có 2 câu
        self.assertEqual(len(refined), 2)
        self.assertEqual(refined[0]["text"], "今天天气真好。")
        # Điểm đầu đã được nắn từ 0.30s về sát 0.68s (0.72 - 0.04)
        self.assertAlmostEqual(refined[0]["start"], 0.68, places=2)
        # Điểm cuối đã được nắn từ 2.55s về sát 2.16s (2.10 + 0.06)
        self.assertAlmostEqual(refined[0]["end"], 2.16, places=2)

        # Câu được cứu ở khoảng trống
        self.assertEqual(refined[1]["text"], "太棒了")
        self.assertAlmostEqual(refined[1]["start"], 5.10, places=2)

    def test_feature_flags_and_routing_support_hybrid_and_sensevoice(self):
        """Kiểm tra Feature Flags, Video Router, Job Policy và Model Registry."""
        with tempfile.TemporaryDirectory() as tmp:
            updated = set_feature_flags({"V1_ASR_MODEL": "whisper_sensevoice"}, tmp)
            self.assertEqual(updated["V1_ASR_MODEL"], "whisper_sensevoice")
            self.assertFalse(updated["ENABLE_QWEN_ASR"])

            meta = VideoMetadata(duration_s=45.0, width=1080, height=1920, fps=30.0)
            pipe = resolve_planned_pipeline(VideoMode.SHORT, meta, workspace_path=tmp)
            self.assertEqual(pipe["asr_model"], "whisper_sensevoice")

            routing = {
                "requested_mode": "AUTO",
                "resolved_mode": "SHORT",
                "metadata": {"duration_s": 45.0},
                "thresholds": {"short_max_seconds": 420.0},
                "planned_pipeline": pipe,
            }
            applied = apply_job_policy(routing, updated)
            self.assertEqual(applied["planned_pipeline"]["asr_model"], "whisper_sensevoice")

            spec = resolve_effective_model(ModelStage.ASR, "SHORT", user_override="whisper_sensevoice")
            self.assertEqual(spec.model_id, "whisper_sensevoice_hybrid")


if __name__ == "__main__":
    unittest.main()
