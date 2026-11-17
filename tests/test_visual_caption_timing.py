from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from backend.visual_caption_timing import refine_observations, observation_intervals
from backend.v1_quality_gate import _transition_pixel_candidates


def _pixel_qc_capture():
    """QC consumes actual advancing PTS instead of seeking a static mock."""
    index = [0]
    def read():
        if index[0] >= 30:
            return False, None
        index[0] += 1
        return True, np.zeros((640, 360, 3), np.uint8)
    return SimpleNamespace(isOpened=lambda: True, read=read, release=lambda: None,
        get=lambda prop: {cv2.CAP_PROP_FPS: 30., cv2.CAP_PROP_FRAME_COUNT: 30,
                          cv2.CAP_PROP_POS_MSEC: max(0,index[0]-1)*1000/30,
                          cv2.CAP_PROP_POS_FRAMES:index[0]}.get(prop,0))


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


@pytest.mark.parametrize('inside', [True,False])
def test_confirmed_source_glyph_leaks_fail_but_nearby_scene_text_warns(tmp_path,inside):
    from backend import ocr_utils as ocr
    from backend.v1_quality_gate import check_rendered_pixel_coverage
    video = tmp_path/'out.mp4'; video.write_bytes(b'fixture')
    seg = SimpleNamespace(index=1,start=0.,end=1.,ocr_mask_status='located',
        best_block=SimpleNamespace(x_pct=.2,max_x_pct=.8,y_pct=.8,max_y_pct=.85))
    cap = _pixel_qc_capture()
    points = [[28,30],[140,30],[140,52],[28,52]] if inside else [[1,1],[20,1],[20,10],[1,10]]
    with patch('cv2.VideoCapture',return_value=cap), patch.object(ocr,'_readtext_batch',return_value=[[(points,'原字幕',.99)]]), patch.object(ocr,'release_ocr_reader'):
        result = check_rendered_pixel_coverage(video,[seg],rendered_duration_s=1.)
    assert result.status == ('FAIL' if inside else 'WARN')


def test_visual_pipeline_masks_wider_caption_on_its_first_frame():
    from backend import ocr_utils as ocr
    from backend.subtitle_blur import collect_source_blur_boxes

    class Capture:
        index = 0
        def set(self, prop, value):
            self.index = round(value * 30 / 1000) if prop == cv2.CAP_PROP_POS_MSEC else int(value)
        def get(self, prop):
            return {cv2.CAP_PROP_FPS: 30, cv2.CAP_PROP_FRAME_COUNT: 30,
                    cv2.CAP_PROP_POS_FRAMES: self.index,
                    cv2.CAP_PROP_POS_MSEC: max(0, self.index - 1) * 1000 / 30}.get(prop, 0)
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


def test_observation_intervals_use_decoded_pts_for_variable_frame_rate():
    samples = {0: 'short', 14: 'short', 15: 'wide', 29: 'wide'}
    pts = {0: 0., 14: 14/30, 15: .5, 29: 1.9}
    intervals = list(observation_intervals(samples, None, 2., frame_times=pts))
    assert intervals == [('short', 0., 14/30), ('short', 14/30, .5),
                         ('wide', .5, 1.9), ('wide', 1.9, 2.)]


@pytest.mark.parametrize('pts', [{0: 0.}, {0: 0., 1: 0.},
                                      {0: 0., 1: float('nan')}, {0: 0., 1: 2.}])
def test_observation_intervals_reject_unverified_frame_clock(pts):
    with pytest.raises(ValueError, match='timestamp|duration'):
        list(observation_intervals({0: 'a', 1: 'b'}, None, 2., frame_times=pts))


def test_refinement_rejects_observations_past_the_actual_frame_count():
    with pytest.raises(ValueError, match='frame range'):
        refine_observations({0: 'a', 30: 'b'}, 30, lambda _: None, lambda x: x)


@pytest.mark.parametrize('count,next_index,pts_ms', [(0, 1, 0), (30, 0, 0),
                                                    (30, 2, 0), (30, 1, -200), (30, 1, float('nan'))])
def test_visual_ocr_rejects_unavailable_decoder_clock_before_inference(count, next_index, pts_ms):
    from backend import ocr_utils as ocr
    capture = SimpleNamespace(set=lambda *_: True,
        read=lambda: (True, np.zeros((640, 360, 3), dtype=np.uint8)),
        get=lambda prop: {cv2.CAP_PROP_FRAME_COUNT: count, cv2.CAP_PROP_POS_FRAMES: next_index,
                          cv2.CAP_PROP_POS_MSEC: pts_ms}.get(prop, 0))
    with patch.object(ocr, '_visual_grid_times', return_value=[0]), \
         patch.object(ocr, '_readtext_batch') as inference:
        with pytest.raises(RuntimeError, match='frame|timestamp'):
            ocr._perform_visual_subtitle_ocr(capture, 360, 640, 1., [], lambda: False)
    inference.assert_not_called()


