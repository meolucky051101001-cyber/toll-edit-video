from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from backend.visual_caption_timing import refine_observations, observation_intervals
from backend.v1_quality_gate import _transition_pixel_candidates


def test_changed_caption_and_empty_gap_are_refined_to_source_frames():
    def observe(frame):
        return 'short' if frame < 13 else ('wide' if frame < 18 else '')
    samples, extra = refine_observations({0: 'short', 10: 'short', 20: ''}, 30,
                                        observe, lambda x: x)
    intervals = list(observation_intervals(samples, 30, 1))
    for frame in range(30):
        active = [text for text, start, end in intervals if start <= frame/30 < end]
        assert active == [observe(frame)]
    assert extra < 20


def test_boundary_budget_and_cancellation_do_not_claim_success():
    with pytest.raises(RuntimeError, match='budget'):
        refine_observations({0: 'a', 29: 'b'}, 30, lambda f: 'a', lambda x: x, max_extra_frames=0)
    with pytest.raises(RuntimeError, match='stop'):
        refine_observations({0: 'a', 29: 'b'}, 30, lambda f: 'a', lambda x: x, stopped=lambda: True)


def test_visual_pipeline_masks_wider_caption_on_its_first_frame():
    from backend import ocr_utils as ocr
    from backend.subtitle_blur import collect_source_blur_boxes

    class Capture:
        index = 0
        def set(self, prop, value):
            self.index = round(value * 30 / 1000) if prop == cv2.CAP_PROP_POS_MSEC else int(value)
        def get(self, prop):
            return 30 if prop == cv2.CAP_PROP_FPS else max(0, self.index - 1) * 1000 / 30
        def read(self):
            frame = np.zeros((640, 360, 3), np.uint8)
            frame[0, 0, 0] = self.index
            self.index += 1
            return True, frame

    def recognize(images):
        result = []
        for frame in images:
            wide = int(frame[0, 0, 0]) >= 13
            left, right = (60, 300) if wide else (120, 240)
            text = '这是比较长的字幕' if wide else '短字幕'
            result.append([([[left, 520], [right, 520], [right, 550], [left, 550]], text, .99)])
        return result

    with patch.object(ocr, '_readtext_batch', side_effect=recognize), \
         patch.object(ocr, 'select_chinese_subtitle_band', return_value=SimpleNamespace(top=.8, bottom=.9, support=5, mode='data')), \
         patch.object(ocr, 'detect_static_watermarks', return_value=[]):
        segments, _ = ocr._perform_visual_subtitle_ocr(Capture(), 360, 640, 1, [], lambda: False)
    boxes = collect_source_blur_boxes(segments)
    for frame in range(30):
        active = [box for box in boxes if box['start'] <= frame/30 < box['end']]
        assert active, frame
        if frame >= 13:
            assert min(b['x_pct'] for b in active) < .2, frame
        else:
            assert min(b['x_pct'] for b in active) > .3, frame


def test_qc_audits_old_caption_boundary_uncertainty_independent_of_speech():
    def track(text, start, end, left, right):
        return SimpleNamespace(text=text, start=start, end=end, x_pct=left, max_x_pct=right,
                               y_pct=.82, max_y_pct=.87)
    segment = SimpleNamespace(index=1, start=0, end=30, tracking_blocks=[
        track('短字幕', 12.13, 13.1, .36, .64),
        track('这是比较长的字幕', 13.18, 14.5, .27, .73)])
    candidates, total = _transition_pixel_candidates([segment])
    assert total == len(candidates)
    assert any(13 <= c['mid_s'] < 13.1 and c['box'][0] == .27 for c in candidates)


def test_qc_transition_sampling_reports_when_capped():
    tracks = [SimpleNamespace(text=str(i), start=i, end=i+.9, x_pct=.2, max_x_pct=.8,
                              y_pct=.8, max_y_pct=.9) for i in range(50)]
    candidates, total = _transition_pixel_candidates([SimpleNamespace(index=1, tracking_blocks=tracks)], limit=96)
    assert len(candidates) == 96
    assert total > 96


@pytest.mark.parametrize('complete', [True, False])
def test_pixel_qc_checks_short_cues_and_never_passes_missing_ocr_results(tmp_path, complete):
    from contextlib import nullcontext
    from backend.v1_quality_gate import check_rendered_pixel_coverage
    import ocr_utils
    video = tmp_path / 'render.mp4'
    video.write_bytes(b'fixture')
    block = SimpleNamespace(text='短字幕', start=.1, end=.2, x_pct=.2, max_x_pct=.8,
                            y_pct=.8, max_y_pct=.9)
    segment = SimpleNamespace(index=1, start=.1, end=.2, ocr_mask_status='located',
                              best_block=block, tracking_blocks=[block])
    capture = SimpleNamespace(isOpened=lambda: True, set=lambda *a: None,
        read=lambda: (True, np.zeros((640, 360, 3), dtype=np.uint8)), release=lambda: None,
        get=lambda prop: 30 if prop in (cv2.CAP_PROP_FPS, cv2.CAP_PROP_FRAME_COUNT) else 0)
    with patch('cv2.VideoCapture', return_value=capture), \
         patch('v1_gpu_gatekeeper.gpu_gatekeeper', return_value=nullcontext()), \
         patch.object(ocr_utils, '_readtext_batch', side_effect=lambda images: [[] for _ in images] if complete else []), \
         patch.object(ocr_utils, 'release_ocr_reader'):
        result = check_rendered_pixel_coverage(video, [segment])
    assert result.status == ('PASS' if complete else 'UNKNOWN')
    if complete:
        assert result.details['transition_samples'] > 0
