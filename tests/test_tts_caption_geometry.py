"""Speech cue grouping must preserve the independently observed source timeline."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from backend.subtitle_blur import collect_source_blur_boxes
from backend.v1_speech_guard import coalesce_tiny_same_voice_cues


def block(start, end, *, left=.2, text="source", verified=True):
    return NS(start=start, end=end, x_pct=left, max_x_pct=.8,
              y_pct=.8, max_y_pct=.86, text=text, prob=.99,
              geometry_verified=verified, visual_locked=verified,
              bg_color_ass="&H202020&", text_contrast_color_ass="&HFFFFFF&")


def cue(index, start, end, tracks=(), *, status="located", verified=True, best=None):
    tracks = list(tracks)
    return NS(index=index, start=timedelta(seconds=start), end=timedelta(seconds=end),
              content=f"words {index}", orig_content=f"source {index}",
              voice_source="fixture", voice_param="female", gender="female",
              visual_sub_to_speech=True, visual_start_sec=start, visual_end_sec=end,
              tracking_blocks=tracks, best_block=best or (tracks[0] if tracks else None),
              ocr_mask_status=status, geometry_verified=verified)


def test_tiny_cue_merge_keeps_exact_source_fragments_and_gaps():
    # Three starts fall inside the existing 150ms TTS grouping window. The
    # final cue contains a real 34ms empty source interval, which must stay empty.
    source_tracks = [block(10.055, 10.158, text="first"),
                     block(10.158, 10.193, left=.25, text="second"),
                     block(10.193, 10.399, text="third"),
                     block(10.433, 11.064, text="third")]
    segments = [cue(10, 10.055, 10.158, source_tracks[:1]),
                cue(11, 10.158, 10.193, source_tracks[1:2]),
                cue(12, 10.193, 11.064, source_tracks[2:])]
    expected = collect_source_blur_boxes(segments)

    assert coalesce_tiny_same_voice_cues(segments) is segments

    assert len(segments) == 1
    merged = segments[0]
    assert merged.content == "words 10 words 11 words 12"
    assert merged.orig_content == "source 10 source 11 source 12"
    assert merged.end.total_seconds() == pytest.approx(11.064)
    assert (merged.visual_start_sec, merged.visual_end_sec) == pytest.approx((10.055, 11.064))
    actual = collect_source_blur_boxes(segments)
    assert actual == expected
    assert not any(row["start"] <= 10.416 < row["end"] for row in actual)
    assert all(merged_block is not original
               for merged_block in merged.tracking_blocks for original in source_tracks)
    assert merged.tracking_blocks[1].bg_color_ass == "&H202020&"
    merged.tracking_blocks[1].x_pct = .3
    assert source_tracks[1].x_pct == .25


@pytest.mark.parametrize("caption_first", [False, True])
def test_no_caption_status_does_not_hide_merged_located_observations(caption_first):
    observed = block(1.05, 2.) if not caption_first else block(1., 1.05)
    located = cue(1 if caption_first else 2, 1. if caption_first else 1.05,
                  1.05 if caption_first else 2., [observed])
    empty = cue(2 if caption_first else 1, 1.05 if caption_first else 1.,
                2. if caption_first else 1.05, status="no_caption_evidence")
    segments = [located, empty] if caption_first else [empty, located]
    expected = collect_source_blur_boxes(segments)

    coalesce_tiny_same_voice_cues(segments)

    assert len(segments) == 1
    assert segments[0].ocr_mask_status == "located"
    assert (segments[0].visual_start_sec, segments[0].visual_end_sec) == (1., 2.)
    assert collect_source_blur_boxes(segments) == expected
    assert segments[0].best_block is not None


@pytest.mark.parametrize("empty_first", [False, True])
def test_verified_empty_cue_never_resurrects_its_stale_best_block(empty_first):
    stale = block(0., 5., left=.01, text="stale")
    empty = cue(1 if empty_first else 2, 1. if empty_first else 1.05,
                1.05 if empty_first else 2., best=stale)
    observed = block(1.05, 2.) if empty_first else block(1., 1.05)
    located = cue(2 if empty_first else 1, 1.05 if empty_first else 1.,
                  2. if empty_first else 1.05, [observed])
    segments = [empty, located] if empty_first else [located, empty]
    expected = collect_source_blur_boxes(segments)

    coalesce_tiny_same_voice_cues(segments)

    assert len(segments) == 1
    assert collect_source_blur_boxes(segments) == expected
    assert (segments[0].visual_start_sec, segments[0].visual_end_sec) == (1., 2.)
    assert all(row.x_pct != stale.x_pct for row in segments[0].tracking_blocks)


@pytest.mark.parametrize("verified_first", [False, True])
def test_two_authoritative_empty_cues_remain_empty_with_different_flags(verified_first):
    verified_empty = cue(1 if verified_first else 2, 1. if verified_first else 1.05,
                         1.05 if verified_first else 2., best=block(0., 5.))
    negative = cue(2 if verified_first else 1, 1.05 if verified_first else 1.,
                   2. if verified_first else 1.05, status="no_caption_evidence",
                   verified=False)
    segments = [verified_empty, negative] if verified_first else [negative, verified_empty]
    assert collect_source_blur_boxes(segments) == []

    coalesce_tiny_same_voice_cues(segments)

    assert len(segments) == 1
    assert collect_source_blur_boxes(segments) == []
    assert not segments[0].tracking_blocks
    assert segments[0].best_block is None


@pytest.mark.parametrize("unresolved_first", [False, True])
def test_merging_cues_does_not_erase_unresolved_geometry(unresolved_first):
    unresolved = cue(1 if unresolved_first else 2, 1. if unresolved_first else 1.05,
                     1.05 if unresolved_first else 2., status="unresolved", verified=False)
    observed = block(1.05, 2.) if unresolved_first else block(1., 1.05)
    located = cue(2 if unresolved_first else 1, 1.05 if unresolved_first else 1.,
                  2. if unresolved_first else 1.05, [observed])
    segments = [unresolved, located] if unresolved_first else [located, unresolved]

    coalesce_tiny_same_voice_cues(segments)

    assert any(segment.ocr_mask_status == "unresolved" for segment in segments)
    with pytest.raises(ValueError, match="OCR"):
        collect_source_blur_boxes(segments)


def test_unverified_best_only_observation_survives_later_tiny_cue():
    first = block(1., 1.05, verified=False)
    second = block(1.05, 2., left=.3, verified=False)
    segments = [cue(1, 1., 1.05, verified=False, best=first),
                cue(2, 1.05, 2., verified=False, best=second)]
    expected = collect_source_blur_boxes(segments)

    coalesce_tiny_same_voice_cues(segments)

    assert len(segments) == 1
    assert collect_source_blur_boxes(segments) == expected
    assert not segments[0].geometry_verified
    assert all(not row.geometry_verified for row in segments[0].tracking_blocks)
    assert all(row is not first and row is not second for row in segments[0].tracking_blocks)