def test_visual_ocr_accepts_only_small_initial_decoder_preroll():
    from backend import ocr_utils as ocr
    capture = SimpleNamespace(set=lambda *_: True,
        read=lambda: (True, np.zeros((640, 360, 3), dtype=np.uint8)),
        get=lambda prop: {cv2.CAP_PROP_FRAME_COUNT: 30, cv2.CAP_PROP_POS_FRAMES: 1,
                          cv2.CAP_PROP_POS_MSEC: -1000/30}.get(prop, 0))
    with patch.object(ocr, '_visual_grid_times', return_value=[0]), \
         patch.object(ocr, '_readtext_batch', return_value=[[]]) as inference:
        assert ocr._perform_visual_subtitle_ocr(capture, 360, 640, 1., [], lambda: False) == ([], .85)
    inference.assert_called_once()


@pytest.mark.parametrize('first_step,second_step', [(1000, 3000), (3000, 1000)])
def test_real_vfr_source_masks_caption_on_its_actual_first_frame(tmp_path, first_step, second_step):
    """Decode real VFR video; mock only the OCR model, never the frame clock."""
    import json
    import shutil
    import subprocess
    from backend import ocr_utils as ocr
    from backend.subtitle_blur import collect_source_blur_boxes

    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        pytest.skip('FFmpeg and ffprobe required for CPU VFR regression')
    width, height = 360, 640
    raw = tmp_path / 'frames.gray'
    raw.write_bytes(b''.join(bytes([i * 8]) * (width * height) for i in range(30)))
    video = tmp_path / 'vfr.mp4'
    expression = (f'settb=1/30000,setpts=if(lt(N\\,15)\\,N*{first_step}'
                  f'\\,{15 * first_step}+(N-15)*{second_step})')
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-f', 'rawvideo',
        '-pix_fmt', 'gray', '-s', f'{width}x{height}', '-r', '30', '-i', str(raw),
        '-vf', expression, '-c:v', 'libx264', '-qp', '0', '-threads', '1',
        '-bf', '0', '-fps_mode', 'vfr', str(video)], check=True, capture_output=True, timeout=20)
    probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-of', 'json', str(video)],
                           check=True, capture_output=True, text=True, timeout=10)
    duration = float(json.loads(probe.stdout)['streams'][0]['duration'])
    actual_times = []
    capture = cv2.VideoCapture(str(video))
    try:
        for _ in range(30):
            ok, _ = capture.read()
            assert ok
            actual_times.append(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000)
        assert actual_times[15] == pytest.approx(15 * first_step / 30000)

        def recognize(images):
            result = []
            for frame in images:
                wide = float(np.mean(frame)) >= 116
                left, right = (60, 300) if wide else (120, 240)
                text = '这是比较长的字幕' if wide else '短字幕'
                result.append([([[left, 520], [right, 520], [right, 550], [left, 550]], text, .99)])
            return result

        with patch.object(ocr, '_readtext_batch', side_effect=recognize), \
             patch.object(ocr, 'select_chinese_subtitle_band', return_value=SimpleNamespace(top=.8, bottom=.9, support=5, mode='data')), \
             patch.object(ocr, 'detect_static_watermarks', return_value=[]):
            segments, _ = ocr._perform_visual_subtitle_ocr(capture, width, height, duration, [], lambda: False)
    finally:
        capture.release()
    boxes = collect_source_blur_boxes(segments)
    assert min(b['start'] for b in boxes if b['x_pct'] < .2) == pytest.approx(actual_times[15])
    for index, pts in enumerate(actual_times):
        active = [box for box in boxes if box['start'] <= pts < box['end']]
        assert active, (index, pts)
        assert (min(b['x_pct'] for b in active) < .2) == (index >= 15), (index, pts)


def test_vfr_coarse_sampling_avoids_failed_time_seeks_without_dropping_tail():
    from backend import ocr_utils as ocr
    pts = [0., .1, .8, .9]

    class Capture:
        def __init__(self):
            self.index = 0
            self.failed = False

        def set(self, prop, value):
            if prop == cv2.CAP_PROP_POS_MSEC:
                self.failed = True
            elif prop == cv2.CAP_PROP_POS_FRAMES:
                self.index = int(value)
                self.failed = False
            return True

        def read(self):
            if self.failed or self.index >= len(pts):
                return False, None
            image = np.full((64, 64, 3), self.index, dtype=np.uint8)
            self.index += 1
            return True, image

        def get(self, prop):
            return {cv2.CAP_PROP_FRAME_COUNT: len(pts),
                    cv2.CAP_PROP_POS_FRAMES: self.index,
                    cv2.CAP_PROP_POS_MSEC: pts[max(0, self.index - 1)] * 1000}.get(prop, 0)

    seen = []
    def recognize(images):
        seen.extend(int(image[0, 0, 0]) for image in images)
        return [[] for _ in images]

    metrics = {}
    with patch.object(ocr, '_visual_grid_times', return_value=[.75, .95]), \
         patch.object(ocr, '_readtext_batch', side_effect=recognize):
        result = ocr._perform_visual_subtitle_ocr(Capture(), 64, 64, 1., [], lambda: False, metrics)
    assert result == ([], .85)
    assert seen == [2, 3]
    assert not metrics.get('visual_seek_fallbacks')


