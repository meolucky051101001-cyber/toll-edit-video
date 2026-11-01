"""
tests/test_v1_asr_mutual_exclusion.py - Kiểm thử lựa chọn ASR của Tool V1.
Đảm bảo:
1. Mặc định là Hybrid Faster-Whisper + SenseVoice.
2. Chọn whisper_turbo / whisper_sensevoice / sensevoice_small lưu đúng vào feature flags.
3. Model Registry giải quyết đúng mô hình ASR hiệu lực.
4. Video Router ghi nhận đúng asr_model vào planned_pipeline.
5. Giá trị Qwen3-ASR cũ được migrate sang whisper_sensevoice thay vì raise.
"""

import json
import tempfile
import unittest
from pathlib import Path

from backend.v1_feature_flags import (
    get_feature_flags,
    migrate_legacy_asr_model,
    set_feature_flags,
)
from backend.v1_model_registry import resolve_effective_model, ModelStage
from backend.v1_video_router import resolve_planned_pipeline, VideoMode, VideoMetadata


class TestAsrSelection(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_whisper_sensevoice(self):
        """Mặc định luôn là Hybrid Faster-Whisper + SenseVoice."""
        flags = get_feature_flags(self.workspace)
        self.assertNotIn("ENABLE_QWEN_ASR", flags)
        self.assertEqual(flags.get("V1_ASR_MODEL"), "whisper_sensevoice")

    def test_explicit_model_overrides_resolve_to_registry_specs(self):
        """User override chọn đúng model registry cho từng lựa chọn ASR."""
        cases = {
            "whisper_turbo": "whisper_large_v3_turbo",
            "whisper_sensevoice": "whisper_sensevoice_hybrid",
            "hybrid": "whisper_sensevoice_hybrid",
            "sensevoice_small": "sensevoice_small",
        }
        for override, expected in cases.items():
            with self.subTest(override=override):
                eff_model = resolve_effective_model(ModelStage.ASR, user_override=override)
                self.assertEqual(eff_model.model_id, expected)

    def test_selected_model_is_persisted_in_workspace_flags(self):
        """Feature flags lưu đúng lựa chọn và không còn cờ Qwen cũ."""
        updated = set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"}, self.workspace)
        self.assertEqual(updated["V1_ASR_MODEL"], "whisper_turbo")
        self.assertNotIn("ENABLE_QWEN_ASR", updated)
        self.assertEqual(get_feature_flags(self.workspace)["V1_ASR_MODEL"], "whisper_turbo")

        updated = set_feature_flags({"V1_ASR_MODEL": "sensevoice_small"}, self.workspace)
        self.assertEqual(updated["V1_ASR_MODEL"], "sensevoice_small")

    def test_video_router_planned_pipeline_contains_asr_model(self):
        """Video Router ghi nhận đúng asr_model vào planned_pipeline."""
        set_feature_flags({"V1_ASR_MODEL": "whisper_sensevoice"}, self.workspace)
        pipe_hybrid = resolve_planned_pipeline(
            VideoMode.SHORT, VideoMetadata(duration_s=120), workspace_path=self.workspace
        )
        self.assertEqual(pipe_hybrid.get("asr_model"), "whisper_sensevoice")

        set_feature_flags({"V1_ASR_MODEL": "whisper_turbo"}, self.workspace)
        pipe_turbo = resolve_planned_pipeline(
            VideoMode.SHORT, VideoMetadata(duration_s=120), workspace_path=self.workspace
        )
        self.assertEqual(pipe_turbo.get("asr_model"), "whisper_turbo")

    def test_legacy_qwen_setting_is_migrated_instead_of_raising(self):
        """Cấu hình cũ gửi qwen3_asr được map sang whisper_sensevoice, không lỗi."""
        updated = set_feature_flags({"V1_ASR_MODEL": "qwen3_asr"}, self.workspace)
        self.assertEqual(updated["V1_ASR_MODEL"], "whisper_sensevoice")

        flags_file = self.workspace / "bot_system" / "v1_feature_flags.json"
        flags_file.write_text(
            json.dumps({"ENABLE_QWEN_ASR": True, "V1_ASR_MODEL": "qwen3_asr"}),
            encoding="utf-8",
        )
        flags = get_feature_flags(self.workspace)
        self.assertNotIn("ENABLE_QWEN_ASR", flags)
        self.assertEqual(flags["V1_ASR_MODEL"], "whisper_sensevoice")

    def test_migration_helper_leaves_current_models_untouched(self):
        for value in ("whisper_turbo", "whisper_sensevoice", "sensevoice_small"):
            self.assertEqual(migrate_legacy_asr_model(value), value)
        self.assertEqual(migrate_legacy_asr_model("qwen3_asr"), "whisper_sensevoice")
        self.assertEqual(migrate_legacy_asr_model("qwen3_asr_preview"), "whisper_sensevoice")


if __name__ == "__main__":
    unittest.main()
