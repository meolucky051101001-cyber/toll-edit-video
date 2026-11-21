"""Small real CPU renders: verify pixels and timing, without models or GPU."""
import shutil
import subprocess

import numpy as np
import pytest

from backend.subtitle_blur import blur_regions, build_localized_blur_filter


def _legacy_graph(boxes, width, height, settings):
    """Frozen v5 execution graph, an independent pixel oracle for v6 slots."""
    import math
    regions = blur_regions(boxes, width, height, settings)
    radius = settings.get('sub_blur_radius', 16)
    parts = [f"format=yuv420p,split={len(regions) + 1}[oldmain0]" +
             ''.join(f'[old{i}crop]' for i in range(len(regions))) + ';']
    for i, (x, y, w, h, intervals) in enumerate(regions):
        luma = min(radius, (min(w, h) - 1) // 2)
        chroma = min(luma, max(0, (min(w, h) // 2 - 1) // 2))
        stamp = lambda t: f'{math.floor(t * 1_000_000) / 1_000_000:.6f}'
        enable = '+'.join(f'gte(t,{stamp(a)})*lt(t,{stamp(b)})' for a, b in intervals)
        output = f'[oldmain{i + 1}];' if i + 1 < len(regions) else ''
        parts.append(f'[old{i}crop]crop={w}:{h}:{x}:{y},'
                     f"boxblur=luma_radius={luma}:luma_power=3:chroma_radius={chroma}:chroma_power=3:enable='{enable}'[old{i}blur];"
                     f"[oldmain{i}][old{i}blur]overlay={x}:{y}:enable='{enable}'{output}")
    return ''.join(parts)


def _render_lossless(tmp_path, graph, frames, width, height, fps, name, pts=None):
    source = tmp_path / f'{name}.in.yuv'
    source.write_bytes(frames)
    script = tmp_path / f'{name}.filters.txt'
    script.write_text((f'settb=1/1000,setpts={pts},' if pts else '') + graph, encoding='utf-8')
    output = tmp_path / f'{name}.out.yuv'
    result = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-nostdin', '-y',
        '-threads', '1', '-filter_threads', '1', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
        '-s', f'{width}x{height}', '-r', str(fps), '-i', str(source),
        '-/filter:v', str(script), '-c:v', 'rawvideo', '-threads', '1',
        '-fps_mode', 'passthrough', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', str(output)],
        capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return output.read_bytes()


@pytest.mark.parametrize('fps', [30, 60])
@pytest.mark.parametrize('case', ['move', 'resize', 'gap', 'tiny_edges', 'overlap', 'vfr'])
def test_compact_slots_match_legacy_yuv_at_every_frame(tmp_path, fps, case):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg required for pixel integration check')
    width, height, count = 96, 64, 24
    yy, xx = np.indices((height, width))
    chunks = []
    for frame in range(count):
        y = ((xx * 23 + yy * 17 + frame * 11) % 220 + 16).astype(np.uint8)
        u = ((np.arange(width * height // 4) * 11 + frame * 7) % 224 + 16).astype(np.uint8)
        v = ((np.arange(width * height // 4) * 13 + frame * 3) % 224 + 16).astype(np.uint8)
        chunks.append(y.tobytes() + u.tobytes() + v.tobytes())
    def box(a, b, x, y, w=24, h=16):
        return dict(start=a/fps, end=b/fps, x_pct=x/width, max_x_pct=(x+w)/width,
                    y_pct=y/height, max_y_pct=(y+h)/height)
    boxes = [box(2, 8, 8, 8), box(9, 15, 40, 32), box(16, 22, 64, 8)]
    if case == 'resize':
        boxes = [box(2, 8, 8, 8, 32, 24), box(9, 15, 40, 32, 16, 16), box(16, 22, 64, 8)]
    elif case == 'gap':
        boxes = [box(2, 8, 8, 8), box(9, 16, 40, 32), box(17, 22, 8, 8)]
    elif case == 'tiny_edges':
        boxes = [box(0, 8, 0, 0, 2, 2), box(9, 16, 94, 62, 2, 2), box(17, 24, 0, 62, 2, 2)]
    elif case == 'overlap':
        # Region 3 cannot move before region 2: their padded pixels overlap.
        boxes = [box(0, 8, 8, 8), box(8, 24, 30, 24, 32, 24), box(10, 20, 40, 32)]
    settings = dict(sub_blur_padding_x=0, sub_blur_padding_y=0, sub_blur_radius=3)
    graph = build_localized_blur_filter(boxes, width, height, settings)
    legacy = _legacy_graph(boxes, width, height, settings)
    pts = r'if(lt(N\,12)\,N*17\,204+(N-12)*41)' if case == 'vfr' else None
    original = _render_lossless(tmp_path, legacy, b''.join(chunks), width, height, fps, 'legacy', pts)
    optimized = _render_lossless(tmp_path, graph, b''.join(chunks), width, height, fps, 'slots', pts)
    assert len(optimized) == len(original) == width * height * 3 // 2 * count
    assert optimized == original
    if case in {'move', 'gap', 'tiny_edges', 'vfr'}:
        assert 'split=2[' in graph


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
               '-/filter:v', str(script), '-frames:v', '20',
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


@pytest.mark.parametrize('fps', [10, 30])
def test_many_moving_regions_render_with_bounded_filter_setup(tmp_path, fps):
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
    boxes = [dict(start=i / fps, end=(i + 1) / fps,
                  x_pct=(i % 15) * .06, max_x_pct=(i % 15) * .06 + .04,
                  y_pct=.1 + (i // 15) * .5, max_y_pct=.25 + (i // 15) * .5)
             for i in range(30)]
    script = tmp_path / 'moving.filters.txt'
    script.write_text(build_localized_blur_filter(boxes, width, height), encoding='utf-8')
    output = tmp_path / 'moving.yuv'
    result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '1',
        '-filter_threads', '1', '-f', 'rawvideo', '-pix_fmt', 'yuv420p',
        '-s', f'{width}x{height}', '-r', str(fps), '-i', str(source),
        '-/filter:v', str(script), '-frames:v', '30', '-c:v', 'rawvideo',
        '-threads', '1', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', str(output)],
        capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    frames = np.frombuffer(output.read_bytes(), dtype=np.uint8).reshape(30, -1)
    for i, raw in enumerate(frames):
        rendered = raw[:width * height].reshape(height, width)
        active = np.zeros((height, width), dtype=bool)
        for x, y, w, h, times in blur_regions(boxes, width, height):
            if any(start <= i / fps < end for start, end in times):
                active[y:y+h, x:x+w] = True
        assert np.array_equal(rendered[~active], luma[~active]), i
        assert np.mean(np.abs(rendered[active].astype(float) - luma[active])) > 40, i
