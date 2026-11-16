"""Source-caption blur geometry shared by ASS, AutoDub and Studio renderers.

No global settings, model imports or filesystem writes: callers freeze settings
and pass source observations, independently of translated speech timing.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict

BLUR_RENDER_VERSION = 2
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


def ass_blur_metadata(segments, settings=None):
    return ASS_BLUR_MARKER + json.dumps({"version": BLUR_RENDER_VERSION,
        "settings": resolve_blur_settings(settings),
        "boxes": collect_source_blur_boxes(segments)}, separators=(",", ":")) + "\n"


def read_ass_blur_metadata(text):
    for line in text.splitlines():
        if line.startswith(ASS_BLUR_MARKER):
            data = json.loads(line[len(ASS_BLUR_MARKER):])
            if data.get("version") != BLUR_RENDER_VERSION:
                raise ValueError("ASS làm mờ cần được tạo lại từ geometry nguồn")
            return {"settings": resolve_blur_settings(data.get("settings")),
                    "boxes": validate_blur_boxes(data.get("boxes"))}
    return None


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


def build_localized_blur_filter(boxes, width, height, settings=None, prefix="caption"):
    """Unlabelled one-input/output filter chain; caller appends crisp subtitles."""
    settings = resolve_blur_settings(settings)
    regions = blur_regions(boxes, width, height, settings)
    parts = []
    for index, (x, y, w, h, intervals) in enumerate(regions):
        # Chroma planes are half-size in yuv420p; clamp their radius separately.
        # FFmpeg requires 2*radius+1 to fit the plane, including even-sized
        # crops. Half the size itself is already one pixel too large.
        luma = min(settings["sub_blur_radius"], (min(w, h) - 1) // 2)
        chroma = min(luma, max(0, (min(w, h) // 2 - 1) // 2))
        enable = "+".join(f"gte(t,{start:.6f})*lt(t,{end:.6f})" for start, end in intervals)
        label = f"{prefix}{index}"
        parts.append(("," if index else "format=yuv420p,") +
            f"split[{label}main][{label}crop];"
            f"[{label}crop]crop={w}:{h}:{x}:{y},"
            f"boxblur=luma_radius={luma}:luma_power=3:chroma_radius={chroma}:chroma_power=3[{label}blur];"
            f"[{label}main][{label}blur]overlay={x}:{y}:enable='{enable}'")
    return "".join(parts)
