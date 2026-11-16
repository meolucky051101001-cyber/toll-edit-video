"""Cloud subtitle timing: speech pages and source-caption masks have separate clocks.

The V1 renderer supplies layout only. Its TTS-based end-time rewrite must not
shorten the source timeline or the lifetime of a source-caption cover.
"""
from __future__ import annotations

import copy
from datetime import timedelta
import math
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace


class SubtitleTimingError(ValueError):
    """Rendered subtitle text or selected source captions are not fully covered."""


def _value(block, key, default=None):
    if isinstance(block, dict):
        return block.get("__object_fields__", block).get(key, default)
    return getattr(block, key, default)


def _source_selected(block):
    return block is not None and not (
        any(_value(block, flag, False) is True for flag in ("is_packaging", "is_static"))
        or any(_value(block, flag) is False for flag in ("is_subtitle", "in_subtitle_band"))
        or str(_value(block, "type", "")).lower() in ("watermark", "logo", "packaging", "scene_text")
    )


def _seconds(value):
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _timestamp(seconds):
    # The ASS format has centisecond resolution. Round rather than drift early.
    centis = round(seconds * 100)
    h, remaining = divmod(centis, 360000)
    m, remaining = divmod(remaining, 6000)
    s, fraction = divmod(remaining, 100)
    return f"{h}:{m:02}:{s:02}.{fraction:02}"


def _normalized_text(text):
    return "".join(c.casefold() for c in str(text) if c.isalnum())


def _interval_gaps(start, end, intervals, tolerance=0.021):
    cursor = start
    gaps = []
    for left, right in sorted(intervals):
        if right <= cursor or left >= end:
            continue
        if left > cursor + tolerance:
            gaps.append([round(cursor, 4), round(min(left, end), 4)])
        cursor = max(cursor, min(end, right))
    if cursor < end - tolerance:
        gaps.append([round(cursor, 4), round(end, 4)])
    return gaps


def _events(ass_text):
    events = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        row = line.split(",", 9)
        if len(row) != 10:
            raise SubtitleTimingError("Malformed ASS dialogue event")
        start, end = _seconds(row[1]), _seconds(row[2])
        if not math.isfinite(start + end) or start < 0 or end <= start:
            raise SubtitleTimingError("Invalid ASS dialogue interval")
        events.append({"start": start, "end": end, "style": row[3], "name": row[4],
                       "raw_text": row[9],
                       "text": re.sub(r"\{[^}]*\}", "", row[9]).replace(r"\N", " ")})
    return events


def _mask_box(raw):
    pos = re.search(r"\\pos\(([-.\d]+),([-.\d]+)\)", raw)
    shape = re.search(r"\{\\p1\}([^{}]+)\{\\p0\}", raw)
    if not pos or not shape:
        return None
    numbers = [float(s) for s in re.findall(r"-?\d+(?:\.\d+)?", shape.group(1))]
    if not numbers or len(numbers) % 2:
        return None
    x, y = map(float, pos.groups())
    return [x + min(numbers[::2]), y + min(numbers[1::2]),
            x + max(numbers[::2]), y + max(numbers[1::2])]


def _source_masks(segments, width, height, video_duration):
    masks = []
    seen = set()
    for segment in segments:
        if not _source_selected(segment):
            continue
        blocks = segment.get("tracking_blocks") or [segment.get("best_block")]
        for block in blocks:
            if not _source_selected(block):
                continue
            try:
                x1, y1, x2, y2 = [float(_value(block, key)) for key in
                                  ("x_pct", "y_pct", "max_x_pct", "max_y_pct")]
                start = max(0.0, float(_value(block, "start", segment["start"])))
                end = min(video_duration, float(_value(block, "end", segment["end"])))
            except (TypeError, ValueError) as exc:
                raise SubtitleTimingError(f"Invalid source caption geometry for cue {segment['id']}") from exc
            if not all(math.isfinite(v) for v in (x1, y1, x2, y2, start, end)):
                raise SubtitleTimingError("Non-finite source caption geometry")
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1 and start < end):
                raise SubtitleTimingError(f"Invalid source caption bounds for cue {segment['id']}")
            box = [max(0, math.floor(x1 * width) - max(2, round(width * .005))),
                   max(0, math.floor(y1 * height) - max(2, round(height * .003))),
                   min(width, math.ceil(x2 * width) + max(2, round(width * .005))),
                   min(height, math.ceil(y2 * height) + max(2, round(height * .003)))]
            key = (round(start, 4), round(end, 4), *box)
            if key in seen:
                continue
            seen.add(key)
            color = str(_value(block, "bg_color_ass", "&H00FFFFFF"))
            if not re.fullmatch(r"&H00[0-9A-Fa-f]{6}", color):
                color = "&H00FFFFFF"  # A translucent rectangle cannot cover source text.
            masks.append({"name": f"cloud_mask_{len(masks) + 1}", "cue_id": str(segment["id"]),
                          "start": start, "end": end, "box": box, "color": color})
    return masks


