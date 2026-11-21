"""Exact recognition reuse must preserve each occurrence and short changes."""
from unittest.mock import patch

import numpy as np
import pytest

from backend.ocr_observation_ledger import (
    OCRObservationLedger, active_ledger, video_observations,
)


def identify(frames):
    return [[{"text": str(int(frame.flat[0])), "box": [1, 2, 3, 4]}] for frame in frames]


@pytest.mark.parametrize("length", [1, 2])
@pytest.mark.parametrize("value", [0, 2])
@pytest.mark.parametrize("offset", range(1, 11))
def test_short_changes_at_every_offset_keep_occurrence_times(length, value, offset):
    values = [1] * 14
    values[offset:offset + length] = [value] * length
    ledger = OCRObservationLedger(("source", 7))
    results = []
    pts = [index * .031 + (index // 4) * .02 for index in range(len(values))]
    for start in range(0, len(values), 8):
        indices = range(start, min(start + 8, len(values)))
        results.extend(ledger.recognize(
            [np.full((6, 6, 3), values[i], np.uint8) for i in indices],
            identify, ("model", "full-frame"), [(0, i, pts[i]) for i in indices]))
    assert [int(rows[0]["text"]) for rows in results] == values
    assert [key[1][2] for key in ledger.observations] == pts
    assert ledger.metrics["ocr_unique_inputs"] == 2
    assert ledger.metrics["ocr_reused_inputs"] == len(values) - 2
    results[0][0]["box"][0] = 999
    assert results[-1][0]["box"][0] == 1


def test_hash_collision_still_requires_identical_bytes():
    ledger = OCRObservationLedger("source")
    a, b = [np.full((6, 6, 3), value, np.uint8) for value in (1, 2)]
    class Collision:
        def digest(self):
            return b"collision"
    with patch("backend.ocr_observation_ledger.hashlib.sha256", return_value=Collision()):
        first = ledger.recognize([a, b, a.copy()], identify, "model")
        second = ledger.recognize([b, a], identify, "model")
    assert [row[0]["text"] for row in first] == ["1", "2", "1"]
    assert [row[0]["text"] for row in second] == ["2", "1"]
    assert ledger.metrics["ocr_unique_inputs"] == 2


def test_dtype_shape_and_model_policy_invalidate_reuse():
    ledger = OCRObservationLedger("source")
    image = np.ones((8, 8), np.uint8)
    calls = []
    def recognize(frames):
        calls.extend(frames)
        return identify(frames)
    ledger.recognize([image], recognize, "model-a")
    ledger.recognize([image.copy()], recognize, "model-a")
    ledger.recognize([image], recognize, "model-b")
    ledger.recognize([image.view(np.int8)], recognize, "model-a")
    ledger.recognize([image.reshape((4, 16))], recognize, "model-a")
    assert len(calls) == 4


def test_cache_memory_and_entries_remain_bounded_after_eviction():
    ledger = OCRObservationLedger("source", max_bytes=300, max_entries=2)
    for value in range(20):
        image = np.full((6, 6, 3), value, np.uint8)
        assert ledger.recognize([image], identify, "model")[0][0]["text"] == str(value)
        assert ledger.bytes <= 300
        assert len(ledger.entries) <= 2
    assert ledger.metrics["ocr_cache_evictions"] > 0
    # An input bigger than the cache still gets recognized without being stored.
    large = np.random.default_rng(5).integers(0, 256, (64, 64, 3), dtype=np.uint8)
    ledger.recognize([large], identify, "model")
    assert ledger.bytes <= 300


def test_incomplete_or_failed_recognition_cannot_poison_cache():
    ledger = OCRObservationLedger("source")
    image = np.ones((4, 4), np.uint8)
    with pytest.raises(RuntimeError, match="incomplete"):
        ledger.recognize([image], lambda _: [], "model")
    assert not ledger.entries
    with pytest.raises(InterruptedError):
        ledger.recognize([image], lambda _: (_ for _ in ()).throw(InterruptedError()), "model")
    assert not ledger.entries
    assert ledger.recognize([image], identify, "model")[0][0]["text"] == "1"


def test_video_scope_isolation_nested_failure_and_cleanup(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"fixture")
    saved = []
    @video_observations
    def nested(_path, **kwargs):
        assert active_ledger() is not saved[-1]
        raise InterruptedError()
    @video_observations
    def run(_path, **kwargs):
        ledger = active_ledger()
        saved.append(ledger)
        ledger.recognize([np.ones((4, 4), np.uint8)], identify, "model")
        with pytest.raises(InterruptedError):
            nested(path)
        assert active_ledger() is ledger
    run(path)
    run(path)
    assert saved[0] is not saved[1]
    assert saved[0].source == saved[1].source
    assert all(not ledger.entries and ledger.bytes == 0 for ledger in saved)
    assert active_ledger() is None


def test_unscoped_qc_calls_do_not_inherit_source_recognition():
    from backend import ocr_utils as ocr
    image = np.ones((6, 6, 3), np.uint8)
    with patch.object(ocr, "_recognize_batch", side_effect=identify) as recognize:
        ocr._readtext_batch([image, image])
        ocr._readtext_batch([image])
    assert recognize.call_count == 2
    assert len(recognize.call_args_list[0].args[0]) == 1


@pytest.mark.parametrize("height,width", [(401, 240), (401, 720), (401, 401)])
def test_model_input_reuse_maps_each_box_and_recognizes_one_pixel_change(height, width):
    from backend import ocr_utils as ocr

    first = np.zeros((height, width, 3), np.uint8)
    second = np.zeros((height + 1, width, 3), np.uint8)
    second[0] = 255  # Outside the recognized band.
    changed = second.copy()
    changed[301, 0, 0] = 1
    box = [[0, 0], [10, 0], [10, 5], [0, 5]]

    def recognize(images):
        assert [image.shape for image in images] == [(77, width, 3)] * 2
        return [[(box, str(image[0, 0, 0]), 1.0)] for image in images]

    with ocr._ocr_band_roi((.75, .94)), patch.object(ocr, "_recognize_batch", side_effect=recognize):
        results = ocr._readtext_batch([first, second, changed, second])
    assert [rows[0][1] for rows in results] == ["0", "0", "1", "0"]
    assert [rows[0][0][0][1] for rows in results] == [300, 301, 301, 301]


@pytest.mark.parametrize("roi,height", [(None, 400), ((.1, .95), 400),
                                        ((.5, .55), 400), ((.2, .7), 128)])
def test_full_frame_fallback_includes_changes_anywhere(roi, height):
    from backend import ocr_utils as ocr

    first = np.zeros((height, 200, 3), np.uint8)
    second = first.copy()
    second[0, 0, 0] = 1
    with ocr._ocr_band_roi(roi), patch.object(ocr, "_recognize_batch", side_effect=identify) as run:
        results = ocr._readtext_batch([first, second])
    assert [image.shape for image in run.call_args.args[0]] == [first.shape, second.shape]
    assert [rows[0]["text"] for rows in results] == ["0", "1"]


def test_pipeline_shares_ledger_across_coarse_rescue_and_source_passes(tmp_path):
    from contextlib import ExitStack
    from types import SimpleNamespace
    from backend import ocr_utils as ocr
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'fixture')
    a = np.ones((6, 6, 3), np.uint8)
    b = a.copy()
    b[0, 0, 0] = 2
    metrics = {}
    band = SimpleNamespace(support=1, mode='data')
    capture = SimpleNamespace(release=lambda: None)
    def coarse(*_):
        ocr._readtext_batch([a])
    def rescue(*_):
        ocr._readtext_batch([a.copy()])
        return band
    def source_pass(*args, **kwargs):
        ocr._readtext_batch([a, b])
        kwargs['metrics']['visual_ocr_calls'] = 1
        return [object()], .8
    replacements = {
        '_open_ocr_video': lambda *_: (capture, 30, 6, 6, 1),
        '_choose_ocr_strategy': lambda *_: 'full',
        '_ocr_target_timestamps': lambda *_: ([], False, metrics),
        '_ocr_segment_texts': lambda *_: {}, '_collect_ocr_samples': coarse,
        '_append_ocr_recognized': lambda *a, **k: None,
        'select_chinese_subtitle_band': lambda **_: band,
        '_rescue_ocr_coordinates': rescue, '_initialize_ocr_geometry': lambda *_: None,
        '_apply_ocr_subtitle_band': lambda *_: .8,
        '_report_ocr_geometry': lambda *_: None,
        '_perform_visual_subtitle_ocr': source_pass, '_attach_source_caption_tracks': lambda *_: None,
    }
    with ExitStack() as stack:
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(ocr, name, replacement))
        recognize = stack.enter_context(patch.object(ocr, '_recognize_batch', side_effect=identify))
        # Bypass media preparation and the outer cleanup, exercise the actual
        # scoped pipeline wrapper and body without loading a model.
        ocr.perform_video_ocr.__wrapped__.__wrapped__(source, srt_segments=[object()], metrics=metrics)
    assert recognize.call_count == 2
    assert metrics['ocr_requested_frames'] == 4
    assert metrics['ocr_unique_inputs'] == 2
    assert metrics['ocr_reused_inputs'] == 2
    assert metrics['ocr_cache_bytes'] == 0
    assert metrics['ocr_cache_peak_bytes'] > 0
