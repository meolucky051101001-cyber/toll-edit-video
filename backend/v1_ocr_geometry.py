"""Mask-coordinate contracts, independent of ASR confidence and voice settings."""
import math
from collections import Counter

OCR_GEOMETRY_VERSION = 3


class OCRGeometryError(RuntimeError):
    """Detected caption cannot safely be covered; retain job for correction."""


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def valid_box(block):
    if block is None:
        return False
    try:
        x, right, y, bottom = [float(value(block, key)) for key in
                               ('x_pct', 'max_x_pct', 'y_pct', 'max_y_pct')]
        return (all(math.isfinite(n) for n in (x, right, y, bottom))
                and 0 <= x < right <= 1 and 0 <= y < bottom <= 1)
    except (TypeError, ValueError):
        return False


def sample_times(start, end, count):
    """Sample inside the cue, not the expanded interval of its neighbours."""
    start, end = max(0.0, float(start)), float(end)
    if end <= start:
        return []
    count = max(1, min(4, int(count)))
    return [start + (i + .5) * (end - start) / count for i in range(count)]


def rescue_times(start, end):
    start, end = max(0., float(start)), float(end)
    return [start + (end - start) * p for p in (.18, .82)] if end > start else []


def normalized_rows(rows, scale, crop_offset, width, height):
    """Undo exact resize/crop transform and retain edge glyph padding."""
    result = []
    for bbox, text, probability in rows:
        if not str(text or '').strip() or len(bbox) < 4:
            continue
        try:
            xs = [float(p[0]) / scale for p in bbox]
            ys = [float(p[1]) / scale + crop_offset for p in bbox]
            if not all(math.isfinite(v) for v in xs + ys):
                continue
            block = dict(text=str(text).strip(), prob=float(probability),
                         x_pct=max(0., min(xs) - width * .01) / width,
                         max_x_pct=min(width, max(xs) + width * .01) / width,
                         y_pct=max(0., min(ys) - height * .005) / height,
                         max_y_pct=min(height, max(ys) + height * .005) / height)
            if valid_box(block):
                result.append(block)
        except (ValueError, TypeError, IndexError):
            continue
    return result


def has_caption_evidence(blocks, segment_id, band):
    """Do not confuse static product labels with a missed caption track."""
    for block in blocks:
        if value(block, 'sample_segment_id') != segment_id or not valid_box(block):
            continue
        text = str(value(block, 'text', '')).strip()
        cjk = ''.join(c for c in text if '\u3400' <= c <= '\u9fff')
        if text.startswith(('@', '＠')) or len(cjk) < 2:
            continue
        top, bottom = value(block, 'y_pct'), value(block, 'max_y_pct')
        center = (top + bottom) / 2
        if band.support > 0 and band.mode != 'default':
            if not band.top - .015 <= center <= band.bottom + .015:
                continue
        else:
            # Ambiguous edge caption is NOT equivalent to a subtitle-free video.
            # Do not mask it speculatively; request correction rather than emit
            # an unrelated Vietnamese card above a still-visible Chinese row.
            width = value(block, 'max_x_pct') - value(block, 'x_pct')
            if (not (center < .25 or center > .72) or width < .25
                    or bottom-top < .012 or float(value(block, 'prob', 0)) < .4):
                continue
        related = {value(b, 'sample_segment_id') for b in blocks
                   if str(value(b, 'text', '')).strip() == text
                   and abs(float(value(b, 'y_pct', 0)) - top) < .015}
        if len(related) >= 3:
            continue
        return True
    return False


def geometry_summary(segments):
    counts = Counter(value(s, 'ocr_mask_status', 'unverified') for s in segments)
    return {'version': OCR_GEOMETRY_VERSION, 'total': len(segments),
            'counts': dict(counts),
            'unresolved_indices': [value(s, 'index') for s in segments
                                   if value(s, 'ocr_mask_status') == 'unresolved']}


def validate_mask_geometry(segments, strict=None):
    if strict is None:
        import os
        strict = os.getenv("V1_STRICT_OCR_GEOMETRY", "0").lower() in ("1", "true")
    invalid = [value(s, 'index') for s in segments
               if value(s, 'ocr_mask_status') == 'unresolved'
               or (value(s, 'ocr_mask_status') == 'located'
                   and not valid_box(value(s, 'best_block')))]
    if invalid:
        msg = ('OCR mask coordinates unresolved for cue(s) ' +
               ', '.join(map(str, invalid[:12])) +
               '; auto-healed with safe fallback')
        if strict:
            raise OCRGeometryError(msg)
        import logging
        logging.getLogger(__name__).warning('[OCR_GEOMETRY] ⚠️ ' + msg)
        for s in segments:
            st = value(s, 'ocr_mask_status')
            if st == 'unresolved' or (st == 'located' and not valid_box(value(s, 'best_block'))):
                if hasattr(s, 'ocr_mask_status'):
                    s.ocr_mask_status = 'no_caption_evidence'
                elif isinstance(s, dict):
                    s['ocr_mask_status'] = 'no_caption_evidence'