def validate_cloud_ass(ass_path, expected_cues, expected_masks, *, video_duration):
    """Verify actual emitted events, not only input cue counts or OCR hit counts."""
    events = _events(Path(ass_path).read_text(encoding="utf-8-sig"))
    errors = []
    cue_audit = []
    expected_names = {cue["name"] for cue in expected_cues}
    for event in events:
        if event["end"] > video_duration + .021:
            errors.append(f"ASS event exceeds video: {event['name']}")
        if event["style"] == "TextStyle" and event["name"] not in expected_names:
            errors.append(f"Unexpected ASS text event: {event['name']}")
    for cue in expected_cues:
        actual = [e for e in events if e["name"] == cue["name"] and e["style"] == "TextStyle"]
        actual.sort(key=lambda e: (e["start"], e["end"]))
        gaps = _interval_gaps(cue["start"], cue["end"], [(e["start"], e["end"]) for e in actual])
        pages = []
        for event in actual:
            text = _normalized_text(event["text"])
            # Layout may emit adjacent copies of one page when a box moves.
            if pages and pages[-1][0] == text and abs(event["start"] - pages[-1][2]) <= .021:
                pages[-1][2] = event["end"]
            else:
                pages.append([text, event["start"], event["end"]])
            if event["start"] < cue["start"] - .021 or event["end"] > cue["end"] + .021:
                errors.append(f"Text event outside its cue window: {cue['id']}")
        text_match = "".join(p[0] for p in pages) == _normalized_text(cue["text"])
        if gaps or not text_match:
            errors.append(f"Missing/reordered subtitle text or timing gap: cue {cue['id']}")
        cue_audit.append({"id": cue["id"], "source_start": cue["source_start"],
                          "source_end": cue["source_end"], "start": cue["start"], "end": cue["end"],
                          "tts_duration": cue.get("tts_duration"), "text_matches": text_match,
                          "uncovered_intervals": gaps, "event_count": len(actual)})
    mask_audit = []
    for mask in expected_masks:
        intervals = []
        for event in events:
            if event["style"] != "CloudSourceMask" or event["name"] != mask["name"]:
                continue
            box = _mask_box(event["raw_text"])
            expected = mask["box"]
            if box and box[0] <= expected[0] and box[1] <= expected[1] and box[2] >= expected[2] and box[3] >= expected[3]:
                intervals.append((event["start"], event["end"]))
        gaps = _interval_gaps(mask["start"], mask["end"], intervals)
        if gaps:
            errors.append(f"Source caption is not fully masked: {mask['name']}")
        mask_audit.append({**mask, "uncovered_intervals": gaps})
    audit = {"policy": "source_timeline_with_independent_caption_masks_v1", "passed": not errors,
             "cue_count": len(expected_cues), "source_mask_count": len(expected_masks),
             "cues": cue_audit, "source_masks": mask_audit, "errors": errors}
    if errors:
        error = SubtitleTimingError("; ".join(errors))
        error.audit = audit
        raise error
    return audit


