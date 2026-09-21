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

    def test_forbidden_entity_variant_rejection(self):
        sources = ["阿特跑了"]
        # "sát thủ" instead of "A Thích"
        translated = ["Sát thủ đã chạy thoát rồi."]
        valid, reasons = validate_translation_batch_quality(sources, translated, strict=True)
        self.assertFalse(valid)
        self.assertTrue(any("A Thích" in r for r in reasons))

    def test_ensure_speech_pauses_and_entity_normalizes_entity(self):
        sources = ["阿特跑了", "刺客在等待"]
        translated = ["sát thủ đã chạy thoát rồi", "assassin đang đợi."]
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertEqual(fixed[0], "A Thích đã chạy thoát rồi.")
        self.assertEqual(fixed[1], "A Thích đang đợi.")

    def test_multi_clause_comma_enforcement(self):
        sources = ["一阵剧烈的震颤，将它与迁徙的族群隔开"]
        translated = ["Một trận rung chấn kinh hoàng chia cắt nó khỏi đàn đang di cư."]
        valid, reasons = validate_translation_batch_quality(sources, translated, strict=True)
        self.assertFalse(valid)
        self.assertTrue(any("dấu phẩy" in r for r in reasons))

        # After normalization with ensure_speech_pauses_and_entity:
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        self.assertIn(",", fixed[0])
        valid_fixed, _ = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid_fixed)

    def test_short_sentence_does_not_force_comma(self):
        sources = ["你好"]
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

    def test_narrative_and_subject_break_pauses(self):
        sources = [
            "其他怪喙龙，也逐渐习惯了这个跟在后面的陌生面孔。",
            "只有阿特，把完整的半个蛋壳推到小怪常待的地方。",
            "后来又学会利用果壳保护小怪。",
            "天黑之前肯定会回到这个石缝。",
        ]
        translated = [
            "Những con quái mỏ long khác cũng dần quen với sự hiện diện của kẻ xa lạ luôn đi theo phía sau này.",
            "Chỉ có A Thích là đẩy nửa chiếc vỏ nguyên vẹn đến nơi quái con thường nghỉ ngơi.",
            "Sau đó lại học cách tận dụng vỏ quả để bảo vệ quái con.",
            "Trước khi trời tối nhất định sẽ quay lại khe đá này.",
        ]
        fixed = ensure_speech_pauses_and_entity(sources, translated)
        valid, reasons = validate_translation_batch_quality(sources, fixed, strict=True)
        self.assertTrue(valid, f"Expected all to pass quality gate, got: {reasons}")
        for s in fixed:
            self.assertIn(",", s)
            self.assertTrue(s.endswith("."))
            self.assertTrue(s[0].isupper())


if __name__ == "__main__":
    unittest.main()

