"""Small real CPU renders: verify pixels and timing, without models or GPU."""
import shutil
import subprocess

import numpy as np
import pytest

from backend.subtitle_blur import blur_regions, build_localized_blur_filter


@pytest.mark.parametrize('width,height,tiny', [(320, 180, False), (180, 320, False), (64, 64, True)])
def test_real_filter_changes_only_active_source_rectangles(tmp_path, width, height, tiny):
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg required for CPU pixel integration check')
    # A monochrome checkerboard exposes both accidental blur and missed blur.
    yy, xx = np.indices((height, width))
    luma = np.where((xx // 3 + yy // 3) % 2, 220, 30).astype(np.uint8)
    frame = luma.tobytes() + bytes([128]) * (width * height // 2)
    source = tmp_path / 'source.yuv'
    source.write_bytes(frame * 20)
    boxes = [dict(start=start, end=end, x_pct=x, max_x_pct=x+.3,
                  y_pct=y, max_y_pct=y+.12)
             for start, end, x, y in [(.4, .8, .1, .7), (1.2, 1.6, .1, .7),
                                      (2., 2.4, .6, .2), (2.8, 3.2, .6, .2)]]
    settings = None
    if tiny:
        boxes = [dict(start=.4, end=.8, x_pct=0, max_x_pct=2/width,
                      y_pct=0, max_y_pct=2/height)]
        settings = dict(sub_blur_padding_x=0, sub_blur_padding_y=0)
    graph = build_localized_blur_filter(boxes, width, height, settings)
    script = tmp_path / 'blur.filters.txt'
    script.write_text(graph, encoding='utf-8')
    output = tmp_path / 'blur.yuv'
    command = [ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '1',
               '-filter_threads', '1', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
               '-s', f'{width}x{height}', '-r', '5', '-i', str(source),
               '-filter_script:v', str(script), '-frames:v', '20',
               '-c:v', 'rawvideo', '-threads', '1', '-pix_fmt', 'yuv420p',
               '-f', 'rawvideo', str(output)]
    result = subprocess.run(command, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    frames = np.frombuffer(output.read_bytes(), dtype=np.uint8).reshape(20, -1)
    assert frames.shape[1] == len(frame)
    for index, raw in enumerate(frames):
        rendered = raw[:width*height].reshape(height, width)
        active = np.zeros((height, width), dtype=bool)
        for x, y, w, h, times in blur_regions(boxes, width, height, settings):
            if any(start <= index/5 < end for start, end in times):
                active[y:y+h, x:x+w] = True
        assert np.array_equal(rendered[~active], luma[~active]), index
        if active.any() and not tiny:
            assert np.mean(np.abs(rendered[active].astype(float) - luma[active])) > 40, index
        assert np.all(raw[width*height:] == 128), 'chroma must stay neutral'


def test_many_moving_regions_render_with_bounded_filter_setup(tmp_path):
    """A real clip yielded 29 rectangles; cascaded splits stalled even frame one."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        pytest.skip('FFmpeg required for CPU pixel integration check')
    width, height = 320, 180
    yy, xx = np.indices((height, width))
    luma = np.where((xx // 2 + yy // 2) % 2, 220, 30).astype(np.uint8)
    frame = luma.tobytes() + bytes([128]) * (width * height // 2)
    source = tmp_path / 'source.yuv'
    source.write_bytes(frame * 30)
    boxes = [dict(start=i / 10, end=(i + 1) / 10,
                  x_pct=(i % 15) * .06, max_x_pct=(i % 15) * .06 + .04,
                  y_pct=.1 + (i // 15) * .5, max_y_pct=.25 + (i // 15) * .5)
             for i in range(30)]
    script = tmp_path / 'moving.filters.txt'
    script.write_text(build_localized_blur_filter(boxes, width, height), encoding='utf-8')
    output = tmp_path / 'moving.yuv'
    result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '1',
        '-filter_threads', '1', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
        '-s', f'{width}x{height}', '-r', '10', '-i', str(source),
        '-filter_script:v', str(script), '-frames:v', '30', '-c:v', 'rawvideo',
        '-threads', '1', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', str(output)],
        capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    frames = np.frombuffer(output.read_bytes(), dtype=np.uint8).reshape(30, -1)
    for i, raw in enumerate(frames):
        rendered = raw[:width * height].reshape(height, width)
        active = np.zeros((height, width), dtype=bool)
        for x, y, w, h, times in blur_regions(boxes, width, height):
            if any(start <= i / 10 < end for start, end in times):
                active[y:y+h, x:x+w] = True
        assert np.array_equal(rendered[~active], luma[~active]), i
        assert np.mean(np.abs(rendered[active].astype(float) - luma[active])) > 40, i
