"""Failed source timing retains counters without claiming verified output."""
from contextlib import ExitStack
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend import ocr_utils as ocr
from backend.v1_stage_runtime import save_payload


@pytest.mark.parametrize('write_fails', [False, True])
def test_source_clock_failure_keeps_final_metrics_and_original_exception(tmp_path, write_fails):
    report_path = tmp_path / 'geometry.json'
    save_payload(report_path, geometry={'counts': {'located': 10}},
                 sampling={'ocr_requested_frames': 19})
    metrics = {'ocr_requested_frames': 19}
    band = SimpleNamespace(support=1, mode='data')
    capture = SimpleNamespace(release=Mock())
    original_error = ValueError('Caption timestamps must increase within the source duration')

    def source_pass(*args, **kwargs):
        kwargs['metrics'].update(ocr_requested_frames=400, ocr_runtime_seconds=120.,
                                 visual_boundary_frames=281)
        raise original_error

    replacements = {
        '_open_ocr_video': lambda *_: (capture, 30, 1080, 1920, 35.),
        '_choose_ocr_strategy': lambda *_: 'full',
        '_ocr_target_timestamps': lambda *_: ([], False, metrics),
        '_ocr_segment_texts': lambda *_: {}, '_collect_ocr_samples': lambda *_: None,
        '_append_ocr_recognized': lambda *a, **k: None,
        'select_chinese_subtitle_band': lambda **_: band,
        '_rescue_ocr_coordinates': lambda *a: band,
        '_initialize_ocr_geometry': lambda *_: None,
        '_apply_ocr_subtitle_band': lambda *_: .8,
        '_report_ocr_geometry': lambda *_: None,
        '_perform_visual_subtitle_ocr': source_pass,
    }
    with ExitStack() as stack:
        for name, replacement in replacements.items():
            stack.enter_context(patch.object(ocr, name, replacement))
        if write_fails:
            stack.enter_context(patch('backend.v1_stage_runtime.save_payload',
                                      side_effect=OSError('report unavailable')))
        attach = stack.enter_context(patch.object(ocr, '_attach_source_caption_tracks'))
        with pytest.raises(ValueError) as caught:
            # Skip media preparation and run the actual pipeline body.
            ocr.perform_video_ocr.__wrapped__.__wrapped__(
                'source.mp4', srt_segments=[object()], metrics=metrics,
                geometry_report_path=str(report_path))
    assert caught.value is original_error
    capture.release.assert_called_once()
    attach.assert_not_called()
    saved = json.loads(report_path.read_text(encoding='utf-8'))
    assert saved['geometry'] == {'counts': {'located': 10}}
    if write_fails:
        assert saved['sampling']['ocr_requested_frames'] == 19
    else:
        assert saved['status'] == 'failed'
        assert saved['failed_phase'] == 'source_caption_timeline'
        assert saved['geometry_is_initial_snapshot'] is True
        assert saved['sampling']['ocr_requested_frames'] == 400
        assert saved['sampling']['visual_boundary_frames'] == 281
        assert saved['sampling']['source_timeline_seconds'] >= 0


def test_failed_metrics_without_report_path_do_not_write():
    with patch('backend.v1_stage_runtime.save_payload') as save:
        ocr._save_failed_source_timeline_metrics({}, {'ocr_requested_frames': 400})
    save.assert_not_called()