def generate_cloud_ass(segments, ass_path, *, video_w=1080, video_h=1920,
                       main_y_pct=.78, video_duration):
    """Use V1 layout per source cue; return verifiable text/mask timing audit.

    Source timing and all input geometry are immutable. A longer TTS can extend
    the text within the next-cue/video bound, but a shorter TTS never removes
    source time. Masks retain their own original OCR lifetimes, even in gaps.
    """
    import srt
    try:
        from ass_utils import generate_ass_file
    except ImportError:
        from backend.ass_utils import generate_ass_file

    duration = float(video_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise SubtitleTimingError("Positive measured source duration is required")
    copied = copy.deepcopy(list(segments))
    if not copied:
        raise SubtitleTimingError("No subtitles to render")
    scale = 720.0 / min(video_w, video_h)
    width, height = round(video_w * scale), round(video_h * scale)
    masks = _source_masks(copied, width, height, duration)
    expected_cues = []
    ids = set()
    page_lines = []
    header = None
    with tempfile.TemporaryDirectory(prefix="cloud-ass-", dir=str(Path(ass_path).parent)) as tmp:
        tmp_path = Path(tmp) / "cue.ass"
        for index, source in enumerate(copied):
            cid = str(source["id"])
            if cid in ids:
                raise SubtitleTimingError(f"Duplicate cue ID: {cid}")
            ids.add(cid)
            start, source_end = float(source["start"]), float(source["end"])
            next_start = float(copied[index + 1]["start"]) if index + 1 < len(copied) else duration
            if not (math.isfinite(start + source_end + next_start) and 0 <= start < source_end <= duration + .001):
                raise SubtitleTimingError(f"Invalid source timeline: cue {cid}")
            if source_end > next_start + .021:
                raise SubtitleTimingError(f"Overlapping source cues: {cid}")
            text = str(source.get("translated_text", "") or "").strip()
            if not _normalized_text(text):
                raise SubtitleTimingError(f"Missing translation: cue {cid}")
            tts_duration = float(source.get("tts_duration", 0.0) or 0.0)
            if not math.isfinite(tts_duration) or tts_duration < 0:
                raise SubtitleTimingError(f"Invalid TTS duration: cue {cid}")
            end = min(duration, next_start, max(source_end, start + tts_duration))
            cue = {"id": cid, "name": f"cloud_cue_{cid}", "source_start": start,
                   "source_end": source_end, "start": start, "end": end,
                   "text": text, "tts_duration": tts_duration}
            expected_cues.append(cue)
            segment = srt.Subtitle(index=index + 1, start=timedelta(seconds=start),
                                   end=timedelta(seconds=end), content=text)
            segment.tracking_blocks = []
            selected = _source_selected(source)
            blocks = source.get("tracking_blocks") or [source.get("best_block")]
            for block in blocks:
                if not selected or not _source_selected(block):
                    continue
                data = dict(block.get("__object_fields__", block)) if isinstance(block, dict) else vars(block).copy()
                left, right = max(start, float(data.get("start", start))), min(end, float(data.get("end", end)))
                if right <= left:
                    continue
                data.update(start=left, end=right, is_subtitle=True, in_subtitle_band=True,
                            is_packaging=False, type="subtitle")
                segment.tracking_blocks.append(SimpleNamespace(**data))
            segment.best_block = segment.tracking_blocks[0] if segment.tracking_blocks else None
            segment.is_subtitle = True
            segment.in_subtitle_band = bool(segment.best_block)
            segment.is_packaging = False
            segment.ocr_mask_status = (
                source.get("ocr_mask_status", "located")
                if segment.best_block else "no_caption_evidence"
            )
            segment.y_pct = source.get("y_pct", main_y_pct)
            segment.max_y_pct = source.get("max_y_pct", min(.95, main_y_pct + .06))
            generate_ass_file([segment], [], tmp_path, play_res_x=video_w, play_res_y=video_h,
                              main_y_pct=main_y_pct, video_duration=end, cover_style="solid_box",
                              strip_terminal_punctuation=True)
            generated = tmp_path.read_text(encoding="utf-8-sig")
            if header is None:
                header = generated.split("Dialogue:", 1)[0]
            for line in generated.splitlines():
                if not line.startswith("Dialogue:"):
                    continue
                row = line.split(",", 9)
                left, right = max(start, _seconds(row[1])), min(end, _seconds(row[2]))
                if right - left < .005:
                    continue
                row[1], row[2], row[4] = _timestamp(left), _timestamp(right), cue["name"]
                page_lines.append(",".join(row))
    if header is None:
        raise SubtitleTimingError("ASS renderer produced no header")
    mask_style = ("Style: CloudSourceMask,Arial,1,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,"
                  "0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n\n")
    header = header.replace("[Events]", mask_style + "[Events]", 1)
    mask_lines = []
    for mask in masks:
        x1, y1, x2, y2 = mask["box"]
        w, h = x2 - x1, y2 - y1
        drawing = (f"{{\\an7\\pos({x1},{y1})\\1c{mask['color']}\\alpha&H00&}}"
                   f"{{\\p1}}m 0 0 l {w} 0 {w} {h} 0 {h}{{\\p0}}")
        mask_lines.append(f"Dialogue: 0,{_timestamp(mask['start'])},{_timestamp(mask['end'])},"
                          f"CloudSourceMask,{mask['name']},0,0,0,,{drawing}")
    Path(ass_path).write_text(header + "\n".join(mask_lines + page_lines) + "\n", encoding="utf-8-sig")
    return validate_cloud_ass(ass_path, expected_cues, masks, video_duration=duration)
