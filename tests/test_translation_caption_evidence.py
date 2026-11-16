from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from backend.ai.v1_caption_evidence import build_ocr_hint, translation_cue_meta


def segment(rows, **kwargs):
    return NS(content="第一个就是可以放脂肪型帮吉利亚克里转我买的是二格的",
              start=timedelta(seconds=4.76), end=timedelta(seconds=9.06),
              tracking_blocks=rows, **kwargs)


def row(text, time, **kwargs):
    return dict(text=text, sample_time=time, prob=.99, **kwargs)


def test_changing_caption_keeps_shape_before_best_frame_quantity():
    cue = segment([row("我buy的是2格的", 7.985),
                   row("就是可以放正方形吧唧的亚克力砖", 5.835),
                   row("我buy的是2格的", 8.5)])
    assert build_ocr_hint(cue) == "就是可以放正方形吧唧的亚克力砖 / 我buy的是2格的"
    meta = translation_cue_meta([cue], [dict(id=17, speaker="female", max_words=15,
                                           ocr_hint="我buy的是2格的")])[0]
    assert meta["id"] == 17 and meta["max_words"] == 15 and meta["speaker"] == "female"
    assert "正方形" in meta["ocr_hint"]


def test_off_cue_low_confidence_and_packaging_do_not_leak_into_hint():
    rows = [row("前一句", 4.5), row("下一句", 9.5), row("广告", 6, is_packaging=True),
            row("水印", 6, is_static=True), row("未确认", 6, is_subtitle=False),
            dict(text="低置信度", sample_time=6, prob=.7), row("不确定", float("nan"))]
    cue = segment(rows, best_block=dict(text="前一句", start=4, end=4.5, prob=.99))
    assert build_ocr_hint(cue) == ""
    assert translation_cue_meta([cue], [dict(ocr_hint="前一句")])[0]["ocr_hint"] == ""


def test_interval_only_serialized_blocks_and_hint_budget():
    cue = segment([dict(text="正方形吧唧", start=4.46, end=6.91, prob=.99),
                   dict(text="我buy的是2格的", start=6.91, end=9.08, prob=.99)])
    assert build_ocr_hint(cue) == "正方形吧唧 / 我buy的是2格的"
    assert build_ocr_hint(cue, max_chars=10) == "正方形吧唧"


def test_empty_cue_does_not_shift_caller_voice_metadata():
    empty = NS(content="")
    cue = segment([row("正方形吧唧", 6)])
    assert translation_cue_meta([empty, cue], [dict(id=2, speaker="male")]) == [
        dict(id=2, speaker="male", ocr_hint="正方形吧唧")]


def test_translate_subtitles_forwards_caption_sequence_and_caller_budgets(monkeypatch):
    """Exercise the public translation entry point without any provider or media I/O."""
    from copy import deepcopy
    import json
    from unittest.mock import Mock

    from backend.ai import translation as tr

    first = segment([row("我buy的是2格的", 7.985),
                     row("就是可以放正方形吧唧的亚克力砖", 5.835)], index=17)
    second = NS(index=22, content="不盖盖子的话就可以放进去",
                start=timedelta(seconds=10), end=timedelta(seconds=13),
                tracking_blocks=[row("不盖盖子的话", 10.5), row("就可以放进去", 12)])
    empty = NS(index=1, content="")
    source_texts = [first.content, second.content]
    metadata = [dict(id=17, speaker="female", max_words=18, max_sec=4.3,
                     ocr_hint="我buy的是2格的"),
                dict(id=22, speaker="male", max_words=12, max_sec=3.0)]
    budgets = [dict(seconds=4.3, max_characters=60), dict(seconds=3.0, max_characters=42)]
    original_metadata, original_budgets = deepcopy(metadata), deepcopy(budgets)
    provider = Mock(return_value=["Tôi mua khối acrylic hai ngăn để badge hình vuông.",
                                 "Nếu không đậy nắp thì có thể đặt vào."])
    frames = Mock(return_value=["fixture-frame"])
    domain = Mock(return_value="general")

    def unexpected_io(*args, **kwargs):
        raise AssertionError("Unexpected provider/network call")

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("V1_ALLOW_TRANSLATION_FALLBACK", "false")
    monkeypatch.setattr(tr, "translate_with_gemini", provider)
    for name in ("translate_with_openai", "translate_with_deepseek", "translate_with_g4f",
                 "GoogleTranslator", "MyMemoryTranslator", "call_gemini_api"):
        monkeypatch.setattr(tr, name, unexpected_io)
    monkeypatch.setattr(tr.requests.sessions.Session, "request", unexpected_io)
    monkeypatch.setattr(tr, "extract_video_frames_base64", frames)
    monkeypatch.setattr(tr, "classify_video_domain", domain)
    monkeypatch.setattr(tr, "resolve_effective_glossary", lambda **kwargs: {})
    monkeypatch.setattr(tr, "load_custom_glossary", lambda *args, **kwargs: {})

    result = tr.translate_subtitles(
        [empty, first, second], api_key="fixture-key", video_path="fixture-video.mp4",
        enable_g4f=False, strict=True, cue_meta=metadata, duration_budgets=budgets,
        context_start_seconds=4, context_end_seconds=13, prior_context=["Ngữ cảnh trước."])

    provider.assert_called_once()
    assert provider.call_args.args[0] == source_texts
    received = provider.call_args.kwargs
    assert received["duration_budgets"] == original_budgets
    assert received["cue_meta"] == [
        dict(original_metadata[0], ocr_hint="就是可以放正方形吧唧的亚克力砖 / 我buy的是2格的"),
        dict(original_metadata[1], ocr_hint="不盖盖子的话 / 就可以放进去"),
    ]
    assert received["context_start_seconds"] == 4 and received["context_end_seconds"] == 13
    assert received["prior_context"] == ["Ngữ cảnh trước."]
    frames.assert_called_once_with("fixture-video.mp4", 4, 13)
    domain.assert_called_once()
    assert domain.call_args.args[:2] == (["fixture-frame"], source_texts)
    assert metadata == original_metadata and budgets == original_budgets

    prompt = tr.build_translation_prompt(source_texts, cue_meta=received["cue_meta"],
                                         structured_output=True)
    payload = json.loads(prompt.split("Dữ liệu:\n", 1)[1])
    assert [(cue["id"], cue["speaker"], cue["max_words"], cue["ocr_hint"]) for cue in payload] == [
        (meta["id"], meta["speaker"], meta["max_words"], meta["ocr_hint"])
        for meta in received["cue_meta"]
    ]
    assert result[0].content == ""
    assert [cue.orig_content for cue in result[1:]] == source_texts
    assert "hình vuông" in result[1].content and "không đậy nắp" in result[2].content
    assert [(cue.index, cue.start, cue.end) for cue in result[1:]] == [
        (17, timedelta(seconds=4.76), timedelta(seconds=9.06)),
        (22, timedelta(seconds=10), timedelta(seconds=13)),
    ]


