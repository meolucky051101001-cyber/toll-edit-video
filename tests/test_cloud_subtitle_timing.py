"""Cloud ASS regressions exercise the real V1 layout with independent masks."""
import copy
from pathlib import Path

import pytest

from backend.cloud.gpu_scripts.subtitle_timing import (
    SubtitleTimingError, generate_cloud_ass, validate_cloud_ass,
)


def box(start, end, **overrides):
    result = dict(start=start, end=end, x_pct=.2, max_x_pct=.8,
                  y_pct=.72, max_y_pct=.77, text="示例字幕", prob=.98)
    result.update(overrides)
    return result


def cue(cid, start, end, text, **overrides):
    return dict(id=cid, start=start, end=end, text="示例字幕", translated_text=text,
                ocr_mask_status="located", **overrides)


def render(tmp_path, segments, duration=40):
    target = tmp_path / "subtitles.ass"
    audit = generate_cloud_ass(segments, target, video_w=720, video_h=960,
                               main_y_pct=.72, video_duration=duration)
    return target, audit


def expected_from_audit(audit):
    # Caller keeps original translated text; audit intentionally stores no duplicate text.
    return [{**row, "name": f"cloud_cue_{row['id']}", "text": "Bản dịch đầy đủ"}
            for row in audit["cues"]]


def test_short_tts_never_truncates_source_text_or_masks(tmp_path):
    segments = [cue(1, 0, 27.46, "Bản dịch đầy đủ", tts_duration=19.89,
                    tracking_blocks=[box(0, 10), box(10, 27.86)])]
    before = copy.deepcopy(segments)
    target, audit = render(tmp_path, segments)
    assert segments == before
    assert audit["passed"] and audit["cues"][0]["end"] == 27.46
    assert audit["cues"][0]["uncovered_intervals"] == []
    assert max(m["end"] for m in audit["source_masks"]) == 27.86
    rows = target.read_text(encoding="utf-8-sig")
    assert "0:00:27.46,TextStyle,cloud_cue_1" in rows
    assert "0:00:27.86,CloudSourceMask" in rows


def test_caption_lifetime_is_independent_of_dialogue_and_next_cue(tmp_path):
    segments = [cue(1, 1, 2, "Bản dịch đầy đủ", tts_duration=.6,
                    tracking_blocks=[box(.3, 3)]),
                cue(2, 2.5, 3.5, "Nội dung tiếp theo", tts_duration=1,
                    tracking_blocks=[box(2.5, 3.7, y_pct=.15, max_y_pct=.2)])]
    _, audit = render(tmp_path, segments, duration=4)
    assert [(c["start"], c["end"]) for c in audit["cues"]] == [(1, 2), (2.5, 3.5)]
    assert [(m["start"], m["end"]) for m in audit["source_masks"]] == [(.3, 3), (2.5, 3.7)]
    assert all(not m["uncovered_intervals"] for m in audit["source_masks"])


def test_missing_mask_or_text_in_actual_ass_is_rejected(tmp_path):
    segments = [cue(1, 0, 3, "Bản dịch đầy đủ", tts_duration=1, best_block=box(0, 3))]
    target, audit = render(tmp_path, segments, duration=4)
    original = target.read_text(encoding="utf-8-sig")
    target.write_text("\n".join(row for row in original.splitlines()
                                if not row.startswith("Dialogue: 0,") or ",CloudSourceMask," not in row), encoding="utf-8")
    with pytest.raises(SubtitleTimingError, match="not fully masked"):
        validate_cloud_ass(target, expected_from_audit(audit), audit["source_masks"], video_duration=4)
    target.write_text(original.replace("Bản dịch đầy đủ", "Thiếu nội dung"), encoding="utf-8")
    with pytest.raises(SubtitleTimingError, match="Missing/reordered"):
        validate_cloud_ass(target, expected_from_audit(audit), audit["source_masks"], video_duration=4)


def test_text_uses_source_start_and_long_tts_cannot_cross_next_cue(tmp_path):
    segments = [cue(1, 1, 2, "Bản dịch đầy đủ", tts_duration=8, best_block=box(1, 2)),
                cue(2, 3, 4, "Nội dung tiếp theo", tts_duration=.4, best_block=box(3, 4))]
    _, audit = render(tmp_path, segments, duration=5)
    assert audit["cues"][0]["start"] == 1
    assert audit["cues"][0]["end"] == 3
    assert audit["cues"][1]["start"] == 3
    assert audit["cues"][1]["end"] == 4


def test_scene_text_is_not_masked_and_missing_translation_fails(tmp_path):
    _, audit = render(tmp_path, [cue(1, 0, 2, "Bản dịch đầy đủ",
                                    best_block=box(0, 2, is_packaging=True))])
    assert not audit["source_masks"]
    with pytest.raises(SubtitleTimingError, match="Missing translation"):
        render(tmp_path, [cue(1, 0, 2, "")])


def test_real_pagination_preserves_text_and_timing(tmp_path):
    long_text = ("Đây là câu hướng dẫn đầu tiên. Câu thứ hai giải thích thao tác tiếp theo. "
                 "Sau đó gập mép giấy rồi ấn vào hộp. Phần giấy thừa được gập lại ngay ngắn.")
    target, audit = render(tmp_path, [cue(1, 0, 14, long_text, tts_duration=8,
                                         tracking_blocks=[box(0, 7), box(7, 14.2)])])
    assert audit["cues"][0]["text_matches"]
    assert audit["cues"][0]["event_count"] >= 4
    assert not audit["cues"][0]["uncovered_intervals"]


@pytest.mark.parametrize("field,value", [("end", float("nan")), ("start", -1), ("tts_duration", -1)])
def test_invalid_timing_cannot_produce_accepted_ass(tmp_path, field, value):
    segment = cue(1, 0, 2, "Bản dịch đầy đủ", best_block=box(0, 2))
    segment[field] = value
    with pytest.raises(SubtitleTimingError):
        render(tmp_path, [segment])