def test_vfr_refinement_uses_sequential_frames_when_nonzero_seeks_lie():
    """OpenCV can report the requested ordinal while decoding another frame."""
    from backend import ocr_utils as ocr
    from backend.subtitle_blur import collect_source_blur_boxes
    pts = [0., .1, .2, .3, .4, .5, .6, .7, .8, .9]

    class Capture:
        def __init__(self):
            self.index = 0
            self.seeks = []

        def set(self, prop, value):
            self.seeks.append((prop, value))
            assert prop == cv2.CAP_PROP_POS_FRAMES and value == 0
            self.index = 0
            return True

        def grab(self):
            if self.index >= len(pts):
                return False
            self.index += 1
            return True

        def retrieve(self):
            return True, np.full((64, 64, 3), self.index - 1, dtype=np.uint8)

        def get(self, prop):
            return {cv2.CAP_PROP_FRAME_COUNT: len(pts),
                    cv2.CAP_PROP_POS_FRAMES: self.index,
                    cv2.CAP_PROP_POS_MSEC: pts[max(0, self.index - 1)] * 1000}.get(prop, 0)

    def recognize(images):
        return [[([[8, 50], [56, 50], [56, 58], [8, 58]],
                  '前面的字幕' if int(image[0, 0, 0]) < 5 else '后面的字幕', .99)]
                for image in images]

    capture = Capture()
    with patch.object(ocr, '_visual_grid_times', return_value=[0., .7]), \
         patch.object(ocr, '_readtext_batch', side_effect=recognize), \
         patch.object(ocr, 'select_chinese_subtitle_band', return_value=SimpleNamespace(
             top=.75, bottom=.95, support=5, mode='data')), \
         patch.object(ocr, 'detect_static_watermarks', return_value=[]):
        segments, _ = ocr._perform_visual_subtitle_ocr(capture, 64, 64, 1., [], lambda: False)
    boxes = collect_source_blur_boxes(segments)
    assert capture.seeks and all(value == 0 for _, value in capture.seeks)
    for index, timestamp in enumerate(pts):
        active = [b for b in boxes if b['start'] <= timestamp < b['end']]
        assert active, (index, timestamp)
    assert min(b.start for seg in segments for b in seg.tracking_blocks
               if b.text == '后面的字幕') == .5


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


def test_source_caption_dropout_filled_only_between_matching_rows():
    from backend.ocr_utils import _bridge_source_caption_gaps

    def row(text, start, end, top=.8):
        return SimpleNamespace(text=text, start=start, end=end, x_pct=.2, max_x_pct=.8,
                               y_pct=top, max_y_pct=top + .05)

    before = row('8严丝合缝的丝滑爽感G', 1, 1.1)
    after = row('严丝合缝的丝滑爽感G', 1.33, 1.5)
    elsewhere = row('包装上的字', 1.2, 1.3, .4)
    result = _bridge_source_caption_gaps([before, elsewhere, after])
    assert (result[-1].start, result[-1].end) == (1.1, 1.33)
    assert before.end == 1.1 and after.start == 1.33
    interrupting = row('这里换了一行字幕', 1.2, 1.3)
    assert _bridge_source_caption_gaps([before, interrupting, after]) == [before, interrupting, after]
    late = row(after.text, 1.6, 1.7)
    assert _bridge_source_caption_gaps([before, late]) == [before, late]


@pytest.mark.parametrize('recognized,status', [('', 'PASS'), (None, 'UNKNOWN'),
                                               ('短字幕', 'FAIL'), ('商品型号', 'WARN')])
def test_pixel_qc_checks_short_cues_and_distinguishes_packaging(tmp_path, recognized, status):
    from contextlib import nullcontext
    from backend.v1_quality_gate import check_rendered_pixel_coverage
    import ocr_utils
    video = tmp_path / 'render.mp4'
    video.write_bytes(b'fixture')
    block = SimpleNamespace(text='短字幕', start=.1, end=.2, x_pct=.2, max_x_pct=.8,
                            y_pct=.8, max_y_pct=.9)
    segment = SimpleNamespace(index=1, start=.1, end=.2, ocr_mask_status='located',
                              best_block=block, tracking_blocks=[block])
    capture = _pixel_qc_capture()

    def recognize(images):
        if recognized is None:
            return []
        # Packaging stays outside the verified caption box; high-confidence
        # CJK inside that box is a confirmed leak even with different spelling.
        y0, y1 = (0, 10) if recognized == '商品型号' else (55, 78)
        row = ([[40, y0], [140, y0], [140, y1], [40, y1]], recognized, .99)
        return [[row] if recognized else [] for _ in images]

    with patch('cv2.VideoCapture', return_value=capture), \
         patch('v1_gpu_gatekeeper.gpu_gatekeeper', return_value=nullcontext()), \
         patch.object(ocr_utils, '_readtext_batch', side_effect=recognize), \
         patch.object(ocr_utils, 'release_ocr_reader'):
        result = check_rendered_pixel_coverage(video, [segment],rendered_duration_s=1.)
    assert result.status == status
    if status == 'PASS':
        assert result.details['transition_samples'] > 0
