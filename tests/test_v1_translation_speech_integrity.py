import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
import srt

from backend.ai import v1_voice_isolated
from backend.ai import voice_cloning
from backend.ai.v1_translation_meaning import (
    ensure_translation_speech_integrity,
    translation_meaning_issues,
    translation_speech_integrity_failures,
)


def cue(index, content, *, original=None):
    segment = srt.Subtitle(
        index=index,
        start=timedelta(seconds=index),
        end=timedelta(seconds=index + 2),
        content=content,
    )
    if original is not None:
        segment.orig_content = original
    return segment


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("未开盖", "Mở nắp"),
        ("尚未打开盖子", "Đã mở nắp"),
        ("无糖", "Có đường"),
    ],
)
def test_reproduced_chinese_negation_inversions_are_rejected(source, target):
    assert "bỏ phủ định hoặc lời cấm" in translation_meaning_issues(source, target)


def test_scoped_no_sugar_pattern_does_not_treat_wuliao_as_negation():
    assert translation_meaning_issues("无聊", "Thật buồn chán") == []
    assert translation_meaning_issues("无糖", "Không đường") == []


def test_speech_integrity_only_checks_trusted_original_and_preserves_cue_ids():
    bad = cue(9, "Mở nắp", original="未开盖")
    untrusted = cue(13, "Mở nắp")
    assert translation_speech_integrity_failures([bad, untrusted]) == [
        "cue 9: bỏ phủ định hoặc lời cấm",
    ]
    ensure_translation_speech_integrity([untrusted])
    assert [bad.index, untrusted.index] == [9, 13]


def test_voice_cloning_rejects_before_translation_provider_or_tts(tmp_path, monkeypatch):
    monkeypatch.setenv("V1_TRANSLATION_MEANING_POLICY", "strict")
    wrong_vietnamese = cue(4, "Mở nắp", original="未开盖")
    pending_chinese = cue(5, "无糖。", original="无糖。")
    with patch("backend.ai.translation.translate_subtitles") as provider, \
            patch.object(voice_cloning, "generate_single_tts") as tts:
        with pytest.raises(RuntimeError, match="Translation fact mismatch"):
            asyncio.run(voice_cloning.generate_dubbing_audio(
                [wrong_vietnamese, pending_chinese], str(tmp_path),
            ))
    provider.assert_not_called()
    tts.assert_not_called()


def test_voice_cloning_rejects_bad_provider_result_before_tts(tmp_path, monkeypatch):
    monkeypatch.setenv("V1_TRANSLATION_MEANING_POLICY", "strict")
    pending_chinese = cue(5, "无糖。", original="无糖。")

    def bad_provider(items, *args, **kwargs):
        items[0].content = "Có đường."
        return items

    with patch("backend.ai.translation.translate_subtitles", side_effect=bad_provider) as provider, \
            patch.object(voice_cloning, "generate_single_tts") as tts:
        with pytest.raises(RuntimeError, match="Translation fact mismatch"):
            asyncio.run(voice_cloning.generate_dubbing_audio([pending_chinese], str(tmp_path)))
    provider.assert_called_once()
    tts.assert_not_called()


def test_isolated_tts_rejects_before_worker_start(tmp_path, monkeypatch):
    from backend import batch_control

    monkeypatch.setenv("V1_TRANSLATION_MEANING_POLICY", "strict")
    segment = cue(7, "Có đường", original="无糖")
    with patch.object(batch_control, "run") as worker:
        with pytest.raises(RuntimeError, match="Translation fact mismatch"):
            asyncio.run(v1_voice_isolated.generate_dubbing_audio_isolated([segment], str(tmp_path)))
    worker.assert_not_called()


def test_default_warn_policy_logs_advisory_without_blocking_tts(monkeypatch, caplog):
    monkeypatch.delenv("V1_TRANSLATION_MEANING_POLICY", raising=False)
    segment = cue(7, "Có đường", original="无糖")
    with caplog.at_level("WARNING"):
        ensure_translation_speech_integrity([segment])
    assert any("Translation meaning advisory" in rec.message for rec in caplog.records)
