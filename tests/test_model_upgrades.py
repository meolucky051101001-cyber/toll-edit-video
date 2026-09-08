import tempfile
import unittest
from pathlib import Path

from backend.ai.model_policy import RuntimeModelPolicy
from backend.ai.transcription import _aligned_units_to_segments
from backend.model_workers.model_runtime_worker import (
    _classify_separator_outputs,
    _paddle_payload,
)


class ModelPolicyTests(unittest.TestCase):
    def test_production_defaults_use_current_strong_models(self):
        policy = RuntimeModelPolicy.from_env({}, project_root=Path("C:/missing"))
        self.assertEqual(policy.separator_model, "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
        self.assertEqual(policy.demucs_primary_model, "htdemucs_ft")
        self.assertEqual(policy.qwen_asr_model, "Qwen/Qwen3-ASR-0.6B")
        self.assertEqual(policy.qwen_aligner_model, "Qwen/Qwen3-ForcedAligner-0.6B")
        self.assertEqual(policy.paddle_ocr_version, "PP-OCRv6")
        self.assertEqual(policy.gemini_model, "gemini-3.8-flash")
        self.assertEqual(policy.gemini_candidates[0], "gemini-3.8-flash")
        self.assertIn("gemini-3.7-flash", policy.gemini_candidates)
        self.assertNotIn("deepseek-v4", policy.deepseek_candidates)

    def test_invalid_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            RuntimeModelPolicy.from_env(
                {"ASR_BACKEND": "imaginary"}, project_root=Path("C:/missing")
            )

    def test_fast_profile_selects_v1_models_without_disabling_v2_features(self):
        policy = RuntimeModelPolicy.from_env({"MODEL_SPEED_PROFILE": "fast"})
        self.assertEqual((policy.asr_backend, policy.whisper_model), ("whisper", "large-v3"))
        self.assertEqual((policy.separator_backend, policy.demucs_primary_model), ("demucs", "htdemucs"))
        self.assertEqual(policy.demucs_fallback_model, "htdemucs_ft")
        self.assertEqual(policy.paddle_ocr_version, "PP-OCRv6")
        self.assertNotEqual(policy.fingerprint_payload(), RuntimeModelPolicy.from_env({}).fingerprint_payload())
        override = RuntimeModelPolicy.from_env({"MODEL_SPEED_PROFILE": "fast", "ASR_BACKEND": "qwen3"})
        self.assertEqual(override.asr_backend, "qwen3")
        with self.assertRaises(ValueError):
            RuntimeModelPolicy.from_env({"MODEL_SPEED_PROFILE": "typo"})


class QwenAlignmentTests(unittest.TestCase):
    def test_chinese_units_join_without_spaces_and_split_on_punctuation(self):
        grouped = _aligned_units_to_segments(
            [
                {"text": "你", "start": 0.10, "end": 0.25},
                {"text": "好", "start": 0.25, "end": 0.45},
                {"text": "。", "start": 0.45, "end": 0.50},
                {"text": "再", "start": 1.60, "end": 1.80},
                {"text": "见", "start": 1.80, "end": 2.05},
            ]
        )
        self.assertEqual([item["text"] for item in grouped], ["你好。", "再见"])
        self.assertEqual(grouped[0]["start"], 0.10)
        self.assertEqual(grouped[1]["end"], 2.05)

    def test_long_speech_is_bounded_into_subtitle_windows(self):
        units = [
            {"text": "字", "start": index * 0.25, "end": index * 0.25 + 0.20}
            for index in range(40)
        ]
        grouped = _aligned_units_to_segments(units)
        self.assertGreater(len(grouped), 1)
        self.assertTrue(all(item["end"] - item["start"] <= 6.25 for item in grouped))


class RuntimeWorkerParsingTests(unittest.TestCase):
    def test_separator_outputs_are_identified_by_stem_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vocals = root / "clip_(Vocals)_model.wav"
            instrumental = root / "clip_(Instrumental)_model.wav"
            vocals.write_bytes(b"voice")
            instrumental.write_bytes(b"music")
            result = _classify_separator_outputs(
                [vocals.name, instrumental.name], str(root)
            )
            self.assertEqual(Path(result["vocals_path"]), vocals.resolve())
            self.assertEqual(Path(result["background_path"]), instrumental.resolve())

    def test_paddle_result_json_wrapper_is_unwrapped(self):
        class FakeResult:
            json = '{"res":{"rec_texts":["字幕"],"rec_scores":[0.99]}}'

        payload = _paddle_payload(FakeResult())
        self.assertEqual(payload["rec_texts"], ["字幕"])


if __name__ == "__main__":
    unittest.main()
