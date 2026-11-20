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


def test_condensing_vietnamese_topic_thi_and_synonym_negation_for_real_xhs_cues():
    cue1 = "Bạn nào mới chơi đồ sưu tầm mà chưa biết chọn kích thước màng bọc thẻ thì xem ngay nhé"
    assert tr.validate_condensed_text(cue1, "Mới chơi chưa biết chọn cỡ bọc thẻ, xem ngay", 15)
    assert tr.validate_condensed_text(cue1, "Mới chơi không biết chọn cỡ bọc thẻ, xem ngay", 15)
    assert not tr.validate_condensed_text(cue1, "Mới chơi biết chọn cỡ bọc thẻ, xem ngay", 15)

    cue3 = "Dù là đồ của công ty chúng mình hay của giới giải trí thì bưu thiếp cơ bản đều có kích thước này"
    assert tr.validate_condensed_text(cue3, "Bưu thiếp hay ảnh thường đều dùng cỡ này", 15)

    explicit = "Nếu chưa biết chọn cỡ màng bọc thì hãy xem ngay nhé"
    assert tr.validate_condensed_text(explicit, "Nếu không biết chọn cỡ bọc, xem ngay", 12)
    assert not tr.validate_condensed_text(explicit, "Chưa biết chọn cỡ bọc, xem ngay", 12)


def test_condensing_preserves_dimensions_with_ascii_x_and_glued_units_and_decimal_comma():
    assert tr.validate_condensed_text("Dùng loại kích thước 57×164 để đựng ảnh", "Dùng loại 57x164 đựng ảnh", 10)
    assert tr.validate_condensed_text("Dùng loại 3.5 inch 65×91 cho thẻ bo góc", "Dùng loại 3,5 inch 65x91", 10)
    assert tr.validate_condensed_text("Chọn hộp chữ nhật rộng 65 mm nhé", "Chọn hộp chữ nhật 65mm", 10)
    assert not tr.validate_condensed_text("Dùng loại 57×164 để đựng ảnh", "Dùng loại 58x164 đựng ảnh", 10)
    assert not tr.validate_condensed_text("Dùng loại 3.5 inch 65×91 cho thẻ", "Dùng loại 65x91 cho thẻ", 10)


def test_condense_batch_honors_max_accept_words_without_loosening_strict_callers(monkeypatch):
    import json
    from unittest.mock import Mock
    from backend.ai import v1_translation_cache as tc

    orig = "Bạn nào mới chơi đồ sưu tầm mà chưa biết chọn kích thước màng bọc thẻ thì xem ngay nhé"
    short_10 = "Mới chơi chưa biết chọn cỡ bọc thẻ xem ngay"
    assert len(short_10.split()) == 10
    seen_payloads = []
    write_mock = Mock()
    monkeypatch.setattr(tc, "read_condense_cache", lambda *a, **k: None)
    monkeypatch.setattr(tc, "write_condense_cache", write_mock)

    def fake_gemini(payload, **kwargs):
        text_prompt = payload["contents"][0]["parts"][0]["text"]
        seen_payloads.append(text_prompt)
        data = {"candidates": [{"finishReason": "STOP", "content": {
            "parts": [{"text": json.dumps({"1": short_10}, ensure_ascii=False)}]
        }}]}
        validator = kwargs.get("response_validator")
        if validator:
            validator(data)
        return data, "high"

    monkeypatch.setattr(tr, "call_gemini_api", fake_gemini)

    relaxed = tr.condense_vietnamese_subtitles_batch(
        [{"index": 1, "text": orig, "target_seconds": 2.31, "target_words": 8, "max_accept_words": 11}],
        api_key="synthetic",
    )
    assert relaxed == {1: short_10}
    assert len(seen_payloads) == 1
    assert "max_accept_words" not in seen_payloads[0]
    write_mock.assert_not_called()

    strict = tr.condense_vietnamese_subtitles_batch(
        [{"index": 1, "text": orig, "target_seconds": 2.31, "target_words": 8}],
        api_key="synthetic",
    )
    assert strict == {}


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


def test_zh_topic_after_unpunctuated_negative_clause_does_not_require_if():
    source = "D版是白色的不会突兀小卡的话是能当四公格放"
    faithful = "Nền trắng nên không lạc tông, thẻ nhỏ xếp được bốn ô"
    assert validate_translation_batch_quality([source], [faithful]) == (True, [])
    dropped_negation = "Nền trắng, thẻ nhỏ xếp được bốn ô"
    assert validate_translation_batch_quality([source], [dropped_negation]) == (
        False, ["Câu 1 bỏ phủ định hoặc lời cấm"],
    )


