"""CPU-only synthetic OCR/ASS acceptance evidence; no models or network."""
from datetime import timedelta
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace as NS

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from backend.ass_utils import generate_ass_file
from backend.pipeline_v2.cover_qc import inspect_covers, parse_ass_covers
from backend.v1_ocr_geometry import OCR_GEOMETRY_VERSION


def _run(args, directory, **kwargs):
    return subprocess.run(args, cwd=directory, check=True, capture_output=True,
                          timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **kwargs)


def create_evidence(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('ffmpeg is required for rendered pixel evidence')
    width, height, fps = 360, 640, 25
    tracks = [NS(start=.4, end=1., x_pct=.1, max_x_pct=.9, y_pct=.72, max_y_pct=.80,
                 geometry_verified=True),
              NS(start=1.8, end=2.2, x_pct=.45, max_x_pct=.95, y_pct=.58, max_y_pct=.69,
                 geometry_verified=True)]
    item = NS(index=1, start=timedelta(), end=timedelta(seconds=3),
              content='Phụ đề tiếng Việt\nTTS dài hơn chữ nguồn — Unicode',
              geometry_verified=True, ocr_mask_status='located',
              best_block=tracks[0], tracking_blocks=tracks)
    ass_path = directory / 'full.ass'
    generate_ass_file([item], [], ass_path, width, height, video_duration=3)
    ass = ass_path.read_text(encoding='utf-8-sig')
    # Render both the full product and source-only layers. Pixel tests must not
    # confuse a configured translation background with an OCR source mask.
    source_only = '\n'.join(line for line in ass.splitlines()
                            if not line.startswith('Dialogue:') or ',SourceMaskStyle,' in line)
    (directory / 'source_masks.ass').write_text(source_only, encoding='utf-8-sig')
    item.tracking_blocks = []
    generate_ass_file([item], [], directory / 'empty.ass', width, height, video_duration=3)
    empty = (directory / 'empty.ass').read_text(encoding='utf-8-sig')
    assert parse_ass_covers(empty)[0] == []
    item.tracking_blocks = tracks

    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    font = ImageFont.truetype(str(font_path), 16) if font_path.is_file() else ImageFont.load_default()
    frames = []
    for number in range(3 * fps):
        time = number / fps
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        frame[:] = (70, 45, 25)
        # Static packaging/watermark remains visible at all timestamps.
        image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(image)
        draw.text((15, 35), '产品包装说明 / @watermark', fill=(200, 180, 80), font=font)
        for track in tracks:
            if track.start <= time < track.end:
                x, y = round(track.x_pct * width) + 4, round(track.y_pct * height) + 3
                text = '今天学习做饭' if track.start < 1 else '第二行字幕\n文字移动了'
                draw.multiline_text((x, y), text, fill=(245, 25, 20), font=font, spacing=1)
        frames.append(cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR))
    _run([ffmpeg, '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
          '-s', f'{width}x{height}', '-r', str(fps), '-i', 'pipe:0', '-an',
          '-c:v', 'ffv1', '-threads', '1', 'source.mkv'], directory,
         input=b''.join(frame.tobytes() for frame in frames))
    for ass_name, output in [('source_masks.ass', 'masked.mkv'), ('full.ass', 'rendered.mkv')]:
        _run([ffmpeg, '-y', '-v', 'error', '-threads', '1', '-filter_threads', '1',
              '-i', 'source.mkv', '-vf', f'ass={ass_name}', '-an', '-c:v', 'ffv1',
              '-threads', '1', output], directory)

    masked = cv2.VideoCapture(str(directory / 'masked.mkv'))
    rendered = cv2.VideoCapture(str(directory / 'rendered.mkv'))
    checks = []
    sample_frames = {0, 9, 10, 24, 25, 35, 44, 45, 54, 55, 65, 74}
    try:
        for number, original in enumerate(frames):
            ok_mask, after = masked.read()
            ok_full, full = rendered.read()
            if not ok_mask or not ok_full:
                raise AssertionError(f'Rendered fixture missing frame {number}')
            if number not in sample_frames:
                continue
            time = number / fps
            active = [t for t in tracks if t.start <= time < t.end]
            delta = cv2.absdiff(original, after)
            changed = int(np.count_nonzero(np.any(delta, axis=2)))
            label_unchanged = bool(np.array_equal(original[:100], after[:100]))
            if active:
                # libass/FFmpeg may express opaque white as limited-range 235.
                # Every pixel inside the observed box must be the same opaque
                # fill, so source glyphs cannot survive under a translucent mask.
                regions = [after[round(t.y_pct*height):round(t.max_y_pct*height),
                                 round(t.x_pct*width):round(t.max_x_pct*width)] for t in active]
                valid = all(int(region.min()) >= 220 and int(region.max()) - int(region.min()) <= 1
                            for region in regions)
                passed = bool(valid and changed > 0 and label_unchanged)
            else:
                passed = changed == 0 and label_unchanged
            # Translation remains visible even when no source mask is active.
            full_changes = int(np.count_nonzero(np.any(cv2.absdiff(original, full), axis=2)))
            passed = passed and full_changes > 0
            checks.append(dict(time=time, source_present=bool(active), changed_mask_pixels=changed,
                               changed_product_pixels=full_changes, packaging_unchanged=label_unchanged,
                               passed=passed))
            stem = f'frame_{number:03d}'
            for suffix, data in [('before', original), ('source_mask', after),
                                 ('product', full), ('pixel_diff', delta)]:
                cv2.imwrite(str(directory / f'{stem}_{suffix}.png'), data)
    finally:
        masked.release()
        rendered.release()
    columns = [('before', 'Source'), ('source_mask', 'Source mask only'),
               ('product', 'Full translation'), ('pixel_diff', 'Mask pixel difference')]
    sheet = Image.new('RGB', (width * 2, height * 2 + 120), 'white')
    labels = ImageDraw.Draw(sheet)
    for row, number in enumerate((10, 35, 45, 65)):
        y = row * (height // 2 + 30)
        for col, (suffix, title) in enumerate(columns):
            x = col * width // 2
            labels.text((x + 3, y + 3), f'{number/fps:.2f}s {title}', fill='black')
            with Image.open(directory / f'frame_{number:03d}_{suffix}.png') as image:
                sheet.paste(image.resize((width // 2, height // 2)), (x, y + 25))
    sheet.save(directory / 'comparison.png')
    covers, _, _ = parse_ass_covers(ass)
    report = dict(geometry_version=OCR_GEOMETRY_VERSION, synthetic=True,
                  models_used=False, network_used=False, fps=fps, source_intervals=[[.4, 1.], [1.8, 2.2]],
                  source_masks=covers, checks=checks,
                  cover_qc=inspect_covers([item], ass, video_duration=3),
                  verified_empty_source_masks=parse_ass_covers(empty)[0])
    (directory / 'audit.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
    return report