@pytest.mark.parametrize("provider_name", ["openai", "deepseek"])
def test_fallback_provider_prompt_retains_evidence_and_string_array_contract(monkeypatch, provider_name):
    """Run each real provider adapter up to its mocked HTTP transport."""
    import json
    from unittest.mock import Mock

    from backend.ai import translation as tr

    cue = segment([row("我buy的是2格的", 7.985),
                   row("就是可以放正方形吧唧的亚克力砖", 5.835)], index=17)
    translation = "Tôi mua khối acrylic hai ngăn để badge hình vuông"
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"message": {"content": json.dumps([translation])}}]}
    transport = Mock(return_value=response)

    def unexpected_io(*args, **kwargs):
        raise AssertionError("Unexpected provider/network call")

    monkeypatch.setenv("LLM_PROVIDER", provider_name)
    monkeypatch.setenv(provider_name.upper() + "_API_KEY", "fixture-key")
    monkeypatch.setenv("V1_ALLOW_TRANSLATION_FALLBACK", "false")
    for other in ("gemini", "openai", "deepseek", "g4f"):
        if other != provider_name:
            monkeypatch.setattr(tr, "translate_with_" + other, unexpected_io)
    for name in ("GoogleTranslator", "MyMemoryTranslator", "call_gemini_api"):
        monkeypatch.setattr(tr, name, unexpected_io)
    monkeypatch.setattr(tr.requests.sessions.Session, "request", unexpected_io)
    monkeypatch.setattr(tr.requests, "post", transport)
    monkeypatch.setattr(tr, "extract_video_frames_base64", lambda *args, **kwargs: [])
    monkeypatch.setattr(tr, "classify_video_domain", lambda *args, **kwargs: "general")
    monkeypatch.setattr(tr, "resolve_effective_glossary", lambda **kwargs: {})
    monkeypatch.setattr(tr, "load_custom_glossary", lambda *args, **kwargs: {})
    monkeypatch.setattr(tr, "current_model_policy", lambda: NS(openai_candidates=["fixture-model"]))

    result = tr.translate_subtitles(
        [cue], video_path="fixture-video.mp4", strict=True, enable_g4f=False,
        cue_meta=[dict(id=17, speaker="female", max_words=18, max_sec=4.3,
                       ocr_hint="我buy的是2格的")])

    transport.assert_called_once()
    assert transport.call_args.args[0] == f"https://api.{provider_name}.com/v1/chat/completions"
    request = transport.call_args.kwargs["json"]
    prompt = request["messages"][1]["content"]
    assert "pure JSON arrays of translated strings" in request["messages"][0]["content"]
    assert "CHỈ trả về mảng JSON chứa các chuỗi dịch" in prompt
    payload = json.loads(prompt.split("Dữ liệu:\n", 1)[1])
    assert payload == [dict(id=17, speaker="female", max_words=18,
                            ocr_hint="就是可以放正方形吧唧的亚克力砖 / 我buy的是2格的",
                            text="第一个就是可以放脂肪型帮吉利亚克里转我买的是二格的")]
    assert result[0].content == translation


@pytest.mark.parametrize("bad_start,bad_end", [(None, 9.06), (4.76, float("nan")), (9.06, 4.76)])
def test_malformed_optional_cue_timing_drops_hint(bad_start, bad_end):
    cue = segment([row("正方形吧唧", 6)])
    cue.start, cue.end = bad_start, bad_end
    assert build_ocr_hint(cue) == ""


def test_inverted_caption_interval_cannot_become_translation_evidence():
    cue = segment([dict(text="倒序时间", start=8, end=7, prob=.99)])
    assert build_ocr_hint(cue) == ""