@pytest.mark.parametrize("source,faithful,wrong", [
    ("不盖盖子的话，放在这里。", "Nếu không đậy nắp, đặt ở đây.", "Đặt ở đây, đừng đậy nắp."),
    ("温度太高的话就等一等。", "Nếu nóng quá thì đợi một lát.", "Đợi một lát."),
])
def test_zh_implicit_conditions_still_require_faithful_translation(source, faithful, wrong):
    assert validate_translation_batch_quality([source], [faithful]) == (True, [])
    _, reasons = validate_translation_batch_quality([source], [wrong])
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


def test_size_dimensions_accept_punctuation_and_ascii_x():
    source = "用到的是3寸58×88的接下来就是我们的大海豹尺寸"
    for faithful in (
        "Dùng cỡ 3 inch 58×88, tiếp theo là kích thước lớn",
        "Dùng cỡ 3 inch 58x88, tiếp theo là kích thước lớn",
        "Dùng cỡ 3 inch 58x88mm, tiếp theo là kích thước lớn",
    ):
        assert validate_translation_batch_quality([source], [faithful]) == (True, [])
    assert validate_translation_batch_quality(["用到的是3寸58×88。"], ["Dùng cỡ 3 inch 58x88."]) == (True, [])
    assert validate_translation_batch_quality(["用到的是3寸58×88。"], ["Dùng cỡ 3 inch 58x88mm."]) == (True, [])
    _, reasons = validate_translation_batch_quality([source], ["Dùng cỡ 3 inch, tiếp theo là kích thước lớn"])
    assert "Câu 1 đổi hoặc bỏ số hiệu/số cỡ" in reasons


def test_uppercase_g_slang_or_tech_not_parsed_as_grams():
    assert validate_translation_batch_quality(
        ["我们平姐姐连迷你8G都要配平哦"],
        ["Chị ấy đến cả huy hiệu mini cũng muốn ghép cặp"],
    ) == (True, [])
    for gram_source in ("重8g", "重8克"):
        _, reasons = validate_translation_batch_quality([gram_source], ["Rất nhẹ"])
        assert "Câu 1 sai số lượng, số đo hoặc đơn vị" in reasons


@pytest.mark.parametrize("source,faithful", [
    ("另外一个是我给纪念卡组里面大的那一张", "Cái còn lại là tấm lớn đó trong bộ thẻ kỷ niệm"),
    ("同一张的大卡册的火焰也是这样做的", "Trang rời của cuốn sổ lớn cùng loại cũng làm như vậy"),
    ("怎么会有这么可爱的一套八机啊", "Sao lại có bộ huy hiệu dễ thương thế này chứ"),
    ("最可爱的一套丙图了吧", "Chắc là bộ tranh in dễ thương nhất rồi"),
    ("买两三个盒子", "Mua hai ba cái hộp"),
    ("大家有没有发现三专明信片比正常的要小一些", "Mọi người có để ý card album ba nhỏ hơn bình thường một chút"),
])
def test_zh_demonstrative_attributive_singular_and_ranges_and_a_not_a(source, faithful):
    assert validate_translation_batch_quality([source], [faithful]) == (True, [])


def test_non_strict_meaning_policy_retries_once_then_warns_instead_of_aborting(monkeypatch):
    from unittest.mock import Mock

    cues = [Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "盒子宽65毫米。")]
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("V1_TRANSLATION_MEANING_POLICY", raising=False)
    monkeypatch.delenv("V1_ALLOW_TRANSLATION_FALLBACK", raising=False)
    monkeypatch.setattr(tr, "condense_vietnamese_subtitles_batch", Mock(return_value={}))

    soft_mismatch = Mock(side_effect=[["Hộp rộng 75 mm."], ["Hộp rộng 75 mm."]])
    monkeypatch.setattr(tr, "translate_with_gemini", soft_mismatch)
    result = tr.translate_subtitles(cues, api_key="fixture-key", domain="general", strict=True)
    assert soft_mismatch.call_count == 2
    assert [cue.content for cue in result] == ["Hộp rộng 75 mm."]

    monkeypatch.setenv("V1_TRANSLATION_MEANING_POLICY", "strict")
    strict_mismatch = Mock(side_effect=[["Hộp rộng 75 mm."], ["Hộp rộng 75 mm."]])
    monkeypatch.setattr(tr, "translate_with_gemini", strict_mismatch)
    with pytest.raises(RuntimeError, match="Strict translation mode"):
        tr.translate_subtitles(
            [Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "盒子宽65毫米。")],
            api_key="fixture-key", domain="general", strict=True,
        )

    monkeypatch.delenv("V1_TRANSLATION_MEANING_POLICY", raising=False)
    cjk_leak = Mock(side_effect=[["Hộp 盒子 rộng 65 mm."], ["Hộp 盒子 rộng 65 mm."]])
    monkeypatch.setattr(tr, "translate_with_gemini", cjk_leak)
    with pytest.raises(RuntimeError, match="Strict translation mode"):
        tr.translate_subtitles(
            [Subtitle(1, timedelta(seconds=1), timedelta(seconds=3), "盒子宽65毫米。")],
            api_key="fixture-key", domain="general", strict=True,
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
