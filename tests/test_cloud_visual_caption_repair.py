import copy

import pytest

from backend.cloud.gpu_scripts.visual_caption_repair import (
    derive_caption_substitution,
    validate_visual_caption_correction,
)


def check(source="这个盒子比较旧的", corrected="这个盒子比较大的", captions=None, times=(1.1, 1.5), **kwargs):
    segment = {"id": 7, "text": source, "start": 1.0, "end": 2.0}
    proposal = {"id": 7, "corrected_text": corrected,
                "caption_reads": [{"sample_time": t, "text": text}
                                  for t, text in zip(times, captions or [corrected] * len(times))]}
    proposal.update(kwargs.pop("proposal_updates", {}))
    return validate_visual_caption_correction(segment, proposal, sampled_times=kwargs.pop("sampled_times", times), **kwargs)


def test_repeated_caption_accepts_only_changed_characters_and_preserves_punctuation():
    result = check("这个盒子，比较旧的！", "这个盒子比较大的")
    assert result["accepted"]
    assert result["text"] == "这个盒子，比较大的！"
    assert len(result["audit"]["substitutions"]) == 1
    assert result["audit"]["substitutions"][0]["from"] == "旧"
    assert result["audit"]["substitutions"][0]["supporting_times"] == [1.1, 1.5]


def test_longer_caption_supports_partial_clause_without_copying_prefix_or_suffix():
    result = check("反着放进盒子", "板着放进盒子", captions=["把它板着放进盒子然后压平"] * 2)
    assert result["accepted"] and result["text"] == "板着放进盒子"


def test_adjacent_substitutions_need_stable_local_neighbours():
    result = check("材质也特别舒适", "裁剪也特别舒适")
    assert result["accepted"] and len(result["audit"]["substitutions"]) == 2
    assert not check("甲乙", "丙丁")["accepted"]


def test_wrong_id_or_invented_frame_times_are_rejected():
    assert check(proposal_updates={"id": 8})["audit"]["reason"] == "cue_id_mismatch"
    assert check(sampled_times=[1.2, 1.8])["audit"]["reason"] == "unbound_caption_frame"
    assert not check(sampled_times=[])["accepted"]


def test_duplicate_frame_and_too_close_frames_are_not_independent():
    assert check(times=(1.1, 1.1))["audit"]["reason"] == "duplicate_caption_frame"
    assert check(times=(1.1, 1.2))["audit"]["reason"] == "insufficient_frame_span"
    assert not check(min_sample_span=0.01)["accepted"]


def test_disagreeing_caption_reads_do_not_accept_model_proposal():
    result = check(captions=["这个盒子比较大的", "这个盒子比较旧的"])
    assert not result["accepted"] and result["text"] == "这个盒子比较旧的"
    assert result["audit"]["reason"] == "conflicting_caption_context"


def test_two_good_reads_cannot_hide_contradictory_frame():
    assert not check(times=(1.1, 1.4, 1.8), captions=["这个盒子比较大的", "这个盒子比较大的", "这个盒子比较旧的"])["accepted"]


@pytest.mark.parametrize("corrected", ["这个盒子比较大的20厘米", "盒子比较大的", "这个盒子", "这个盒子比较大大的"])
def test_prefix_deletion_or_insertion_cannot_pass_even_if_caption_supports_it(corrected):
    result = check(corrected=corrected)
    assert not result["accepted"] and result["text"] == "这个盒子比较旧的"


def test_digits_and_latin_cannot_change():
    assert check("20厘米的旧盒子", "30厘米的大盒子")["audit"]["reason"] == "non_cjk_change"
    assert check("A款的旧盒子", "B款的大盒子")["audit"]["reason"] == "non_cjk_change"
    result = check("1.5厘米的旧盒子", "15厘米的大盒子")
    assert result["accepted"] and result["text"] == "1.5厘米的大盒子"


