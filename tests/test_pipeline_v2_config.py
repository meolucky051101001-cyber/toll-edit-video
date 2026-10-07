import unittest

from backend.ai.model_policy import RuntimeModelPolicy
from backend.pipeline_v2.config import PipelineMode, PipelineSettings, QCGatePolicy


class PipelineConfigurationTests(unittest.TestCase):
    def test_render_and_quality_settings_load_from_environment(self):
        settings = PipelineSettings.from_env(
            {
                "PIPELINE_MODE": "v2",
                "QC_GATE_POLICY": "warn",
                "ATEMPO_MIN": "0.95",
                "ATEMPO_MAX": "1.35",
                "MIXER_MAX_INPUTS_PER_PASS": "32",
            }
        )

        self.assertIs(settings.mode, PipelineMode.V2)
        self.assertIs(settings.qc_gate_policy, QCGatePolicy.WARN)
        self.assertEqual(settings.atempo_min, 0.95)
        self.assertEqual(settings.atempo_max, 1.35)
        self.assertEqual(settings.mixer_max_inputs_per_pass, 32)

    def test_invalid_render_limits_fail_fast(self):
        with self.assertRaisesRegex(ValueError, "atempo_min"):
            PipelineSettings.from_env({"ATEMPO_MIN": "1.2", "ATEMPO_MAX": "1.1"})

    def test_quality_model_defaults_are_not_changed_by_resource_profiles(self):
        policy = RuntimeModelPolicy.from_env({})

        self.assertEqual(policy.separator_model, "model_bs_roformer_ep_317_sdr_12.9755.ckpt")
        self.assertEqual(policy.qwen_asr_model, "Qwen/Qwen3-ASR-0.6B")
        self.assertEqual(policy.qwen_aligner_model, "Qwen/Qwen3-ForcedAligner-0.6B")
        self.assertEqual(policy.whisper_model, "large-v3")
        self.assertEqual(policy.paddle_ocr_version, "PP-OCRv6")
        self.assertEqual(policy.gemini_model, "gemini-3.8-flash")


if __name__ == "__main__":
    unittest.main()
