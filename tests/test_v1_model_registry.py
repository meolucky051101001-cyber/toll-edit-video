import os
import unittest
from unittest import mock
from pathlib import Path
from datetime import timedelta
import srt

from backend.v1_model_registry import (
    ModelStage,
    ModelSpec,
    get_model,
    list_models_for_stage,
    resolve_effective_model,
    log_stage_telemetry,
    MODEL_REGISTRY,
)
from backend.ai.v1_qwen_asr_adapter import (
    is_qwen_asr_enabled,
    check_qwen_readiness,
    run_qwen_asr_benchmark,
)
from backend import ocr_utils


class ModelRegistryTests(unittest.TestCase):

    def test_registry_models_defined(self):
        # 1. Separation
        sep_models = list_models_for_stage(ModelStage.SEPARATION)
        self.assertGreaterEqual(len(sep_models), 2)
        model_ids = [m.model_id for m in sep_models]
        self.assertIn("bs_roformer_sdr12", model_ids)
        self.assertIn("demucs_htdemucs", model_ids)

        # 2. ASR
        asr_models = list_models_for_stage(ModelStage.ASR)
        self.assertGreaterEqual(len(asr_models), 2)
        asr_ids = [m.model_id for m in asr_models]
        self.assertIn("whisper_large_v3_turbo", asr_ids)
        self.assertIn("qwen3_asr_preview", asr_ids)

        # 3. OCR, Translation, TTS, Mixer, Render
        self.assertIsNotNone(get_model("pp_ocrv6_tiny"))
        self.assertIsNotNone(get_model("gemini_flash_translation"))
        self.assertIsNotNone(get_model("capcut_tts_streaming"))
        self.assertIsNotNone(get_model("edge_tts_fallback"))
        self.assertIsNotNone(get_model("pydub_soft_ducking"))
        self.assertIsNotNone(get_model("nvenc_h264"))

    def test_model_readiness_checks(self):
        # Whisper should be ready
        whisper = get_model("whisper_large_v3_turbo")
        self.assertIsNotNone(whisper)
        is_ready, note = whisper.check_readiness()
        self.assertTrue(is_ready)

        # Qwen3-ASR should be not ready by default (flag disabled)
        qwen = get_model("qwen3_asr_preview")
        self.assertIsNotNone(qwen)
        with mock.patch.dict(os.environ, {"ENABLE_QWEN_ASR": "false"}):
            ready_qwen, note_qwen = qwen.check_readiness()
            self.assertFalse(ready_qwen)
            self.assertIn("flag", note_qwen.lower())

    def test_resolve_effective_model_by_mode(self):
        # SHORT mode
        m_sep = resolve_effective_model(ModelStage.SEPARATION, video_mode="SHORT")
        self.assertEqual(m_sep.model_id, "bs_roformer_sdr12")

        # ASR always returns Whisper Large-v3 Turbo by default
        m_asr = resolve_effective_model(ModelStage.ASR, video_mode="SHORT")
        self.assertEqual(m_asr.model_id, "whisper_large_v3_turbo")

        # TTS returns CapCut TTS streaming
        m_tts = resolve_effective_model(ModelStage.TTS, video_mode="SHORT")
        self.assertEqual(m_tts.model_id, "capcut_tts_streaming")

        # User override with valid model
        m_ovr = resolve_effective_model(ModelStage.SEPARATION, video_mode="SHORT", user_override="demucs_htdemucs")
        self.assertEqual(m_ovr.model_id, "demucs_htdemucs")

    def test_qwen_asr_sandbox_isolation(self):
        with mock.patch.dict(os.environ, {"ENABLE_QWEN_ASR": "false"}):
            self.assertFalse(is_qwen_asr_enabled())
            ready, reason = check_qwen_readiness()
            self.assertFalse(ready)
            # Benchmark gracefully returns None and does not crash
            res = run_qwen_asr_benchmark("dummy.wav")
            self.assertIsNone(res)

    def test_stage_telemetry_logging(self):
        whisper = get_model("whisper_large_v3_turbo")
        record = log_stage_telemetry(
            video_name="sample.mp4",
            stage=ModelStage.ASR,
            model_spec=whisper,
            execution_seconds=12.5,
            success=True
        )
        self.assertEqual(record["video_name"], "sample.mp4")
        self.assertEqual(record["stage"], "asr")
        self.assertEqual(record["model_id"], "whisper_large_v3_turbo")
        self.assertEqual(record["execution_seconds"], 12.5)
        self.assertTrue(record["success"])

    def test_smart_skip_ocr_filter_logic(self):
        # Mock 20 subtitles
        segs = [
            srt.Subtitle(
                index=i,
                start=timedelta(seconds=i * 2.0),
                end=timedelta(seconds=i * 2.0 + 1.8),
                content=f"Câu thoại tiếng Trung mẫu số {i} rất dài và rõ ràng",
            )
            for i in range(20)
        ]
        # Attach confidence
        for s in segs:
            s.confidence = 0.95
            s.avg_logprob = -0.10

        # Segment 5 is low confidence
        segs[5].confidence = 0.65
        # Segment 10 is very short
        segs[10].content = "你好"

        # Under smart_skip on a 250s video (> 180s), verify that high-confidence non-boundary cues are skipped
        # Create a mock for cv2.VideoCapture
        mock_cap = mock.MagicMock()
        mock_cap.isOpened.return_value = True
        mock_cap.get.side_effect = lambda prop: 250.0 if prop == 7 else (1920 if prop == 3 else (1080 if prop == 4 else 30.0))
        mock_cap.read.return_value = (False, None)  # Return no frame to stop loop safely

        with mock.patch("cv2.VideoCapture", return_value=mock_cap), \
             mock.patch.object(ocr_utils, "_readtext_batch", return_value=[]):
            # Test with ocr_strategy="smart_skip"
            blocks, w, h, _ = ocr_utils.perform_video_ocr(
                "fixture.mp4", srt_segments=segs, ocr_strategy="smart_skip"
            )
            # Verify it did not crash and ran safely
            self.assertEqual(w, 1920)
            self.assertEqual(h, 1080)


if __name__ == "__main__":
    unittest.main()