def test_caption_without_local_unchanged_context_is_not_enough():
    result = check(captions=["大的", "大的"])
    assert not result["accepted"] and result["audit"]["reason"] == "insufficient_local_context_support"


def test_unrelated_reads_cannot_authorize_correction():
    result = check(captions=["这是包装的说明", "请关注购买链接"])
    assert not result["accepted"]


def test_three_changes_allowed_only_for_longer_source_with_strict_ratio():
    result = check("先把旧纸张折旧然后放入旧盒子", "先把新纸张折好然后放入大盒子")
    assert result["accepted"] and len(result["audit"]["substitutions"]) == 3
    assert not check("旧纸旧盒旧袋", "新纸新盒新袋")["accepted"]


def test_does_not_mutate_evidence_or_cue_and_keeps_source_on_failure():
    segment = {"id": 7, "start": 1, "end": 2, "text": "这个盒子比较旧的"}
    proposal = {"id": 7, "corrected_text": "这个盒子比较大的", "caption_reads": [
        {"sample_time": 1.1, "text": "这个盒子比较大的"}, {"sample_time": 1.5, "text": "这个盒子比较大的"}]}
    originals = copy.deepcopy((segment, proposal))
    assert validate_visual_caption_correction(segment, proposal, sampled_times=[1.1, 1.5])["accepted"]
    assert (segment, proposal) == originals


@pytest.mark.parametrize("bad_time", [float("nan"), float("inf"), 0.5, 3.0])
def test_invalid_frame_time_never_passes(bad_time):
    assert not check(times=(1.1, bad_time))["accepted"]


def test_short_cue_all_substitutions_require_explicit_bound_audio_and_full_captions():
    proposal = {"audio_transcript": "上刑具"}
    without = check("战红剧", "上刑具", proposal_updates=proposal)
    assert not without["accepted"]
    result = check("战红剧！", "上刑具", proposal_updates=proposal,
                   allow_audio_corroboration=True, audio_sha256="a" * 64)
    assert result["accepted"] and result["text"] == "上刑具！"
    assert result["audit"]["method"] == "frame_bound_audio_caption_short_substitution_v1"
    assert result["audit"]["audio_sha256"] == "a" * 64


@pytest.mark.parametrize("updates,hash_value", [({"audio_transcript": "别的字"}, "a" * 64),
                                               ({}, "a" * 64), ({"audio_transcript": "上刑具"}, None),
                                               ({"audio_transcript": "上刑具"}, "unbound")])
def test_missing_or_unsupported_audio_cannot_authorize_short_full_rewrite(updates, hash_value):
    result = check("战红剧", "上刑具", proposal_updates=updates,
                   allow_audio_corroboration=True, audio_sha256=hash_value)
    assert not result["accepted"]


def test_audio_mode_cannot_use_substring_caption_or_change_numeric_text():
    for captions in [["现在上刑具", "现在上刑具"], ["上刑具", "上工具"]]:
        result = check("战红剧", "上刑具", captions=captions, proposal_updates={"audio_transcript": "上刑具"},
                       allow_audio_corroboration=True, audio_sha256="b" * 64)
        assert not result["accepted"]
    result = check("1战红剧", "2上刑具", proposal_updates={"audio_transcript": "2上刑具"},
                   allow_audio_corroboration=True, audio_sha256="b" * 64)
    assert not result["accepted"]


def test_audio_mode_does_not_authorize_arbitrary_long_rewrite():
    result = check("甲乙丙丁戊己庚辛", "今天我们学习包装", proposal_updates={"audio_transcript": "今天我们学习包装"},
                   allow_audio_corroboration=True, audio_sha256="a" * 64)
    assert not result["accepted"]


