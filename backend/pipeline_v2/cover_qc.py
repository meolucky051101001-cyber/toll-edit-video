from dataclasses import dataclass
import re
from typing import Any, Dict, List, Optional, Sequence, Union


@dataclass
class ExpectedCoverEvent:
    segment_id: Any
    src_start: float
    src_end: float
    expected_start: float
    expected_end: float
    hold_seconds: float
    bridged_to_next: bool
    position_changed: bool
    x_pct: float
    y_pct: float
    max_x_pct: float
    max_y_pct: float
    text: str = ""


def _prop(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_expected_cover_timeline(
    segments: Sequence[Any],
    video_duration: Optional[float] = None,
    fps: float = 30.0,
) -> List[ExpectedCoverEvent]:
    """Build the expected visual cover timeline from OCR / subtitle tracking data.

    Rules enforced:
    1. Visual driven: Uses exact OCR start/end times rather than constraining to ASR boundaries.
    2. Subtitle classification: Does NOT blindly filter by y_pct >= 0.45. Subtitles in the upper
       screen (e.g. is_subtitle=True) are kept; packaging / background text in the lower screen
       (e.g. is_subtitle=False or is_packaging=True) is excluded. Never falls back to discarded blocks.
    3. No fake covers: Segments lacking OCR geometry emit no expected cover event (unverified).
    4. Block gap splitting: Multiple tracking blocks in a segment separated by > 1.0s gap are split
       into separate visual events.
    5. Hold 1.0s & continuous bridging: Covers hold 1.0s after text disappears, or bridge continuously
       to the next event if it begins within 1.0s.
    6. Video end clamping: Events are clamped to video_duration if provided.
    """
    frame_dur = 1.0 / max(1.0, float(fps))
    raw_events = []

    for seg in (segments or []):
        is_sub = _prop(seg, "is_subtitle", None)
        is_pack = _prop(seg, "is_packaging", False) or _prop(seg, "is_static", False)
        if is_sub is False or is_pack is True:
            continue

        blocks = _prop(seg, "tracking_blocks") or ([_prop(seg, "best_block")] if _prop(seg, "best_block") else [])
        if not blocks:
            # Segment has no OCR geometry -> Do not fabricate a default cover!
            continue

        valid_blocks = []
        for b in blocks:
            b_is_sub = _prop(b, "is_subtitle", None)
            b_is_pack = _prop(b, "is_packaging", False) or _prop(b, "is_static", False)
            if b_is_sub is False or b_is_pack is True:
                continue
            if b_is_sub is True:
                valid_blocks.append(b)
                continue

            in_band = _prop(b, "in_subtitle_band", None)
            if in_band is None:
                in_band = _prop(seg, "in_subtitle_band", None)
            if in_band is False:
                continue
            if in_band is True:
                valid_blocks.append(b)
                continue

            b_type = str(_prop(b, "type", "")).lower()
            if b_type in ("packaging", "background", "logo", "watermark"):
                continue

            prob = _prop(b, "prob", 1.0)
            if prob is not None and float(prob) < 0.3:
                continue

            valid_blocks.append(b)

        if not valid_blocks:
            # All blocks were filtered as background/packaging.
            # Do NOT fall back to original blocks!
            continue

        sorted_blocks = sorted(valid_blocks, key=lambda x: float(_prop(x, "start", 0.0)))

        # Split into clusters if gap between adjacent blocks > 1.0s
        clusters: List[List[Any]] = []
        current_cluster: List[Any] = []
        for b in sorted_blocks:
            b_start = float(_prop(b, "start", 0.0))
            if not current_cluster:
                current_cluster.append(b)
            else:
                prev_end = float(_prop(current_cluster[-1], "end", b_start))
                if (b_start - prev_end) > 1.0:
                    clusters.append(current_cluster)
                    current_cluster = [b]
                else:
                    current_cluster.append(b)
        if current_cluster:
            clusters.append(current_cluster)

        seg_id = _prop(seg, "id", _prop(seg, "index", 0))
        seg_text = str(_prop(seg, "text", "") or "")

        for cluster in clusters:
            c_start = float(_prop(cluster[0], "start", 0.0))
            c_end = float(_prop(cluster[-1], "end", c_start))
            if c_end <= c_start:
                continue

            x_pct = min(float(_prop(b, "x_pct", 0.1)) for b in cluster)
            y_pct = min(float(_prop(b, "y_pct", 0.8)) for b in cluster)
            max_x_pct = max(float(_prop(b, "max_x_pct", 0.9)) for b in cluster)
            max_y_pct = max(float(_prop(b, "max_y_pct", 0.9)) for b in cluster)

            c_text = " ".join(str(_prop(b, "text", "")) for b in cluster if _prop(b, "text")).strip() or seg_text

            raw_events.append({
                "seg_id": seg_id,
                "src_start": c_start,
                "src_end": c_end,
                "x_pct": x_pct,
                "y_pct": y_pct,
                "max_x_pct": max_x_pct,
                "max_y_pct": max_y_pct,
                "text": c_text,
            })

    raw_events.sort(key=lambda ev: (ev["src_start"], ev["src_end"]))

    expected_timeline: List[ExpectedCoverEvent] = []
    for i, ev in enumerate(raw_events):
        next_start = raw_events[i + 1]["src_start"] if (i + 1 < len(raw_events)) else None
        expected_start = max(0.0, ev["src_start"])

        if next_start is not None and (next_start - ev["src_end"]) <= 1.0:
            bridged = True
            expected_end = max(ev["src_end"], next_start)
            next_y = raw_events[i + 1]["y_pct"]
            pos_changed = abs(ev["y_pct"] - next_y) > 0.04
        else:
            bridged = False
            expected_end = ev["src_end"] + 1.0
            pos_changed = False

        if video_duration is not None:
            v_dur = float(video_duration)
            expected_end = min(v_dur, expected_end)
            expected_start = min(v_dur, expected_start)

        hold_sec = max(0.0, expected_end - ev["src_end"])

        expected_timeline.append(
            ExpectedCoverEvent(
                segment_id=ev["seg_id"],
                src_start=round(ev["src_start"], 3),
                src_end=round(ev["src_end"], 3),
                expected_start=round(expected_start, 3),
                expected_end=round(expected_end, 3),
                hold_seconds=round(hold_sec, 3),
                bridged_to_next=bridged,
                position_changed=pos_changed,
                x_pct=round(ev["x_pct"], 4),
                y_pct=round(ev["y_pct"], 4),
                max_x_pct=round(ev["max_x_pct"], 4),
                max_y_pct=round(ev["max_y_pct"], 4),
                text=ev["text"],
            )
        )

    return expected_timeline


def seconds(value):
    h, m, s = value.split(':')
    return int(h)*3600 + int(m)*60 + float(s)


def parse_ass_covers(ass):
    width = re.search(r'^PlayResX:\s*(\d+)', ass, re.M)
    height = re.search(r'^PlayResY:\s*(\d+)', ass, re.M)
    if not width or not height:
        raise ValueError('ASS canvas is missing')
    w, h = int(width[1]), int(height[1])
    outline = 0.
    for line in ass.splitlines():
        if line.startswith('Style: BgStyle,'):
            style = line.split(':', 1)[1].strip().split(',')
            if len(style) > 16:
                outline = max(0., float(style[16]))
    covers = []
    for line in ass.splitlines():
        if not line.startswith('Dialogue:'):
            continue
        fields = line.split(',', 9)
        if len(fields) != 10 or fields[3] != 'BgStyle':
            continue
        if r'\an7' not in fields[9]:
            continue
        pos = re.search(r'\\pos\(([-\d.]+),([-\d.]+)\)', fields[9])
        drawing = re.search(r'\{\\p1\}(.*?)\{\\p0\}', fields[9])
        if not pos or not drawing:
            continue
        numbers = [float(x) for x in re.findall(r'-?\d+(?:\.\d+)?', drawing[1])]
        if len(numbers) < 4:
            continue
        x, y = float(pos[1]), float(pos[2])
        covers.append((seconds(fields[1]), seconds(fields[2]),
            x+min(numbers[::2])-outline, y+min(numbers[1::2])-outline,
            x+max(numbers[::2])+outline, y+max(numbers[1::2])+outline))
    return covers, w, h


def inspect_covers(segments, ass):
    covers, w, h = parse_ass_covers(ass)
    failures, unverified = [], []
    checked = 0
    for seg in segments:
        start, end = float(seg['start']), float(seg['end'])
        blocks = seg.get('tracking_blocks') or ([seg['best_block']] if seg.get('best_block') else [])
        if not blocks:
            unverified.append(seg.get('index'))
        for block in blocks:
            left, right = max(start,float(block['start'])), min(end,float(block['end']))
            if right-left <= .03:
                continue
            checked += 1
            valid = sorted((max(left,a), min(right,z)) for a,z,x,y,r,b in covers
                if z>left and a<right and x<=float(block['x_pct'])*w+2
                and r>=float(block['max_x_pct'])*w-2
                and y<=float(block['y_pct'])*h+2 and b>=float(block['max_y_pct'])*h-2)
            cursor, gap = left, 0.
            for a,z in valid:
                gap += max(0,a-cursor)
                cursor=max(cursor,z)
            gap += max(0,right-cursor)
            if gap > .03:
                failures.append(dict(segment=seg.get('index'),start=left,end=right,uncovered_seconds=round(gap,3)))
    return dict(checked_rectangles=checked, failures=failures, unverified_segments=unverified,
                diagnostic_times=sorted({round(f['start'],2) for f in failures})[:12])


def inspect_frame_pixel_coverage(frame_path, covers, canvas_w=1080, canvas_h=1920, timestamp=None):
    """Verify that active cover boxes contain valid background fill in rendered output pixels."""
    from pathlib import Path
    p = Path(frame_path)
    if not p.is_file():
        return {"checked": False, "reason": "frame_file_missing"}

    # Filter active covers if timestamp is provided.
    # ASS dialog events are displayed on [start, end), so a frame sampled at
    # or after end time does not have the subtitle active.
    active = covers
    if timestamp is not None:
        t = float(timestamp)
        active = [c for c in covers if c[0] <= t < (c[1] - 0.01)]
    if not active:
        return {"checked": False, "reason": "no_active_covers_at_timestamp"}

    try:
        from PIL import Image
        import numpy as np

        with Image.open(p) as img:
            img = img.convert("RGB")
            fw, fh = img.size
            arr = np.array(img)

        scale_x = fw / max(1, canvas_w)
        scale_y = fh / max(1, canvas_h)

        checked_boxes = []
        for cover in active:
            _, _, x1, y1, x2, y2 = cover
            px1 = max(0, min(fw - 1, int(x1 * scale_x)))
            py1 = max(0, min(fh - 1, int(y1 * scale_y)))
            px2 = max(0, min(fw, int(x2 * scale_x)))
            py2 = max(0, min(fh, int(y2 * scale_y)))
            if px2 <= px1 or py2 <= py1:
                continue

            patch = arr[py1:py2, px1:px2]
            h, w = patch.shape[:2]
            if h < 2 or w < 2:
                continue

            # Bright sticker background pixels (white / off-white #F0F0F0)
            is_white = (patch[:, :, 0] > 185) & (patch[:, :, 1] > 185) & (patch[:, :, 2] > 185)
            white_ratio = float(np.mean(is_white))

            # Foreground subtitle text pixels (dark characters, typically 5-15% of sticker)
            is_dark_fg = (patch[:, :, 0] < 85) & (patch[:, :, 1] < 85) & (patch[:, :, 2] < 85)
            foreground_ratio = float(np.mean(is_dark_fg))

            # Total accounted area (sticker background + valid foreground text)
            total_accounted = white_ratio + foreground_ratio

            # A valid subtitle sticker must have a dominant white background (>= 65%),
            # reasonable foreground text coverage (<= 25%), and total accounted area >= 80%.
            # This prevents exposed video scenes or Chinese text bleed (which fail to form a valid sticker).
            has_cover_fill = (
                white_ratio >= 0.65
                and foreground_ratio <= 0.25
                and total_accounted >= 0.80
            )

            checked_boxes.append({
                "bbox": [px1, py1, px2, py2],
                "white_ratio": round(white_ratio, 3),
                "foreground_ratio": round(foreground_ratio, 3),
                "total_accounted": round(total_accounted, 3),
                "has_cover_fill": has_cover_fill,
            })

        if not checked_boxes:
            # Active covers existed at this timestamp, but none produced a valid bounding box on this frame
            return {
                "checked": True,
                "frame": p.name,
                "timestamp": timestamp,
                "boxes_checked": 0,
                "all_boxes_filled": False,
                "reason": "active_covers_unverifiable_or_degenerate",
                "details": [],
            }

        all_ok = all(b["has_cover_fill"] for b in checked_boxes)
        return {
            "checked": True,
            "frame": p.name,
            "timestamp": timestamp,
            "boxes_checked": len(checked_boxes),
            "all_boxes_filled": all_ok,
            "details": checked_boxes,
        }
    except Exception as exc:
        return {"checked": False, "error": str(exc)}
