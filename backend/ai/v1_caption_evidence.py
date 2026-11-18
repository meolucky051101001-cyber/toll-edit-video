"""Collect selected, timed captions as translation evidence without rewriting ASR."""
from __future__ import annotations

import math


def _value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _seconds(value):
    return float(value.total_seconds() if hasattr(value, "total_seconds") else value)


def build_ocr_hint(segment, max_chars=320):
    """Keep changing captions in cue order; never promote packaging or off-cue rows."""
    try:
        start, end = _seconds(segment.start), _seconds(segment.end)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ""
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        return ""
    tracking = _value(segment, "tracking_blocks", []) or []
    rows = tracking or [_value(segment, "best_block")]
    accepted = []
    for row in rows:
        if not row or _value(row, "is_packaging") or _value(row, "is_static"):
            continue
        if _value(row, "is_subtitle") is False or _value(row, "in_subtitle_band") is False:
            continue
        text = str(_value(row, "text", "") or "").strip()
        try:
            confidence = float(_value(row, "prob", 0) or 0)
            sample = _value(row, "sample_time")
            has_verified_interval = bool(
                (_value(row, "visual_locked") or _value(segment, "source_caption_timing_verified"))
                and _value(row, "start") is not None
                and _value(row, "end") is not None
            )
            # A synthesized best_block has sample_time=0; its interval is the evidence.
            if tracking and sample is not None and not has_verified_interval:
                time = _seconds(sample)
                within = start <= time <= end
            else:
                r_start = _seconds(_value(row, "start", start))
                finish = _seconds(_value(row, "end", end))
                within = math.isfinite(r_start) and math.isfinite(finish) and finish > r_start and r_start < end and finish > start
                time = max(start, r_start) if within else r_start
        except (TypeError, ValueError, OverflowError):
            continue
        if text and math.isfinite(confidence) and confidence >= 0.80 and math.isfinite(time) and within:
            accepted.append((time, text))
    parts = []
    for _, text in sorted(accepted, key=lambda item: item[0]):
        if parts and text == parts[-1]:
            continue
        if len(" / ".join(parts + [text])) > max_chars:
            break  # Do not silently cut a number, name, or phrase in half.
        parts.append(text)
    return " / ".join(parts)


def translation_cue_meta(segments, existing=None):
    """Align caller voice/budgets and caption evidence with nonempty source cues."""
    sources = [segment for segment in segments if segment.content]
    valid = isinstance(existing, list) and len(existing) == len(sources)
    result = []
    for position, segment in enumerate(sources):
        meta = dict(existing[position]) if valid and isinstance(existing[position], dict) else {}
        meta.setdefault("id", position + 1)
        if _value(segment, "tracking_blocks") or _value(segment, "best_block"):
            # Replace the caller's best-frame-only hint, including when all rows are rejected.
            meta["ocr_hint"] = build_ocr_hint(segment)
        pf_alt = str(_value(segment, "paraformer_alt", "") or "").strip()
        src_txt = str(_value(segment, "content", "") or "").strip()
        if pf_alt and pf_alt != src_txt:
            meta["asr_alt"] = pf_alt[:200]
        result.append(meta)
    return result
