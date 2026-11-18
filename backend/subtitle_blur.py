"""Source-caption blur geometry shared by ASS, AutoDub and Studio renderers.

No global settings, model imports or filesystem writes: callers freeze settings
and pass source observations, independently of translated speech timing.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

BLUR_RENDER_VERSION = 6
ASS_BLUR_MARKER = "; ToolV1BlurData: "
BLUR_DEFAULTS = {"sub_blur_radius": 16, "sub_blur_padding_x": 0.015,
                 "sub_blur_padding_y": 0.008}
COVER_STYLES = {"solid_box", "adaptive_box", "frosted_blur", "localized_blur"}


def value(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get("__object_fields__", obj).get(key, default)
    return getattr(obj, key, default)


def seconds(raw):
    return float(raw.total_seconds() if hasattr(raw, "total_seconds") else raw)


def resolve_blur_settings(config=None):
    config = config or {}
    result = {}
    for key, default in BLUR_DEFAULTS.items():
        raw = config.get(key, default)
        if isinstance(raw, bool):
            raise ValueError(f"{key} phải là số")
        number = float(raw)
        high = 32 if key == "sub_blur_radius" else 0.05
        low = 2 if key == "sub_blur_radius" else 0
        if not math.isfinite(number) or not low <= number <= high:
            raise ValueError(f"{key} phải trong khoảng {low}–{high}")
        result[key] = int(number) if key == "sub_blur_radius" else number
    return result


def freeze_subtitle_settings(config=None, defaults=None):
    """Resolve once; old jobs call with their snapshot, never live defaults."""
    merged = dict(defaults or {})
    merged.update(config or {})
    style = str(merged.get("sub_cover_style", "solid_box")).strip().lower()
    if style not in COVER_STYLES:
        raise ValueError("Kiểu che phụ đề không hợp lệ")
    return {"sub_cover_style": style, **resolve_blur_settings(merged)}


def _valid_source(block):
    return not (value(block, "is_subtitle") is False
                or value(block, "is_packaging") is True
                or value(block, "is_static") is True
                or str(value(block, "type", "")).lower() in
                {"packaging", "background", "logo", "watermark"})


def validate_blur_boxes(boxes):
    if not isinstance(boxes, (list, tuple)):
        raise ValueError("Thiếu danh sách vùng phụ đề nguồn đã xác minh")
    result = []
    for block in boxes:
        try:
            row = {key: float(value(block, key)) for key in
                   ("x_pct", "max_x_pct", "y_pct", "max_y_pct")}
            row.update(start=seconds(value(block, "start")), end=seconds(value(block, "end")))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Vùng làm mờ thiếu tọa độ hoặc thời gian nguồn") from exc
        if (not all(math.isfinite(v) for v in row.values())
                or not 0 <= row["x_pct"] < row["max_x_pct"] <= 1
                or not 0 <= row["y_pct"] < row["max_y_pct"] <= 1
                or not 0 <= row["start"] < row["end"]):
            raise ValueError("Tọa độ/thời gian vùng làm mờ không hợp lệ")
        result.append(row)
    return result


def collect_source_blur_boxes(segments):
    boxes = []
    for segment in segments:
        status = value(segment, "ocr_mask_status")
        tracks = value(segment, "tracking_blocks")
        verified = bool(value(segment, "geometry_verified", False))
        if status == "unresolved":
            raise ValueError("Chưa xác minh vùng phụ đề nguồn; cần sửa OCR trước khi làm mờ")
        if status == "no_caption_evidence":
            continue
        # Explicit verified emptiness is authoritative; never resurrect best_block.
        if verified and not tracks:
            continue
        if tracks:
            chosen = [b for b in tracks if _valid_source(b)]
        else:
            best = value(segment, "best_block")
            chosen = [best] if best is not None and _valid_source(best) else []
        if not chosen and status != "no_caption_evidence" and not verified:
            raise ValueError("Thiếu observation phụ đề nguồn; không tự làm mờ băng ngang")
        # Source block timestamps survive TTS fitting. No fallback to cue/audio bounds.
        boxes.extend(validate_blur_boxes(chosen))
    unique = {tuple(row.items()): row for row in boxes}
    return sorted(unique.values(), key=lambda row: (row["start"], row["end"], row["x_pct"]))


def ass_blur_metadata(segments, settings=None, text_boxes=None):
    return ASS_BLUR_MARKER + json.dumps({"version": BLUR_RENDER_VERSION,
        "settings": resolve_blur_settings(settings),
        "boxes": collect_source_blur_boxes(segments),
        "text_boxes": validate_blur_boxes(text_boxes or [])}, separators=(",", ":")) + "\n"


def read_ass_blur_metadata(text):
    for line in text.splitlines():
        if line.startswith(ASS_BLUR_MARKER):
            data = json.loads(line[len(ASS_BLUR_MARKER):])
            # Version 6 changes only the filter execution plan. Version 5 stores
            # the same exact source geometry and remains safe to render.
            if data.get("version") not in {5, BLUR_RENDER_VERSION}:
                raise ValueError("ASS làm mờ cần được tạo lại từ geometry nguồn")
            return {"settings": resolve_blur_settings(data.get("settings")),
                    "boxes": validate_blur_boxes(data.get("boxes")),
                    "text_boxes": validate_blur_boxes(data.get("text_boxes", []))}
    return None


def _rect_contains(outer, inner, tol=1e-6):
    return (
        outer["x_pct"] <= inner["x_pct"] + tol
        and outer["y_pct"] <= inner["y_pct"] + tol
        and outer["max_x_pct"] >= inner["max_x_pct"] - tol
        and outer["max_y_pct"] >= inner["max_y_pct"] - tol
    )


def _rects_intersect(a, b):
    return (
        a["x_pct"] <= b["max_x_pct"]
        and b["x_pct"] <= a["max_x_pct"]
        and a["y_pct"] <= b["max_y_pct"]
        and b["y_pct"] <= a["max_y_pct"]
    )


def _stabilize_blur_shoulders(rows, max_shoulder_s=0.15, min_main_s=0.20):
    """Absorb <=150ms pre/post offset shoulders into an immediately touching outer union box.

    Preserves exact [start, end) intervals while preventing 1-4 frame two-step box-size
    snaps when source caption onset/offset and translated speech differ by tens of ms.
    """
    if len(rows) < 2:
        return rows
    by_interval = defaultdict(list)
    for row in rows:
        by_interval[(row["start"], row["end"])].append(dict(row))
    slices = [(s, e, by_interval[(s, e)]) for s, e in sorted(by_interval)]

    # Forward pass: short pre-shoulder touching a longer containing main slice
    for i in range(len(slices) - 1):
        s0, e0, cur_rects = slices[i]
        s1, e1, nxt_rects = slices[i + 1]
        if not (0.0 < (e0 - s0) <= max_shoulder_s and abs(s1 - e0) <= 1e-6 and (e1 - s1) >= min_main_s):
            continue
        prev_main_rects = (
            slices[i - 1][2]
            if i > 0 and abs(s0 - slices[i - 1][1]) <= 1e-6 and (slices[i - 1][1] - slices[i - 1][0]) >= min_main_s
            else []
        )
        for r in cur_rects:
            if any(_rect_contains(pm, r) for pm in prev_main_rects):
                continue
            containers = [m for m in nxt_rects if _rect_contains(m, r)]
            if len(containers) == 1:
                m = containers[0]
                if not any(other is not r and _rects_intersect(m, other) for other in cur_rects):
                    r.update(x_pct=m["x_pct"], y_pct=m["y_pct"], max_x_pct=m["max_x_pct"], max_y_pct=m["max_y_pct"])

    # Backward pass: short post-shoulder touching a longer containing main slice
    for i in range(len(slices) - 1, 0, -1):
        s0, e0, cur_rects = slices[i]
        ps, pe, prv_rects = slices[i - 1]
        if not (0.0 < (e0 - s0) <= max_shoulder_s and abs(s0 - pe) <= 1e-6 and (pe - ps) >= min_main_s):
            continue
        next_main_rects = (
            slices[i + 1][2]
            if i + 1 < len(slices) and abs(slices[i + 1][0] - e0) <= 1e-6 and (slices[i + 1][1] - slices[i + 1][0]) >= min_main_s
            else []
        )
        for r in cur_rects:
            if any(_rect_contains(nm, r) for nm in next_main_rects):
                continue
            containers = [m for m in prv_rects if _rect_contains(m, r)]
            if len(containers) == 1:
                m = containers[0]
                if not any(other is not r and _rects_intersect(m, other) for other in cur_rects):
                    r.update(x_pct=m["x_pct"], y_pct=m["y_pct"], max_x_pct=m["max_x_pct"], max_y_pct=m["max_y_pct"])

    return [r for _, _, rects in slices for r in rects]


def combine_blur_boxes(source_boxes, text_boxes):
    """Union intersecting source/translated rectangles only while both exist.

    Inputs must already use the same coordinate space. Disjoint captions stay
    separate, and source-only time ranges never inherit translated cue timing.
    """
    rows = validate_blur_boxes(source_boxes) + validate_blur_boxes(text_boxes)
    changes = defaultdict(list)
    for index, row in enumerate(rows):
        changes[row["start"]].append((index, True))
        changes[row["end"]].append((index, False))
    active, result = {}, []
    times = sorted(changes)
    for start, end in zip(times, times[1:]):
        for index, adding in changes[start]:
            if adding:
                active[index] = rows[index]
            else:
                active.pop(index, None)
        rectangles = []
        for row in active.values():
            rect = [row[k] for k in ("x_pct", "y_pct", "max_x_pct", "max_y_pct")]
            index = 0
            while index < len(rectangles):
                other = rectangles[index]
                if (rect[0] <= other[2] and other[0] <= rect[2]
                        and rect[1] <= other[3] and other[1] <= rect[3]):
                    rect = [min(rect[0], other[0]), min(rect[1], other[1]),
                            max(rect[2], other[2]), max(rect[3], other[3])]
                    rectangles.pop(index)
                    index = 0
                else:
                    index += 1
            rectangles.append(rect)
        result.extend(dict(start=start, end=end, x_pct=r[0], y_pct=r[1],
                           max_x_pct=r[2], max_y_pct=r[3]) for r in rectangles)
    return _stabilize_blur_shoulders(result)


def blur_regions(boxes, width, height, settings=None):
    """Coalesce only identical pixel rectangles and overlapping/touching times."""
    settings = resolve_blur_settings(settings)
    width, height = int(width), int(height)
    if width < 2 or height < 2:
        raise ValueError("Kích thước video quá nhỏ để làm mờ")
    grouped = defaultdict(list)
    for row in validate_blur_boxes(boxes):
        px, py = settings["sub_blur_padding_x"], settings["sub_blur_padding_y"]
        x = max(0, math.floor((row["x_pct"] - px) * width / 2) * 2)
        y = max(0, math.floor((row["y_pct"] - py) * height / 2) * 2)
        right = min(width // 2 * 2, math.ceil((row["max_x_pct"] + px) * width / 2) * 2)
        bottom = min(height // 2 * 2, math.ceil((row["max_y_pct"] + py) * height / 2) * 2)
        if right - x < 2 or bottom - y < 2:
            raise ValueError("Vùng làm mờ không có diện tích hợp lệ")
        grouped[(x, y, right - x, bottom - y)].append((row["start"], row["end"]))
    result = []
    for rect, intervals in grouped.items():
        merged = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        result.append((*rect, merged))
    return result


def _blur_timestamp(value):
    # Never round a half-open boundary upward into the next source frame.
    return f"{math.floor(value * 1_000_000) / 1_000_000:.6f}"


def _blur_enable(intervals):
    return "+".join(f"gte(t,{_blur_timestamp(start)})*lt(t,{_blur_timestamp(end)})"
                    for start, end in intervals)


def _regions_overlap_in_time(left, right):
    return any(max(a, c) < min(b, d)
               for a, b in left[4] for c, d in right[4])


def _regions_overlap_in_pixels(left, right):
    x, y, w, h = left[:4]
    ox, oy, ow, oh = right[:4]
    return max(x, ox) < min(x + w, ox + ow) and max(y, oy) < min(y + h, oy + oh)


def _blur_slots(regions):
    """Share only fixed-size crops whose lifetimes never overlap.

    Moving a later region to an earlier overlay slot is safe only if it cannot
    overlap any overlay it passes. Width/height stay fixed: dynamically changing
    crop size does not reliably reconfigure downstream boxblur/framesync.
    """
    slots = []
    for index, region in enumerate(regions):
        for slot in slots:
            if slot["regions"][0][2:4] != region[2:4]:
                continue
            if any(_regions_overlap_in_time(other, region) for other in slot["regions"]):
                continue
            if any(_regions_overlap_in_time(other, region)
                   and _regions_overlap_in_pixels(other, region)
                   for other in regions[slot["first"] + 1:index]):
                continue
            slot["regions"].append(region)
            break
        else:
            slots.append({"first": index, "regions": [region]})
    return slots


def _slot_position(regions, axis):
    initial = regions[0][axis]
    terms = [str(initial)]
    for region in regions[1:]:
        delta = region[axis] - initial
        if delta:
            terms.append(f"({delta})*({_blur_enable(region[4])})")
    return "+".join(terms)


def build_localized_blur_filter(boxes, width, height, settings=None, prefix="caption", *, metrics=None):
    """Unlabelled one-input/output filter chain; caller appends crisp subtitles.

    Each fixed-size slot reads untouched source pixels. Reusing a slot changes
    only its crop/overlay position; incompatible sizes or overlay order retain
    separate branches. Geometry and half-open source lifetimes are unchanged.
    """
    settings = resolve_blur_settings(settings)
    regions = blur_regions(boxes, width, height, settings)
    if not regions:
        if metrics is not None:
            metrics.update(rectangles=0, slots=0, split_branches=0, strategy="empty")
        return ""
    slots = _blur_slots(regions)
    # A moving-position expression has a cost too. On real geometry with mostly
    # different crop sizes, sharing only a few regions regressed CPU benchmarks.
    # Keep the original static graph unless at least a quarter of branches go.
    if len(slots) * 4 > len(regions) * 3:
        slots = [{"first": index, "regions": [region]} for index, region in enumerate(regions)]
    if metrics is not None:
        metrics.update(rectangles=len(regions), slots=len(slots), split_branches=len(slots) + 1,
                       strategy="fixed_size_slots" if len(slots) < len(regions) else "static",
                       active_pixel_seconds=sum(w * h * (end - start)
                           for _, _, w, h, intervals in regions for start, end in intervals))
    # Split the untouched source once. Repeated split/overlay diamonds make
    # FFmpeg negotiate the same upstream graph exponentially as regions grow.
    # Each crop also needs original pixels, not a previous region's blurred output.
    parts = [f"format=yuv420p,split={len(slots) + 1}[{prefix}main0]" +
             "".join(f"[{prefix}{index}crop]" for index in range(len(slots))) + ";"]
    for index, slot in enumerate(slots):
        members = slot["regions"]
        x, y, w, h, _ = members[0]
        intervals = sorted(interval for region in members for interval in region[4])
        # Chroma planes are half-size in yuv420p; clamp their radius separately.
        # FFmpeg requires 2*radius+1 to fit the plane, including even-sized
        # crops. Half the size itself is already one pixel too large.
        luma = min(settings["sub_blur_radius"], (min(w, h) - 1) // 2)
        chroma = min(luma, max(0, (min(w, h) // 2 - 1) // 2))
        enable = _blur_enable(intervals)
        crop_x, crop_y = _slot_position(members, 0), _slot_position(members, 1)
        crop = (f"{w}:{h}:{x}:{y}" if len(members) == 1
                else f"{w}:{h}:x='{crop_x}':y='{crop_y}'")
        overlay = f"{x}:{y}" if len(members) == 1 else f"x='{crop_x}':y='{crop_y}'"
        label = f"{prefix}{index}"
        output = f"[{prefix}main{index + 1}];" if index + 1 < len(slots) else ""
        parts.append(
            f"[{label}crop]crop={crop},"
            f"boxblur=luma_radius={luma}:luma_power=3:chroma_radius={chroma}:chroma_power=3:enable='{enable}'[{label}blur];"
            f"[{prefix}main{index}][{label}blur]overlay={overlay}:enable='{enable}'{output}")
    return "".join(parts)
