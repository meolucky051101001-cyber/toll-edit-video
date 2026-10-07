import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.ai import transcription
from backend.ai.v1_model_policy import V1ModelPolicy


class V1ModelPolicyTests(unittest.TestCase):
    def test_fast_models_are_the_v1_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = V1ModelPolicy.from_env({}, project_root=Path(temporary))

        self.assertEqual(
            policy.whisper_candidates, ("large-v3-turbo", "large-v3")
        )
        self.assertEqual(policy.demucs_model, "htdemucs")
        self.assertEqual(policy.ocr_backend, "auto")
        self.assertEqual(policy.paddle_detection_model, "PP-OCRv6_tiny_det")
        self.assertEqual(policy.paddle_recognition_model, "PP-OCRv6_tiny_rec")
        self.assertEqual(policy.paddle_engine, "onnxruntime")

    def test_environment_overrides_do_not_duplicate_fallbacks(self):
        policy = V1ModelPolicy.from_env(
            {
                "V1_WHISPER_MODEL": "large-v3",
                "V1_WHISPER_FALLBACK_MODEL": "large-v3-turbo",
                "V1_DEMUCS_MODEL": "mdx_extra_q",
                "V1_OCR_BACKEND": "easyocr",
            }
        )

        self.assertEqual(policy.whisper_candidates, ("large-v3", "large-v3-turbo"))
        self.assertEqual(policy.demucs_model, "mdx_extra_q")
        self.assertEqual(policy.ocr_backend, "easyocr")

    def test_invalid_ocr_backend_fails_early(self):
        with self.assertRaisesRegex(ValueError, "Unsupported V1 backend"):
            V1ModelPolicy.from_env({"V1_OCR_BACKEND": "unknown"})

    def test_asr_uses_large_v3_when_turbo_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "subtitles.srt"
            policy = SimpleNamespace(
                whisper_candidates=("large-v3-turbo", "large-v3"),
                model_cache_directory=temporary,
            )
            segment = [{"start": 1.0, "end": 2.0, "text": "你好"}]
            with mock.patch.object(
                transcription, "current_v1_model_policy", return_value=policy
            ), mock.patch.object(
                transcription,
                "_transcribe_once",
                side_effect=[RuntimeError("turbo unavailable"), segment],
            ) as transcribe_once:
                result = transcription.extract_subtitles_whisper(
                    "speech.wav", str(output)
                )

        self.assertEqual(result[0].content, "你好")
        self.assertEqual(
            [call.args[1] for call in transcribe_once.call_args_list],
            ["large-v3-turbo", "large-v3"],
        )


if __name__ == "__main__":
    unittest.main()
