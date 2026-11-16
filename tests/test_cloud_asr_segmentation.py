"""Word-boundary segmentation preserves speech content and source timing."""
from copy import deepcopy

import pytest

from backend.cloud.gpu_scripts.asr_segmentation import ASRSegmentationError, split_aligned_asr_segments


def segment(words, *, start=None, end=None, text=None):
    return {"id": 42, "start": words[0][1] if start is None else start,
            "end": words[-1][2] if end is None else end,
            "text": "".join(word[0] for word in words) if text is None else text,
            "words": [{"word": word, "start": start, "end": end} for word, start, end in words]}


def test_multi_clause_speech_keeps_each_action_at_its_real_source_time():
    raw = [segment([("先把纸放平，", 8.0, 9.2), ("再把边折起来，", 9.4, 11.0),
                    ("最后压紧。", 15.0, 16.2)])]
    saved = deepcopy(raw)
    cues, audit = split_aligned_asr_segments(raw)
    assert [(cue["start"], cue["end"]) for cue in cues] == [(8, 9.2), (9.4, 11), (15, 16.2)]
    assert "".join(cue["text"] for cue in cues) == raw[0]["text"]
    assert [row["source_word_range"] for row in audit["mappings"]] == [[0, 1], [1, 2], [2, 3]]
    assert audit["timestamps_interpolated"] is False
    assert raw == saved  # Caller retains pristine decoder evidence.


def test_unpunctuated_speech_is_bounded_at_word_ends_without_interpolation():
    raw = [segment([(str(i), 20 + i * 0.9, 20 + i * 0.9 + 0.8) for i in range(12)])]
    cues, _ = split_aligned_asr_segments(raw, max_cue_seconds=3.0, max_cue_characters=5)
    assert "".join(cue["text"] for cue in cues) == raw[0]["text"]
    assert all(cue["end"] - cue["start"] <= 3.0 for cue in cues)
    assert all(len(cue["text"]) <= 5 for cue in cues)
    assert all(cue["start"] in {word["start"] for word in raw[0]["words"]} for cue in cues)
    assert all(cue["end"] in {word["end"] for word in raw[0]["words"]} for cue in cues)


def test_short_fragment_joins_next_clause_but_silence_is_not_shifted():
    raw = [segment([("对着，", 10, 10.25), ("盒子的边往下按。", 10.4, 12),
                    ("完成了。", 14, 14.8)])]
    cues, _ = split_aligned_asr_segments(raw)
    assert [(cue["text"], cue["start"], cue["end"]) for cue in cues] == [
        ("对着，盒子的边往下按。", 10, 12), ("完成了。", 14, 14.8)]


def test_original_spacing_and_punctuation_are_sliced_not_rewritten():
    raw = [segment([(" First step,", 1, 2), (" next step.", 2.2, 3.3)],
                   text="  First  step,\n next step.  ")]
    cues, _ = split_aligned_asr_segments(raw)
    assert [cue["text"] for cue in cues] == ["First  step,", "next step."]


def test_zero_duration_token_is_retained_with_timed_neighbors():
    raw = [segment([("第一句。", 0, 1), ("啊", 1, 1)], end=1)]
    cues, _ = split_aligned_asr_segments(raw)
    assert cues == [{"id": 1, "start": 0, "end": 1, "text": "第一句。啊"}]


def test_short_validated_segment_without_words_is_compatible_but_long_one_fails():
    raw = [{"start": 30.0, "end": 32.5, "text": "最后一句"}]
    cues, audit = split_aligned_asr_segments(raw)
    assert cues == [{"id": 1, "start": 30.0, "end": 32.5, "text": "最后一句"}]
    assert audit["mappings"][0]["source_word_range"] is None
    with pytest.raises(ASRSegmentationError, match="no word alignment"):
        split_aligned_asr_segments([dict(raw[0], end=40)])


@pytest.mark.parametrize("mutate, error", [
    (lambda row: row.update(text="第一句，第二句。遗漏"), "cover all transcript"),
    (lambda row: row["words"][0].update(word="另一个句子，"), "differs from transcript"),
    (lambda row: row["words"][0].update(start=-1), "out-of-source"),
    (lambda row: row["words"][0].update(end=9), "out-of-source"),
    (lambda row: row["words"][0].update(start=float("nan")), "not finite"),
    (lambda row: row["words"][1].update(start=0.8), "overlapping"),
])
def test_unsafe_alignment_fails_instead_of_fabricating_or_dropping_text(mutate, error):
    raw = segment([("第一句，", 0, 1), ("第二句。", 1.2, 2.5)])
    mutate(raw)
    with pytest.raises(ASRSegmentationError, match=error):
        split_aligned_asr_segments([raw])


def test_single_word_cannot_be_cut_to_satisfy_a_cap():
    with pytest.raises(ASRSegmentationError, match="single aligned word"):
        split_aligned_asr_segments([segment([("unbroken", 1, 8)])])


def test_adjacent_segments_preserve_order_and_reject_overlap():
    raw = [segment([("第一句。", 10, 11)]), segment([("第二句。", 11, 12)])]
    cues, _ = split_aligned_asr_segments(raw)
    assert [cue["id"] for cue in cues] == [1, 2]
    raw[1]["start"] = 10.5
    with pytest.raises(ASRSegmentationError, match="overlapping source interval"):
        split_aligned_asr_segments(raw)


def test_micro_cue_joins_nearby_same_speaker_and_keeps_full_lineage():
    raw = [segment([("拉直", 11.0, 11.3)]), segment([("再放进去。", 11.3, 12.6)])]
    cues, audit = split_aligned_asr_segments(raw)
    assert cues == [{"id": 1, "start": 11.0, "end": 12.6, "text": "拉直 再放进去。"}]
    assert [part["source_segment_index"] for part in audit["mappings"][0]["source_parts"]] == [0, 1]
    assert audit["unmerged_short_cue_ids"] == []


@pytest.mark.parametrize("next_start,speaker", [(12.0, None), (11.3, "different")])
def test_micro_cue_never_merges_across_pause_or_speaker_change(next_start, speaker):
    raw = [segment([("好", 11.0, 11.3)]), segment([("现在继续。", next_start, next_start + 1.2)])]
    if speaker:
        raw[0]["speaker"] = "first"
        raw[1]["speaker"] = speaker
    cues, audit = split_aligned_asr_segments(raw)
    assert len(cues) == 2
    assert cues[0]["end"] == 11.3
    assert audit["unmerged_short_cue_ids"] == [1]


def test_micro_cue_does_not_merge_past_duration_or_character_cap():
    raw = [segment([("大段话", 0, 5.9)]), segment([("好", 5.9, 6.3)])]
    cues, audit = split_aligned_asr_segments(raw)
    assert len(cues) == 2 and audit["unmerged_short_cue_ids"] == [2]
    raw = [segment([("一二三四五", 0, 2)]), segment([("好", 2, 2.4)])]
    cues, audit = split_aligned_asr_segments(raw, max_cue_characters=5)
    assert len(cues) == 2 and audit["unmerged_short_cue_ids"] == [2]
