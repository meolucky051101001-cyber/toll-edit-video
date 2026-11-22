"""Per-cue caption rows for sources whose burnt-in subtitles move per scene."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from backend import ocr_subtitle_locator as locator
from backend.ocr_subtitle_locator import (
    CueCaptionRow, locate_cue_caption_rows, per_cue_caption_mode_needed,
    select_chinese_subtitle_band,
)
from backend.cloud.gpu_scripts import caption_geometry as geometry


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cloud_b610d6_ocr_blocks.json"


@pytest.fixture(scope="module")
def source_data():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def segment_texts(source_data):
    return {s["id"]: s["text"] for s in source_data["segments"]}


@pytest.fixture(scope="module")
def cue_rows(source_data, segment_texts):
    return locate_cue_caption_rows(
        source_data["blocks"], segment_texts,
        source_data["frame_width"], source_data["frame_height"],
    )


def _center(row):
    return (row.top + row.bottom) * 0.5


def test_global_band_still_collapses_to_the_sticker_row(source_data, segment_texts):
    band = select_chinese_subtitle_band(
        source_data["blocks"], segment_texts,
        frame_width=source_data["frame_width"], frame_height=source_data["frame_height"],
        duration=source_data["duration"],
    )
    assert band.mode == "asr_match"
    assert band.top == pytest.approx(0.6234, abs=1e-3)


def test_locate_cue_caption_rows_finds_each_scene_position(cue_rows):
    assert set(cue_rows) == set(range(1, 14))
    assert _center(cue_rows[5]) == pytest.approx(0.383, abs=0.04)
    assert _center(cue_rows[7]) == pytest.approx(0.795, abs=0.045)
    assert _center(cue_rows[8]) == pytest.approx(0.795, abs=0.045)
    assert _center(cue_rows[11]) == pytest.approx(0.642, abs=0.04)
    # Cue 9's two-line sample and one-line sample merge by vertical overlap.
    assert 0.67 <= _center(cue_rows[9]) <= 0.72
    for sid in (2, 3, 4):
        assert 0.69 <= _center(cue_rows[sid]) <= 0.79
    # Cue 1 must land on the dialogue row, not the bottle label at ~0.40-0.46.
    assert 0.69 <= _center(cue_rows[1]) <= 0.75
    # No chosen row may sit on the static watermark at 0.12-0.17.
    assert all(not 0.12 <= _center(row) <= 0.17 for row in cue_rows.values())


def _block(sid, sample_time, text, x1, x2, y1, y2):
    return {"text": text, "x_pct": x1, "max_x_pct": x2, "y_pct": y1,
            "max_y_pct": y2, "prob": 0.0, "sample_segment_id": sid,
            "sample_time": sample_time, "start": 0.0, "end": 10.0,
            "is_subtitle": True, "is_packaging": False}


def test_row_clustering_uses_vertical_overlap_not_center_distance():
    # A two-line row (center ~0.699) and a one-line row (center ~0.682) sit
    # 0.0175 apart - beyond the old center-distance tolerance - yet overlap.
    blocks = [
        _block(1, 1.0, "鬼冒冒哥煦寇思", 0.28, 0.72, 0.662, 0.700),
        _block(1, 1.0, "恩名芄}", 0.40, 0.60, 0.699, 0.735),
        _block(1, 2.0, "鬼冒聱晷恩", 0.28, 0.72, 0.662, 0.702),
    ]
    rows = locate_cue_caption_rows(blocks, {1: "字幕文本字幕"}, 1920, 1080)
    assert set(rows) == {1}
    assert rows[1].top <= 0.662 and rows[1].bottom >= 0.735
    assert rows[1].support == 2


def test_single_sample_cluster_falls_back_to_visual_weak():
    # Two samples with rows at different heights: both support-1 clusters are
    # weak evidence, but the cue still gets a row instead of being dropped.
    blocks = [
        _block(1, 1.0, "第一行字幕", 0.30, 0.70, 0.70, 0.74),
        _block(1, 2.0, "完全不同的字", 0.30, 0.70, 0.40, 0.44),
    ]
    rows = locate_cue_caption_rows(blocks, {1: "字幕文本"}, 1920, 1080)
    assert set(rows) == {1}
    assert rows[1].source == "visual_weak"
    # With no decided neighbour, the lower row (cy >= 0.5) is preferred.
    assert _center(rows[1]) == pytest.approx(0.72, abs=1e-6)


def test_per_cue_trigger_fires_on_moving_captions(source_data, cue_rows):
    enabled, outside = per_cue_caption_mode_needed(
        cue_rows, 0.6234, 0.6609, anchored=True, cue_count=13,
    )
    assert enabled
    assert len(outside) >= 2


def test_per_cue_trigger_stays_off_for_static_captions():
    rows = {sid: CueCaptionRow(0.63, 0.66, 0.2, 0.8, "visual", 3, 0.9)
            for sid in range(1, 6)}
    enabled, outside = per_cue_caption_mode_needed(
        rows, 0.62, 0.67, anchored=True, cue_count=5,
    )
    assert not enabled and outside == []


def test_per_cue_trigger_stays_off_with_few_outside_rows():
    rows = {sid: CueCaptionRow(0.63, 0.66, 0.2, 0.8, "visual", 3, 0.9)
            for sid in range(1, 7)}
    rows[3] = CueCaptionRow(0.36, 0.40, 0.2, 0.8, "visual", 3, 0.9)
    enabled, outside = per_cue_caption_mode_needed(
        rows, 0.62, 0.67, anchored=True, cue_count=13,
    )
    assert not enabled and outside == [3]


def test_observe_caption_boxes_unions_distinct_bands(monkeypatch):
    answers = iter([
        [[.2, .36, .8, .40]], [[.2, .36, .8, .40]],   # band A scales 1., 1.5
        [[.2, .63, .8, .66]], [[.2, .63, .8, .66]],   # band B scales 1., 1.5
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(answers))
    frame = np.zeros((1920, 1080, 3))
    measured = geometry.observe_caption_boxes(
        None, frame, bands=[(.35, .41), (.62, .67)])
    assert sorted(b[1] for b in measured) == [.36, .63]


def test_observe_caption_boxes_dedupes_overlapping_band_results(monkeypatch):
    answers = iter([
        [[.2, .63, .8, .66]], [[.2, .63, .8, .66]],
        [[.21, .631, .8, .661]], [[.21, .631, .8, .661]],
    ])
    monkeypatch.setattr(geometry, "_detect", lambda *a, **kw: next(answers))
    frame = np.zeros((1920, 1080, 3))
    measured = geometry.observe_caption_boxes(
        None, frame, bands=[(.62, .67), (.63, .68)])
    assert len(measured) == 1


def test_uncovered_caption_cues_thresholds():
    rows = {sid: (0.30, 0.40) for sid in range(1, 14)}
    segments = [{"id": sid, "start": float(sid), "end": float(sid) + 1.0,
                 "tracking_blocks": [{"start": float(sid), "end": float(sid) + 1.0,
                                      "y_pct": 0.30, "max_y_pct": 0.40}]}
                for sid in range(1, 14)]
    assert geometry.uncovered_caption_cues(segments, rows) == []
    segments[0]["tracking_blocks"] = []
    assert geometry.uncovered_caption_cues(segments, rows) == [1]
    segments[1]["tracking_blocks"] = [{"start": 2.0, "end": 3.0,
                                       "y_pct": 0.70, "max_y_pct": 0.74}]
    assert geometry.uncovered_caption_cues(segments, rows) == [1, 2]


def test_uncovered_caption_cues_requires_time_coverage():
    row = {1: (0.30, 0.40)}
    segment = {"id": 1, "start": 0.0, "end": 2.2, "tracking_blocks": []}
    # Masks at the right row but only covering 0.4s of the 2.2s cue window.
    segment["tracking_blocks"] = [{"start": 0.0, "end": 0.4,
                                   "y_pct": 0.30, "max_y_pct": 0.40}]
    assert geometry.uncovered_caption_cues([segment], row) == [1]
    # ~82% of the window covered (two touching blocks merge).
    segment["tracking_blocks"] = [
        {"start": 0.0, "end": 0.9, "y_pct": 0.30, "max_y_pct": 0.40},
        {"start": 0.8, "end": 1.8, "y_pct": 0.30, "max_y_pct": 0.40},
    ]
    assert geometry.uncovered_caption_cues([segment], row) == []
    # Blocks clipped to the cue window: coverage outside the cue does not count.
    segment["tracking_blocks"] = [{"start": 3.0, "end": 9.0,
                                   "y_pct": 0.30, "max_y_pct": 0.40}]
    assert geometry.uncovered_caption_cues([segment], row) == [1]


def test_download_qc_fails_on_uncovered_caption_rows(tmp_path, monkeypatch):
    from backend.cloud.cloud_orchestrator import CloudOrchestrator, _OrchestratorStageError
    from backend.cloud.cloud_job_store import CloudJobStore
    from backend.cloud import cloud_orchestrator
    output = tmp_path / "output_video.mp4"
    output.write_bytes(b"\x00" * 64)
    meta = dict(
        status="completed", segments_count=13, translated_count=13, tts_verified_count=13,
        input_duration_seconds=1.,
        source_caption_evidence=dict(
            version=1, passed=True, cue_count=13,
            verified_cues=[str(i) for i in range(1, 14)], unresolved_cues=[],
            sampled_frames=3, caption_samples=1, empty_samples=2, source_mask_count=3),
        subtitle_timing={"passed": True, "cue_count": 13, "source_mask_count": 3},
        caption_band={"mode": "per_cue", "global_band": [0.62, 0.66],
                      "cue_rows": {}, "uncovered_cues": [5, 7]},
    )
    (tmp_path / "result_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(cloud_orchestrator, "probe_video_metadata", lambda _: dict(
        probe_method="ffprobe", video_codec="h264", audio_codec="aac", duration_seconds=1.))
    orch = CloudOrchestrator(store=CloudJobStore(tmp_path / "jobs.db"), adapter=SimpleNamespace())
    with pytest.raises(_OrchestratorStageError, match="chưa được che"):
        orch.validate_downloaded_output(output, tmp_path)
    assert json.loads((tmp_path / "qc_report.json").read_text(encoding="utf-8"))["passed"] is False


def test_download_qc_tolerates_missing_caption_band(tmp_path, monkeypatch):
    from backend.cloud.cloud_orchestrator import CloudOrchestrator
    from backend.cloud.cloud_job_store import CloudJobStore
    from backend.cloud import cloud_orchestrator
    output = tmp_path / "output_video.mp4"
    output.write_bytes(b"\x00" * 64)
    meta = dict(
        status="completed", segments_count=1, translated_count=1, tts_verified_count=1,
        input_duration_seconds=1.,
        source_caption_evidence=dict(
            version=1, passed=True, cue_count=1, verified_cues=["1"], unresolved_cues=[],
            sampled_frames=3, caption_samples=1, empty_samples=2, source_mask_count=1),
        subtitle_timing={"passed": True, "cue_count": 1, "source_mask_count": 1},
    )
    (tmp_path / "result_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(cloud_orchestrator, "probe_video_metadata", lambda _: dict(
        probe_method="ffprobe", video_codec="h264", audio_codec="aac", duration_seconds=1.))
    orch = CloudOrchestrator(store=CloudJobStore(tmp_path / "jobs.db"), adapter=SimpleNamespace())
    assert orch.validate_downloaded_output(output, tmp_path)["qc_report"]["passed"]


def test_download_qc_passes_with_single_uncovered_cue(tmp_path, monkeypatch):
    from backend.cloud.cloud_orchestrator import CloudOrchestrator
    from backend.cloud.cloud_job_store import CloudJobStore
    from backend.cloud import cloud_orchestrator
    output = tmp_path / "output_video.mp4"
    output.write_bytes(b"\x00" * 64)
    meta = dict(
        status="completed", segments_count=13, translated_count=13, tts_verified_count=13,
        input_duration_seconds=1.,
        source_caption_evidence=dict(
            version=1, passed=True, cue_count=13,
            verified_cues=[str(i) for i in range(1, 14)], unresolved_cues=[],
            sampled_frames=3, caption_samples=1, empty_samples=2, source_mask_count=3),
        subtitle_timing={"passed": True, "cue_count": 13, "source_mask_count": 3},
        caption_band={"mode": "per_cue", "global_band": [0.62, 0.66],
                      "cue_rows": {}, "uncovered_cues": [9]},
    )
    (tmp_path / "result_metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setattr(cloud_orchestrator, "probe_video_metadata", lambda _: dict(
        probe_method="ffprobe", video_codec="h264", audio_codec="aac", duration_seconds=1.))
    orch = CloudOrchestrator(store=CloudJobStore(tmp_path / "jobs.db"), adapter=SimpleNamespace())
    report = orch.validate_downloaded_output(output, tmp_path)["qc_report"]
    assert report["passed"]
    assert "caption_row_coverage" in report["checks"]