def derive(source, captions, *, times=None, sampled_times=None, **proposal_updates):
    times = times or (1.1, 1.5, 1.8)[:len(captions)]
    segment = {"id": 7, "text": source, "start": 1.0, "end": 2.0}
    proposal = {"id": 7, "corrected_text": "忽略这个不可信的改写",
                "caption_reads": [{"sample_time": time, "text": text}
                                  for time, text in zip(times, captions)]}
    proposal.update(proposal_updates)
    original_inputs = copy.deepcopy((segment, proposal))
    result = derive_caption_substitution(
        segment, proposal, sampled_times=times if sampled_times is None else sampled_times,
    )
    assert (segment, proposal) == original_inputs
    return result


@pytest.mark.parametrize("source,captions,expected", [
    ("我盯着镜头开,", ["我盯着镜头拍所以没对准"] * 2, "我盯着镜头拍,"),
    ("开着镜头看", ["所以没对准拍着镜头看"] * 2, "拍着镜头看"),
    ("完美 进阶版狂放不羁的欧牙纸", ["进阶版 狂放不羁的欧雅纸"] * 2,
     "完美 进阶版狂放不羁的欧雅纸"),
    ("拉直 对进去再拉直", ["怼进去再拉直"] * 2, "拉直 怼进去再拉直"),
    ("欧牙纸", ["欧雅纸"] * 2, "欧雅纸"),
    ("A款20厘米的旧盒子！", ["A款20厘米的大盒子"] * 2, "A款20厘米的大盒子！"),
])
def test_derive_preserves_source_clauses_and_only_borrows_repeated_substitutions(source, captions, expected):
    result = derive(source, captions)
    assert result is not None and result["accepted"] and result["text"] == expected
    assert result["audit"]["method"] == "frame_bound_equal_length_substitution_from_caption_reads_v1"
    assert result["audit"]["supporting_read_count"] == 2


@pytest.mark.parametrize("captions", [
    ["这个盒子比较大的"],
    ["这个盒子比较大的", "这个盒子比较小的"],
    ["这个盒子比较大的", "这个盒子比较旧的"],
    ["这个盒子比较大的", "这个盒子比较大的", "这个盒子比较小的"],
    ["大的", "这个盒子比较大的"],
])
def test_derive_requires_consistent_candidates_in_two_independent_frames(captions):
    assert derive("这个盒子比较旧的", captions) is None


@pytest.mark.parametrize("source,caption", [
    ("这个盒子", "这个大盒子"),
    ("这个大盒子", "这个盒子"),
    ("这个旧盒子", "这个非常大的盒子"),
    ("这个盒子", "这个盒子然后压平"),
    ("完美 进阶版", "进阶版"),
])
def test_derive_never_applies_insertion_or_deletion(source, caption):
    assert derive(source, [caption] * 2) is None


def test_derive_requires_matching_cue_and_actual_sample_times():
    source, captions = "欧牙纸", ["欧雅纸"] * 2
    assert derive(source, captions, id=8) is None
    assert derive(source, captions, sampled_times=[1.2, 1.8]) is None
    assert derive(source, captions, times=[1.1, 1.1]) is None
    assert derive(source, captions, times=[1.1, 1.2]) is None
    assert derive(source, captions, sampled_times=[]) is None


def test_derive_never_uses_audio_for_full_short_rewrites():
    assert derive("战红剧", ["上刑具"] * 2, corrected_text="上刑具", audio_transcript="上刑具") is None


def test_derive_ignores_model_text_even_if_it_would_change_source_numbers():
    result = derive("20厘米的旧盒子", ["20厘米的大盒子"] * 2, corrected_text="30厘米的大盒子")
    assert result is not None and result["text"] == "20厘米的大盒子"


@pytest.mark.parametrize("segment,proposal", [
    (None, {}), ({}, None), ({"text": "旧盒子"}, {}),
    ({"text": "旧盒子"}, {"caption_reads": [{"text": "大盒子"}, None]}),
])
def test_derive_malformed_inputs_return_none(segment, proposal):
    assert derive_caption_substitution(segment, proposal, sampled_times=[1.1, 1.5]) is None
