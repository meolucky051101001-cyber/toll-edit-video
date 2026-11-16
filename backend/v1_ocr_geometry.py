"""Mask-coordinate contracts, independent of ASR confidence and voice settings."""
import math
from collections import Counter
from difflib import SequenceMatcher

OCR_GEOMETRY_VERSION = 7


class OCRGeometryError(RuntimeError):
    """Detected caption cannot safely be covered; retain job for correction."""


def value(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        if "__object_fields__" in obj:
            obj = obj["__object_fields__"]
        return obj.get(key, default)
    return getattr(obj, key, default)


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
    count = max(1, min(8, int(count)))
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


def _text_key(text):
    return ''.join(ch for ch in str(text or '') if ch.isalnum())


def _sample_key(block):
    sid = value(block, 'sample_segment_id')
    return sid if sid is not None else value(block, 'sample_time')


def _repeated_packaging(blocks, text):
    """True when text repeats across scenes, tolerating OCR punctuation and
    character variants (watermark/logo); real caption rows change every cue."""
    key = _text_key(text)
    if len(key) < 2:
        return False
    groups = {}
    for block in blocks:
        other = _text_key(value(block, 'text', ''))
        if len(other) >= 2:
            groups.setdefault(other, set()).add(_sample_key(block))
    for other, samples in groups.items():
        if len(samples) < 3:
            continue
        if key in other or other in key:
            return True
        if (abs(len(key) - len(other)) <= 2
                and SequenceMatcher(None, key, other).ratio() >= .8):
            return True
    return False


def has_caption_evidence(blocks, segment_id, band):
    """Do not confuse static product labels with a missed caption track."""
    for block in blocks:
        if value(block, 'sample_segment_id') != segment_id or not valid_box(block):
            continue
        text = str(value(block, 'text', '')).strip()
        cjk = ''.join(c for c in text if '\u3400' <= c <= '\u9fff')
        latin = sum(1 for c in text if c.isascii() and c.isalpha())
        english_band = str(value(band, 'mode', '') or '') == 'english_match'
        if text.startswith(('@', '＠')) or (
            len(cjk) < 2 and not (english_band and latin >= 4)
        ):
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
        if _repeated_packaging(blocks, text):
            continue
        return True
    return False


def geometry_summary(segments):
    counts = Counter(value(s, 'ocr_mask_status', 'unverified') for s in segments)
    return {'version': OCR_GEOMETRY_VERSION, 'total': len(segments),
            'counts': dict(counts),
            'unresolved_indices': [value(s, 'index') for s in segments
                                   if value(s, 'ocr_mask_status') == 'unresolved']}


def relax_subtitle_free_geometry(segments, blocks, band):
    if not segments:
        return None
    no_caption = [s for s in segments if value(s, 'ocr_mask_status') == 'no_caption_evidence']
    unresolved = [(idx, s) for idx, s in enumerate(segments)
                  if value(s, 'ocr_mask_status') == 'unresolved']
    if not unresolved or len(no_caption) * 5 < len(segments) * 4:
        return None
    segment_ids = [value(s, 'index') if value(s, 'index') is not None else idx
                   for idx, s in unresolved]
    if any(has_caption_evidence(blocks, sid, band) for sid in segment_ids):
        return None
    for (_, segment), sid in zip(unresolved, segment_ids):
        if isinstance(segment, dict):
            segment['best_block'] = None
            segment['tracking_blocks'] = []
            segment['ocr_mask_status'] = 'no_caption_evidence'
        else:
            segment.best_block = None
            segment.tracking_blocks = []
            segment.ocr_mask_status = 'no_caption_evidence'
    return {'applied': True,
            'reason': 'at_least_80_percent_no_caption_evidence_and_no_unresolved_cjk',
            'converted_indices': segment_ids}


def validate_mask_geometry(segments, strict=None):
    if strict is None:
        import os
        strict = os.getenv("V1_STRICT_OCR_GEOMETRY", "1").lower() in ("1", "true")
    # Validation must not manufacture observations from neighbouring cues.
    # Retain unresolved geometry for correction, including in warning mode.
    invalid = [value(s, 'index') for s in segments
               if value(s, 'ocr_mask_status') == 'unresolved'
               or (value(s, 'ocr_mask_status') == 'located'
                   and (not valid_box(value(s, 'best_block'))
                        or any(not valid_box(b) for b in
                               (value(s, 'tracking_blocks', []) or []))))]
    if invalid:
        msg = ('OCR mask coordinates unresolved for cue(s) ' +
               ', '.join(map(str, invalid[:12])) +
               '; OCR verification or correction required')
        if strict:
            raise OCRGeometryError(msg)
        import logging
        logging.getLogger(__name__).warning('[OCR_GEOMETRY] ⚠️ ' + msg)

