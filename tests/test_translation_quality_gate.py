import unittest
from backend.ai.translation import (
    ensure_speech_pauses_and_entity,
    validate_translation_batch_quality,
)


class TranslationQualityGateTests(unittest.TestCase):
    def test_cjk_leak_rejection(self):
        sources = ["你好世界"]
        translated = ["Chào bạn 世界."]
        valid, reasons = validate_translation_batch_quality(sources, translated, strict=True)
        self.assertFalse(valid)
        self.assertTrue(any("CJK" in r or "chữ Hán" in r for r in reasons))

    def test_case_1_video_with_athich_character_via_glossary(self):
        """Case 1: Video with character 'A Thích' configured via glossary / entity_map."""
        sources = ["阿特跑了", "阿特正在准备食物。"]
        translated = ["a thích đã chạy thoát rồi", "a thích đang chuẩn bị thức ăn."]
        glossary = {"阿特": "A Thích"}
        fixed = ensure_speech_pauses_and_entity(sources, translated, glossary=glossary)
        self.assertEqual(fixed[0], "A Thích đã chạy thoát rồi")
        self.assertEqual(fixed[1], "A Thích đang chuẩn bị thức ăn.")
        valid, reasons = validate_translation_batch_quality(sources, fixed, glossary=glossary, strict=True)
        self.assertTrue(valid, f"Expected valid, got: {reasons}")

    def test_case_2_video_genuinely_about_assassins_no_athich_replacement(self):
        """Case 2: Video genuinely about assassins/sát thủ/thích khách.
        Verifies that 'sát thủ' and 'thích khách' are NOT replaced with 'A Thích'."""
        sources = ["刺客在黑夜中潜行", "两名刺客正在等待目标。"]
        translated = ["Thích khách đang lẻn đi trong đêm tối", "Hai tên sát thủ đang chờ đợi mục tiêu."]
        # No glossary provided, ordinary term should be retained
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertIn("Thích khách", fixed[0])
        self.assertIn("sát thủ", fixed[1])
        self.assertNotIn("A Thích", fixed[0])
        self.assertNotIn("A Thích", fixed[1])
        valid, reasons = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid, f"Expected valid without A Thích, got: {reasons}")

    def test_case_3_video_with_different_proper_name(self):
        """Case 3: Video with different character name (e.g. 'Tiểu Bạch')."""
        sources = ["小白跑了", "小白正在准备行李。"]
        translated = ["tiểu bạch đã chạy thoát rồi", "tiểu bạch đang chuẩn bị hành lý."]
        glossary = {"小白": "Tiểu Bạch"}
        fixed = ensure_speech_pauses_and_entity(sources, translated, glossary=glossary)
        self.assertEqual(fixed[0], "Tiểu Bạch đã chạy thoát rồi")
        self.assertEqual(fixed[1], "Tiểu Bạch đang chuẩn bị hành lý.")
        valid, reasons = validate_translation_batch_quality(sources, fixed, glossary=glossary, strict=True)
        self.assertTrue(valid, f"Expected valid, got: {reasons}")

    def test_case_4_video_with_no_proper_names(self):
        """Case 4: Video with common terms, no proper names."""
        sources = ["木匠在修椅子", "医生正在检查病人。"]
        translated = ["Người thợ mộc đang sửa chiếc ghế", "Bác sĩ đang khám cho bệnh nhân."]
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertEqual(fixed[0], "Người thợ mộc đang sửa chiếc ghế")
        self.assertEqual(fixed[1], "Bác sĩ đang khám cho bệnh nhân.")
        valid, reasons = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid, f"Expected valid, got: {reasons}")

    def test_sentence_boundary_and_asr_split_no_mid_sentence_period(self):
        """Continuous sentence split across segments by ASR.
        Must NOT insert periods on incomplete clauses."""
        sources = [
            "当一阵剧烈的震颤",
            "将它与迁徙的族群隔开",
            "它落入了死火山的石缝中。",
        ]
        translated = [
            "Khi một trận rung chấn kinh hoàng",
            "chia cắt nó khỏi bầy đàn đang di cư",
            "nó rơi vào khe đá của ngọn núi lửa đã tắt.",
        ]
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        # Segments 0 and 1 must NOT end with a period
        self.assertFalse(fixed[0].endswith("."))
        self.assertFalse(fixed[1].endswith("."))
        # Segment 2 ends with a period because source ended with '。'
        self.assertTrue(fixed[2].endswith("."))
        valid, reasons = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid, f"Expected valid, got: {reasons}")

    def test_multi_clause_comma_enforcement(self):
        sources = ["一阵剧烈的震颤，将它与迁徙的族群隔开。"]
        translated = ["Một trận rung chấn kinh hoàng chia cắt nó khỏi đàn đang di cư."]
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertIn(",", fixed[0])
        valid_fixed, reasons = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid_fixed, f"Expected valid, got: {reasons}")

    def test_short_sentence_does_not_force_comma(self):
        sources = ["你好。"]
        translated = ["Xin chào."]
        valid, reasons = validate_translation_batch_quality(sources, translated, strict=True)
        self.assertTrue(valid)
        self.assertEqual(reasons, [])

    def test_ellipses_rejection_and_normalization(self):
        sources = ["等等..."]
        translated = ["Đợi đã..."]
        valid, reasons = validate_translation_batch_quality(sources, translated, strict=True)
        self.assertFalse(valid)
        self.assertTrue(any("ba chấm" in r for r in reasons))

        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertNotIn("...", fixed[0])
        self.assertNotIn("…", fixed[0])


if __name__ == "__main__":
    unittest.main()
