import unittest
from datetime import timedelta

import pytest
from srt import Subtitle

from backend.ai import translation as tr
from backend.ai.translation import (
    ensure_speech_pauses_and_entity,
    validate_translation_batch_quality,
)


@pytest.mark.parametrize("source, wrong, faithful", [
    ("如果不盖盖子，拿长方形收纳盒。", "Nếu đậy nắp thì lấy hộp vuông.",
     "Nếu không đậy nắp thì lấy hộp chữ nhật."),
    ("这个长方形盒子宽65毫米，售价二十九元。", "Hộp vuông rộng 75 mm, giá 39 tệ.",
     "Hộp chữ nhật rộng 6,5 cm, giá 29 tệ."),
    ("拿两个袋子，不要盖盖子。", "Lấy một túi rồi đậy nắp.", "Lấy hai túi, đừng đậy nắp."),
    ("双面厚度六十五丝。", "Một mặt dày 0,65 mm.", "Cả hai mặt dày 0,65 mm."),
    ("这台机器售价5万元。", "Máy giá 5 nghìn tệ.", "Máy giá 50 nghìn tệ."),
    ("这个盒子宽六十五毫米。", "Cái hộp này rộng sáu mươi lăm xăng-ti-mét.",
     "Cái hộp này rộng sáu mươi lăm mi-li-mét."),
])
def test_hard_facts_reject_changes_and_allow_equivalent_notation(source, wrong, faithful):
    assert not validate_translation_batch_quality([source], [wrong])[0]
    assert validate_translation_batch_quality([source], [faithful]) == (True, [])


def test_condensing_preserves_units_quantities_and_conditions():
    original = "Nếu không đậy nắp thì chọn hộp chữ nhật rộng 65 mm."
    assert not tr.validate_condensed_text(original, "Nếu đậy nắp, chọn hộp vuông 65.", 15)
    assert tr.validate_condensed_text(original, "Nếu không đậy nắp, lấy hộp chữ nhật 65 mm.", 15)


def test_condensing_preserves_uncertainty_without_literal_negative_word():
    original = "Đang phân vân không biết nên treo chiếc acrylic hình con lười này"
    assert tr.validate_condensed_text(original, "Phân vân treo móc con lười", 6)
    assert not tr.validate_condensed_text(original, "Cứ treo móc con lười", 6)
    assert not tr.validate_condensed_text(original, "Đang phân vân treo móc con lười", 6)
    original = "Không biết nên chọn hộp 65 mm, đừng đậy nắp"
    assert tr.validate_condensed_text(original, "Phân vân chọn hộp 65 mm, đừng đậy nắp", 10)
    assert not tr.validate_condensed_text(original, "Phân vân chọn hộp 65 mm, đậy nắp", 10)
    assert not tr.validate_condensed_text(original, "Phân vân chọn hộp 75 mm, đừng đậy nắp", 10)


def test_condensing_preserves_an_alternative_and_enforces_explicit_counts():
    original = "Năm nay mình lại muốn đổi sang một loại sổ tay khác để thử xem sao"
    assert tr.validate_condensed_text(original, "Năm nay thử sổ khác", 5)
    assert not tr.validate_condensed_text(original, "Năm nay thử hai loại sổ khác", 8)
    assert not tr.validate_condensed_text(original, "Năm nay thử sổ tay", 5)
    assert not tr.validate_condensed_text("Năm nay chỉ thử một loại sổ khác", "Năm nay thử sổ khác", 5)


def test_chinese_indefinite_article_does_not_force_a_spoken_quantity():
    assert validate_translation_batch_quality(
        ["然后是一个果冻，一起品尝一下吧。"], ["Tiếp theo là thạch, cùng nếm nhé."],
    ) == (True, [])
    assert not validate_translation_batch_quality(["只放一个。"], ["Chỉ đặt hai cái."])[0]


def test_muji_clip_preserves_count_and_does_not_invent_a_condition():
    sources = ["你们三个在我的购物车躺了好久啊", "有点像贴纸主要是刷到它可以收纳小卡相卡"]
    translation = ["Ba món này nằm trong giỏ hàng lâu rồi", "Hơi giống nhãn dán, chủ yếu là thấy đựng được thẻ ảnh"]
    assert validate_translation_batch_quality(sources, translation) == (True, [])
    translation[0] = "Mấy món này nằm trong giỏ hàng lâu rồi"
    assert validate_translation_batch_quality(sources, translation) == (
        False, ["Câu 1 sai số lượng, số đo hoặc đơn vị"],
    )


def test_colloquial_chinese_if_still_requires_a_condition():
    source = "要是没盖盖子，就用这个盒子。"
    assert validate_translation_batch_quality([source], ["Nếu chưa đậy nắp, thì dùng hộp này."]) == (True, [])
    _, reasons = validate_translation_batch_quality([source], ["Chưa đậy nắp và dùng hộp này."])
    assert "Câu 1 đổi hoặc bỏ điều kiện" in reasons


def test_quality_retry_only_retranslates_bad_cue_with_adjacent_context(monkeypatch):
    from unittest.mock import Mock

    source = ["材料在这里。", "盒子宽65毫米。", "下面开始组装。"]
    cues = [Subtitle(i, timedelta(seconds=i), timedelta(seconds=i + 1), text)
            for i, text in enumerate(source, start=1)]
    gemini = Mock(side_effect=[
        ["Vật liệu ở đây.", "Hộp rộng 75 mm.", "Tiếp theo là lắp ráp."],
        ["Hộp rộng 65 mm."],
    ])
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(tr, "translate_with_gemini", gemini)
    translated = tr.translate_subtitles(cues, api_key="fixture-key", domain="general", strict=True)
    assert [cue.content for cue in translated] == [
        "Vật liệu ở đây.", "Hộp rộng 65 mm.", "Tiếp theo là lắp ráp.",
    ]
    retry = gemini.call_args
    assert retry.args[0] == [source[1]]
    repair = retry.kwargs["cue_meta"][0]["repair"]
    assert repair["rejected_translation"] == "Hộp rộng 75 mm."
    assert [neighbor["source"] for neighbor in repair["neighbors"]] == [source[0], source[2]]
    assert "65" in retry.args[0][0] and repair["issues"]


def test_translation_shrinks_only_measured_risk_before_handoff(monkeypatch):
    from unittest.mock import Mock

    source = ["正方形盒子宽65毫米。", "放在桌上。"]
    cues = [Subtitle(i, timedelta(seconds=i), timedelta(seconds=i + 1), text)
            for i, text in enumerate(source, start=1)]
    candidate = "Hộp vuông rộng 65 mm."
    provider = Mock(return_value=["Chiếc hộp hình vuông này có chiều ngang là 65 mm.", "Đặt lên bàn."])
    shorten = Mock(return_value={0: candidate})
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr(tr, "translate_with_gemini", provider)
    monkeypatch.setattr(tr, "condense_vietnamese_subtitles_batch", shorten)
    metadata = [{"id": 1, "max_sec": 1.45, "max_words": 5}, {"id": 2, "max_sec": 2, "max_words": 6}]
    result = tr.translate_subtitles(cues, api_key="fixture-key", domain="general", strict=True, cue_meta=metadata)
    assert [cue.content for cue in result] == [candidate, "Đặt lên bàn."]
    assert shorten.call_args.args[0][0]["target_seconds"] == 1.45
    assert shorten.call_args.args[0][0]["source_text"] == source[0]
    assert len(shorten.call_args.args[0]) == 1


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
