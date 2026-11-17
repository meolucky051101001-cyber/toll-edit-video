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


def test_stable_endpoints_do_not_hide_short_source_caption_and_silence():
    def observe(frame):
        return 'a' if frame < 5 or frame > 14 else ('wide' if frame < 10 else '')
    samples, _ = refine_observations({0:'a',29:'a'},30,observe,lambda x:x,max_stable_gap_frames=3)
    intervals = list(observation_intervals(samples,30,1))
    for frame in range(30):
        assert [text for text,start,end in intervals if start <= frame/30 < end] == [observe(frame)]


def test_batched_refinement_preserves_sample_indices_and_rejects_missing_results():
    def observe(index):
        return 'a' if index < 5 or index > 14 else ('wide' if index < 10 else '')

    observations = {index: observe(index) for index in (0, 9, 20, 29)}
    options = dict(max_stable_gap_frames=3)
    expected = refine_observations(observations, 30, observe, str, **options)
    batches = []

    def batch(indices):
        batches.append(indices)
        return [observe(index) for index in indices]

    assert refine_observations(observations, 30, None, str, observe_batch=batch, **options) == expected
    assert any(len(indices) > 1 for indices in batches)
    with pytest.raises(RuntimeError, match='incomplete'):
        refine_observations(observations, 30, None, str, observe_batch=lambda indices: [], **options)


@pytest.mark.parametrize('inside', [True,False])
def test_confirmed_source_glyph_leaks_fail_but_nearby_scene_text_warns(tmp_path,inside):
    from backend import ocr_utils as ocr
    from backend.v1_quality_gate import check_rendered_pixel_coverage
    video = tmp_path/'out.mp4'; video.write_bytes(b'fixture')
    seg = SimpleNamespace(index=1,start=0.,end=1.,ocr_mask_status='located',
        best_block=SimpleNamespace(x_pct=.2,max_x_pct=.8,y_pct=.8,max_y_pct=.85))
    cap = __import__('unittest.mock',fromlist=['MagicMock']).MagicMock()
    cap.isOpened.return_value = True
    cap.get.side_effect = lambda p: {cv2.CAP_PROP_FPS:30.,cv2.CAP_PROP_FRAME_COUNT:30}.get(p,0)
    cap.read.return_value = True,np.zeros((640,360,3),np.uint8)
    points = [[28,30],[140,30],[140,52],[28,52]] if inside else [[1,1],[20,1],[20,10],[1,10]]
    with patch('cv2.VideoCapture',return_value=cap), patch.object(ocr,'_readtext_batch',return_value=[[(points,'原字幕',.99)]]), patch.object(ocr,'release_ocr_reader'):
        result = check_rendered_pixel_coverage(video,[seg])
    assert result.status == ('FAIL' if inside else 'WARN')


def test_visual_pipeline_masks_wider_caption_on_its_first_frame():
    from backend import ocr_utils as ocr
    from backend.subtitle_blur import collect_source_blur_boxes

    class Capture:
        index = 0
        def set(self, prop, value):
            self.index = round(value * 30 / 1000) if prop == cv2.CAP_PROP_POS_MSEC else int(value)
        def get(self, prop):
            return {
                cv2.CAP_PROP_FPS: 30,
                cv2.CAP_PROP_FRAME_COUNT: 30,
                cv2.CAP_PROP_POS_FRAMES: self.index,
                cv2.CAP_PROP_POS_MSEC: max(0, self.index - 1) * 1000 / 30,
            }.get(prop, 0)
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
