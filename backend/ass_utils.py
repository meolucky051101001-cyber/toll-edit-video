import codecs
from difflib import SequenceMatcher
import math
import os
import re
import textwrap
import copy
from dataclasses import dataclass
from datetime import timedelta

_TRAILING_PUNCT_RE = re.compile(r"[.。!！?？,，;；:：]+$")


def _strip_terminal_punct(text):
    raw = str(text or "").strip()
    cleaned = _TRAILING_PUNCT_RE.sub("", raw).strip()
    return cleaned or raw


def _cjk_text(text):
    return "".join(ch for ch in str(text or "") if "\u3400" <= ch <= "\u9fff")


def _merge_contiguous_tracking_blocks(blocks, seg_s=None, seg_e=None):
    """Clamp optional segment bounds and merge contiguous tracking blocks with matching text or geometry."""
    if not blocks:
        return []
    prepared = []
    for block in blocks:
        b = copy.copy(block)
        b_s = float(_block_value(b, "start", 0.0))
        b_e = float(_block_value(b, "end", 0.0))
        if seg_s is not None:
            b_s = max(float(seg_s), b_s)
        if seg_e is not None:
            b_e = min(float(seg_e), b_e)
        if b_e <= b_s + 1e-4:
            continue
        _set_block_value(b, "start", b_s)
        _set_block_value(b, "end", b_e)
        prepared.append(b)
    prepared.sort(key=lambda item: (float(_block_value(item, "start", 0.0)), float(_block_value(item, "end", 0.0))))

    merged = []
    for curr in prepared:
        if not merged:
            merged.append(curr)
            continue
        prev = merged[-1]
        p_end = float(_block_value(prev, "end", 0.0))
        c_start = float(_block_value(curr, "start", 0.0))
        if c_start - p_end > 0.25:
            merged.append(curr)
            continue
        p_y1 = float(_block_value(prev, "y_pct", 0.0))
        p_y2 = float(_block_value(prev, "max_y_pct", 0.0))
        c_y1 = float(_block_value(curr, "y_pct", 0.0))
        c_y2 = float(_block_value(curr, "max_y_pct", 0.0))
        if abs((p_y1 + p_y2) * 0.5 - (c_y1 + c_y2) * 0.5) > 0.08:
            merged.append(curr)
            continue

        p_cjk = _cjk_text(_block_value(prev, "text", ""))
        c_cjk = _cjk_text(_block_value(curr, "text", ""))
        if p_cjk and c_cjk:
            should_merge = (
                p_cjk == c_cjk
                or SequenceMatcher(None, p_cjk, c_cjk).ratio() >= 0.80
            )
        else:
            p_x1 = float(_block_value(prev, "x_pct", 0.0))
            p_x2 = float(_block_value(prev, "max_x_pct", 0.0))
            c_x1 = float(_block_value(curr, "x_pct", 0.0))
            c_x2 = float(_block_value(curr, "max_x_pct", 0.0))
            should_merge = (
                abs(p_x1 - c_x1) <= 0.035
                and abs(p_x2 - c_x2) <= 0.035
                and abs(p_y1 - c_y1) <= 0.025
                and abs(p_y2 - c_y2) <= 0.025
            )
        if not should_merge:
            merged.append(curr)
            continue

        _set_block_value(prev, "end", max(p_end, float(_block_value(curr, "end", 0.0))))
        _set_block_value(prev, "x_pct", min(float(_block_value(prev, "x_pct", 0.0)), float(_block_value(curr, "x_pct", 0.0))))
        _set_block_value(prev, "max_x_pct", max(float(_block_value(prev, "max_x_pct", 0.0)), float(_block_value(curr, "max_x_pct", 0.0))))
        _set_block_value(prev, "y_pct", min(p_y1, c_y1))
        _set_block_value(prev, "max_y_pct", max(p_y2, c_y2))
        prev_txt = str(_block_value(prev, "text", "") or "")
        curr_txt = str(_block_value(curr, "text", "") or "")
        if len(curr_txt) > len(prev_txt):
            _set_block_value(prev, "text", curr_txt)
        elif len(curr_txt) == len(prev_txt) and float(_block_value(curr, "prob", 0.0) or 0.0) > float(_block_value(prev, "prob", 0.0) or 0.0):
            _set_block_value(prev, "text", curr_txt)
            _set_block_value(prev, "prob", float(_block_value(curr, "prob", 0.0) or 0.0))
    return merged

try:
    from .subtitle_layout import subtitle_measure, wrap_subtitle_text
    from .subtitle_text import (
        clean_incomplete_segment_stops,
        split_segment_text,
        split_subtitle_sentences,
    )
except ImportError:
    try:
        from subtitle_layout import subtitle_measure, wrap_subtitle_text
        from subtitle_text import (
            clean_incomplete_segment_stops,
            split_segment_text,
            split_subtitle_sentences,
        )
    except ImportError:
        subtitle_measure = None
        wrap_subtitle_text = None
        split_segment_text = None
        split_subtitle_sentences = None
        clean_incomplete_segment_stops = None


def _block_value(block, key, default=None):
    if block is None:
        return default
    if isinstance(block, dict):
        if "__object_fields__" in block:
            block = block["__object_fields__"]
        return block.get(key, default)
    return getattr(block, key, default)


def _set_block_value(block, key, value):
    if block is None:
        return
    if isinstance(block, dict):
        if "__object_fields__" in block and isinstance(block["__object_fields__"], dict):
            block["__object_fields__"][key] = value
        else:
            block[key] = value
    else:
        setattr(block, key, value)


def _excluded_source(block):
    if block is None:
        return True
    if _block_value(block, "is_subtitle") is False:
        return True
    if _block_value(block, "is_packaging") is True or _block_value(block, "is_static") is True:
        return True
    if str(_block_value(block, "type", "")).lower() in ("packaging", "background", "logo", "watermark"):
        return True
    if _block_value(block, "is_subtitle") is True:
        return False
    if _block_value(block, "in_subtitle_band") is False:
        return True
    return False


def _verified_geometry(segment):
    return bool(_block_value(segment, "geometry_verified", False) or any(
        _block_value(block, "geometry_verified", False)
        for block in (_block_value(segment, "tracking_blocks", None) or [])
    ))


def _is_visual_segment(segment):
    if _block_value(segment, "visual_sub_to_speech", False):
        return True
    return any(
        _block_value(block, "visual_locked", False)
        for block in (_block_value(segment, "tracking_blocks", None) or [])
    )


def _copy_visual_colors(dst: dict, *sources) -> dict:
    """Giữ lại thông tin màu nền/chữ thích ứng (nếu có) khi dựng lại block phủ ASS."""
    for field in ("bg_color_ass", "text_contrast_color_ass"):
        if dst.get(field):
            continue
        for src in sources:
            val = _block_value(src, field, None)
            if val:
                dst[field] = val
                break
    return dst


def _transition_cover_blocks(blocks):
    """Cover sampling uncertainty at contiguous, nearby caption transitions."""
    keys = ('start', 'end', 'x_pct', 'max_x_pct', 'y_pct', 'max_y_pct')
    originals = [
        _copy_visual_colors(
            dict(
                {key: _block_value(b, key, 0.) for key in keys},
                **({'geometry_verified': True} if _block_value(b, "geometry_verified", False) else {}),
            ),
            b,
        )
        for b in blocks
    ]
    result = [dict(b) for b in originals]
    for i, a in enumerate(originals[:-1]):
        b = originals[i+1]
        if (a.get('geometry_verified') or b.get('geometry_verified')
                or abs(b['start']-a['end']) > .02
                or max(a['end']-a['start'], b['end']-b['start']) > .5
                or abs((a['y_pct']+a['max_y_pct'])-(b['y_pct']+b['max_y_pct'])) > .06
                or max(a['max_y_pct'], b['max_y_pct'])-min(a['y_pct'], b['y_pct']) > .10):
            continue
        for key in keys[2:]:
            bound = (max if key.startswith('max_') else min)(a[key], b[key])
            for index in (i, i+1):
                result[index][key] = (max if key.startswith('max_') else min)(result[index][key], bound)
    return result


def _rounded_box(width, height, radius=12):
    """Rounded ASS path inside the existing bounds; no extra border thickness."""
    r = min(radius, width / 2, height / 2)
    k = r * 0.55228475
    w, h = width, height
    return (
        f"m {w} {r:g} l {w} {h-r:g} "
        f"b {w} {h-r+k:g} {w-r+k:g} {h} {w-r:g} {h} "
        f"l {r:g} {h} b {r-k:g} {h} 0 {h-r+k:g} 0 {h-r:g} "
        f"l 0 {r:g} b 0 {r-k:g} {r-k:g} 0 {r:g} 0 "
        f"l {w-r:g} 0 b {w-r+k:g} 0 {w} {r-k:g} {w} {r:g}"
    )


def _held_tracking_blocks(segments, video_duration=None):
    """Visual lifetime independent of translated page/audio boundaries."""
    keys = ('start', 'end', 'x_pct', 'max_x_pct', 'y_pct', 'max_y_pct')
    unique = {}
    for seg in segments:
        blocks = getattr(seg, 'tracking_blocks', None) or []
        if _verified_geometry(seg):
            for block in blocks:
                values = tuple(float(_block_value(block, k, 0)) for k in keys)
                if not _excluded_source(block) and values[1] > values[0]:
                    unique[values] = _copy_visual_colors(dict(zip(keys, values), geometry_verified=True), block)
            continue
        if not blocks and getattr(seg, 'best_block', None):
            best = getattr(seg, 'best_block')
            if not _excluded_source(best):
                blocks = [best]
        # OCR boxes jitter (and can contain a partial last observation). Keep
        # the confirmed caption footprint stable within a nearby vertical band,
        # but never union a genuine move across the screen into a huge card.
        bands = []
        for block in sorted(blocks, key=lambda b: _block_value(b, 'start', 0)):
            values = _copy_visual_colors({k: float(_block_value(block, k, 0)) for k in keys}, block)
            if values['end'] <= values['start']:
                continue
            if bands:
                band = bands[-1]
                low = min(b['y_pct'] for b in band)
                high = max(b['max_y_pct'] for b in band)
                nearby = (abs((low + high) - (values['y_pct'] + values['max_y_pct'])) / 2.0 <= .04
                          and max(high, values['max_y_pct']) - min(low, values['y_pct']) <= .12
                          and values['start'] - max(b['end'] for b in band) <= 1.0)
            else:
                nearby = False
            if not nearby:
                bands.append([])
            bands[-1].append(values)
        stable_blocks = []
        for band in bands:
            bounds = {k: (max if k.startswith('max_') else min)(b[k] for b in band)
                      for k in keys[2:]}
            stable_blocks.extend(dict(b, **bounds) for b in band)
        for block in stable_blocks:
            values = tuple(float(_block_value(block, k, 0)) for k in keys)
            if values[1] > values[0]:
                unique[values] = _copy_visual_colors(dict(zip(keys, values)), block)
    blocks = sorted(unique.values(), key=lambda b: (b['start'], b['end']))
    held = []
    for block in blocks:
        if block.get('geometry_verified'):
            end = min(block['end'], video_duration) if video_duration is not None else block['end']
            held.append(dict(block, end=end))
            continue
        following = [b['start'] for b in blocks if b['start'] > block['start'] + .0001 and b['end'] > block['end']]
        end = block['end'] + 1.0
        if following:
            end = max(block['end'], min(end, min(following)))
        if video_duration is not None:
            end = min(end, video_duration)
        held.append(dict(block, end=end))
    return held
def sync_and_clamp_subtitles(translated_segments, dubbing_audio_files=None):
    """
    Đồng bộ thời lượng phụ đề chuẩn xác theo giọng đọc thực tế và chống đè chéo (Anti-Overlap).
    - Cập nhật seg.end theo độ dài thực tế của file âm thanh TTS
    - Đảm bảo phụ đề câu trước kết thúc trước câu sau ít nhất 0.05s
    - TUYỆT ĐỐI KHÔNG dịch chuyển next_seg.start về sau vì âm thanh TTS bắt đầu tại next_seg.start.
    """
    import datetime
    if not translated_segments:
        return translated_segments

    # 1. Đồng bộ end time theo độ dài thực tế của file âm thanh TTS
    if dubbing_audio_files:
        audio_dur_map = {}
        for audio_info in dubbing_audio_files:
            if isinstance(audio_info, dict):
                idx = audio_info.get("index")
                actual_duration = audio_info.get("actual_audio_duration") or audio_info.get("duration", 0)
                if idx is not None and actual_duration > 0:
                    audio_dur_map[idx] = float(actual_duration)

        for seg in translated_segments:
            seg_idx = getattr(seg, "index", None) if not isinstance(seg, dict) else seg.get("index")
            if seg_idx in audio_dur_map:
                actual_dur = audio_dur_map[seg_idx]
                if isinstance(seg, dict):
                    start_s = float(seg.get("start", 0))
                    seg["end"] = start_s + actual_dur
                else:
                    seg.end = seg.start + datetime.timedelta(seconds=actual_dur)

    # 2. Chống đè sub (Anti-Overlap): Đảm bảo sub trước phải biến mất trước khi sub sau xuất hiện
    # TUYỆT ĐỐI KHÔNG dịch chuyển next_seg.start về sau (Codex Plan - Điểm 4)
    for i in range(len(translated_segments) - 1):
        curr_seg = translated_segments[i]
        next_seg = translated_segments[i + 1]
        if isinstance(curr_seg, dict):
            c_start = float(curr_seg.get("start", 0))
            c_end = float(curr_seg.get("end", 0))
            n_start = float(next_seg.get("start", 0)) if isinstance(next_seg, dict) else next_seg.start.total_seconds()
            if c_end > n_start:
                safe_end = n_start - 0.05
                curr_seg["end"] = max(c_start + 0.1, safe_end) if safe_end > c_start else c_start + 0.1
        else:
            n_start = next_seg.start if hasattr(next_seg, 'start') else datetime.timedelta(seconds=float(next_seg.get('start', 0)))
            if curr_seg.end > n_start:
                safe_end = n_start - datetime.timedelta(seconds=0.05)
                if safe_end > curr_seg.start:
                    curr_seg.end = safe_end
                else:
                    curr_seg.end = curr_seg.start + datetime.timedelta(seconds=0.1)

    if dubbing_audio_files:
        for seg in translated_segments:
            if _is_visual_segment(seg) or _verified_geometry(seg):
                # Visual OCR cues keep the exact on-screen lifetime of the
                # burned-in caption; only speech/text timing may stretch.
                continue
            if isinstance(seg, dict):
                seg_s = float(seg.get("start", 0.0))
                seg_e = float(seg.get("end", 0.0))
                t_blocks = seg.get("tracking_blocks") or []
            else:
                seg_s = seg.start.total_seconds()
                seg_e = seg.end.total_seconds()
                t_blocks = getattr(seg, "tracking_blocks", None) or []
            valid_tb = [b for b in t_blocks if not _excluded_source(b)]
            valid_tb = _merge_contiguous_tracking_blocks(valid_tb, seg_s=seg_s, seg_e=seg_e)
            if isinstance(seg, dict):
                if valid_tb:
                    seg["tracking_blocks"] = valid_tb
            else:
                if valid_tb:
                    seg.tracking_blocks = valid_tb
            if valid_tb:
                if float(_block_value(valid_tb[0], "start", seg_s)) > seg_s:
                    _set_block_value(valid_tb[0], "start", seg_s)
                if float(_block_value(valid_tb[-1], "end", seg_e)) < seg_e:
                    _set_block_value(valid_tb[-1], "end", seg_e)
                for k in range(len(valid_tb) - 1):
                    ce = float(_block_value(valid_tb[k], "end", seg_s))
                    ns = float(_block_value(valid_tb[k + 1], "start", seg_e))
                    if ce < ns:
                        mid = (ce + ns) * 0.5
                        _set_block_value(valid_tb[k], "end", mid)
                        _set_block_value(valid_tb[k + 1], "start", mid)

    return translated_segments




@dataclass(frozen=True)
class _AssLayout:
    canvas_x: object
    canvas_y: object
    font_size: object
    line_h: object
    outline: object
    is_landscape: object
    sticker_padding_x: object
    sticker_padding_y: object
    min_margin: object
    max_allowed_w: object
    text_max_w: object
    watermark_x_limit: object
    safe_bottom_text_max_w: object
    measure: object
    main_y_pct: object
    watermark_box: object
    wrap_text: object
    cover_style: str = "solid_box"
    strip_terminal_punctuation: bool = False
    blur_text_boxes: object = None


@dataclass(frozen=True)
class _AssBand:
    main_track_centers: object
    med_center_pct: object
    locked_center_y: object
    locked_band_min_y: object
    locked_band_max_y: object
    video_has_source: object


@dataclass(frozen=True)
class _AssText:
    num_lines: object
    formatted_text: object
    actual_text_w: object
    required_text_h: object
    glyph_bounds: object = None


def _format_ass_time(td):
    ticks = max(0, round(td.total_seconds() * 100))
    total_seconds, centiseconds = divmod(ticks, 100)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

def generate_ass_file(
    dialogue_segments,
    floating_segments,
    output_path,
    play_res_x=1080,
    play_res_y=1920,
    main_y_pct=0.85,
    *,
    font_name="Arial",
    font_color="&H00000000",
    font_weight=2,
    video_duration=None,
    watermark_box=None,
    cover_style=None,
    strip_terminal_punctuation=False,
    blur_settings=None,
    source_segments=None,
):
    """Generate an ASS subtitle file.
    
    When source subtitles are selected, uses full X-Y bounding boxes to cover them.
    If no source subtitles are present (has_source is False), no cover box is drawn
    (no default box in the middle of the screen).
    Supports both landscape (horizontal) and portrait (vertical) videos.
    """
    try:
        from .v1_ocr_geometry import validate_mask_geometry
    except ImportError:
        from v1_ocr_geometry import validate_mask_geometry
    validate_mask_geometry(dialogue_segments)
    if not font_name.strip() or any(char in font_name for char in ",\r\n"):
        font_name = "Arial"
    if not re.fullmatch(r"&H[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?&?", font_color):
        font_color = "&H00000000"

    bold = -1 if font_weight > 1 else 0

    # Normalize scale while preserving any source aspect ratio (including square).
    source_x = max(1, int(play_res_x or 1080))
    source_y = max(1, int(play_res_y or 1920))
    scale = 720.0 / min(source_x, source_y)
    canvas_x = round(source_x * scale)
    canvas_y = round(source_y * scale)
    font_size = 32 if source_x > source_y else 38
    line_h = 36 if source_x > source_y else 42
    outline = 0

    is_landscape = canvas_x > canvas_y
    sticker_padding_x = 18
    sticker_padding_y = 8 if is_landscape else 10
    min_margin = math.ceil(canvas_x * 0.05)
    max_allowed_w = canvas_x - 2 * min_margin
    # Subtitle wrapping strictly at 90% of video width:
    # "khi nào kí tự kéo dài đến 90% chiều ngang video rồi mới xuống dòng"
    text_max_w = int(canvas_x * 0.90)

    # Watermark protection zone (bottom-right red seal at X >= 0.8468, Y >= 0.9074 on landscape videos)
    if is_landscape and watermark_box is not None:
        wm_x1 = watermark_box[0]
        wm_ratio = (wm_x1 / source_x) if wm_x1 > 1.0 else wm_x1
        watermark_x_limit = int(canvas_x * min(wm_ratio - 0.02, 1574.0 / 1920.0))
        safe_bottom_text_max_w = int(canvas_x * 0.65)
    else:
        watermark_x_limit = canvas_x - min_margin
        safe_bottom_text_max_w = text_max_w

    # Initialize font measurement if available
    measure = None
    if subtitle_measure is not None:
        try:
            measure = subtitle_measure(font_name, font_size, bold != 0)
        except Exception:
            measure = None

    if measure is None:
        char_est = int(font_size * 0.55)
        def _measure(text):
            return len(text) * char_est
        measure = _measure

    def _wrap_text(text, max_w):
        if wrap_subtitle_text is not None:
            try:
                # Wrap greedily: text fills up to max_w (90% width) before wrapping
                return wrap_subtitle_text(text, max_w, measure)
            except Exception:
                pass
        chars = max(1, int(max_w / (font_size * 0.55)))
        return textwrap.wrap(text, width=chars) or [text]

    active_cover_style = str(cover_style or os.getenv("V1_SUBTITLE_COVER_STYLE", "")).strip().lower()
    if not active_cover_style:
        try:
            import canvas_settings as _cs
            active_cover_style = str(_cs.get_canvas_settings().get("sub_cover_style", "solid_box")).strip().lower()
        except Exception:
            active_cover_style = "solid_box"
    if active_cover_style not in ("solid_box", "frosted_blur", "adaptive_box", "localized_blur"):
        active_cover_style = "solid_box"
    if active_cover_style == "localized_blur":
        if not hasattr(measure, "glyph_bounds"):
            raise ValueError("Không đo được font phụ đề để tạo vùng blur chính xác")
        font_name = measure.font_family

    text_outline = 2 if active_cover_style == "localized_blur" else 0
    text_shadow = 1 if active_cover_style == "localized_blur" else 0
    if active_cover_style == "frosted_blur":
        bg_color_ass = "&H50FFFFFF"
    elif active_cover_style == "localized_blur":
        bg_color_ass = "&HFF000000"
    else:
        bg_color_ass = "&H00FFFFFF"
    style_text_color = (
        "&H00FFFFFF"
        if (active_cover_style == "localized_blur" and font_color == "&H00000000")
        else font_color
    )

    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_x}
PlayResY: {canvas_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BgStyle,Arial,{font_size},{bg_color_ass},{bg_color_ass},{bg_color_ass},{bg_color_ass},0,0,0,0,100,100,0,0,1,{outline},0,5,0,0,0,1
Style: TextStyle,{font_name},{font_size},{style_text_color},&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,{text_outline},{text_shadow},5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    blur_source_segments = dialogue_segments if source_segments is None else source_segments
    blur_text_boxes = []
    c = _AssLayout(
        canvas_x=canvas_x,
        canvas_y=canvas_y,
        font_size=font_size,
        line_h=line_h,
        outline=outline,
        is_landscape=is_landscape,
        sticker_padding_x=sticker_padding_x,
        sticker_padding_y=sticker_padding_y,
        min_margin=min_margin,
        max_allowed_w=max_allowed_w,
        text_max_w=text_max_w,
        watermark_x_limit=watermark_x_limit,
        safe_bottom_text_max_w=safe_bottom_text_max_w,
        measure=measure,
        main_y_pct=main_y_pct,
        watermark_box=watermark_box,
        wrap_text=_wrap_text,
        cover_style=active_cover_style,
        strip_terminal_punctuation=bool(strip_terminal_punctuation),
        blur_text_boxes=blur_text_boxes,
    )
    if dialogue_segments:
        dialogue_segments, held_tracks = _align_ass_source_pages(dialogue_segments, video_duration)
        processed_segments = _paginate_ass_dialogue(dialogue_segments, c)
        band_layout = _lock_ass_band(processed_segments, dialogue_segments, c)
        ass_content += _render_ass_dialogue(processed_segments, held_tracks, c, band_layout)

    if active_cover_style == "localized_blur":
        from subtitle_blur import ass_blur_metadata
        ass_content += ass_blur_metadata(blur_source_segments, blur_settings, blur_text_boxes)

    with codecs.open(output_path, "w", "utf-8-sig") as f:
        f.write(ass_content)


def _align_ass_source_pages(dialogue_segments, video_duration):
    dialogue_segments = copy.deepcopy(list(dialogue_segments))
    for seg in dialogue_segments:
        # Cached/imported geometry must obey the same classification as OCR.
        if _excluded_source(seg):
            seg.tracking_blocks = []
            seg.best_block = None
        else:
            original_tracks = getattr(seg, "tracking_blocks", None) or []
            seg.tracking_blocks = [b for b in original_tracks if not _excluded_source(b)]
            if _excluded_source(getattr(seg, "best_block", None)) or (
                original_tracks and not seg.tracking_blocks
            ):
                seg.best_block = None
            if seg.tracking_blocks and not _verified_geometry(seg):
                first_b = seg.tracking_blocks[0]
                b_start = float(_block_value(first_b, "start", seg.start.total_seconds()))
                s_start = seg.start.total_seconds()
                if 0.0 < (b_start - s_start) <= 1.0:
                    if isinstance(first_b, dict):
                        first_b["start"] = s_start
                    else:
                        setattr(first_b, "start", s_start)
    groups = []
    held_tracks = _held_tracking_blocks(dialogue_segments, video_duration)
    for seg in dialogue_segments:
        source_id = getattr(seg, "source_segment_id", None) or seg.index
        if not groups or groups[-1][0] != source_id:
            groups.append((source_id, []))
        groups[-1][1].append(seg)
    for _, pages in groups:
        if all(_verified_geometry(page) for page in pages):
            # Source masks have their own lifetime. Confirmed observations must
            # not extend translated dialogue into a period without speech.
            continue
        tracks = [b for page in pages for b in
                  (getattr(page, "tracking_blocks", None) or [])]
        if not tracks:
            for page in pages:
                best = getattr(page, "best_block", None)
                if best and not _excluded_source(best):
                    tracks.append(best)
        if tracks:
            first = min(float(_block_value(b, "start", pages[0].start.total_seconds())) for b in tracks)
            last = max(float(_block_value(b, "end", pages[-1].end.total_seconds())) for b in tracks)
            is_short = len(str(getattr(pages[0], "content", "")).split()) <= 2
            if not is_short and pages[0].start.total_seconds() - 0.30001 <= first:
                pages[0].start = timedelta(seconds=max(0, min(
                    pages[0].start.total_seconds(), first)))
            def _track_matches_held(b, t):
                t_start = float(_block_value(t, 'start', pages[0].start.total_seconds()))
                t_end = float(_block_value(t, 'end', pages[-1].end.total_seconds()))
                if abs(b['start'] - t_start) < 0.05:
                    return True
                if (b['start'] - 0.05 <= t_start <= b['end'] + 0.05) or (b['start'] - 0.05 <= t_end <= b['end'] + 0.05):
                    b_cy = (b['y_pct'] + b['max_y_pct']) / 2.0
                    t_cy = (float(_block_value(t, 'y_pct', 0)) + float(_block_value(t, 'max_y_pct', 0))) / 2.0
                    if abs(b_cy - t_cy) <= 0.06:
                        return True
                return False

            matching_ends = [b['end'] for b in held_tracks if any(_track_matches_held(b, t) for t in tracks)]
            held_end = max(matching_ends, default=last)
            if held_end > pages[-1].end.total_seconds():
                pages[-1].end = timedelta(seconds=held_end)
    for i in range(len(groups) - 1):
        boundary = groups[i + 1][1][0].start
        if any(_verified_geometry(page) for page in groups[i][1] + groups[i + 1][1]):
            for page in groups[i][1]:
                if _is_visual_segment(page):
                    continue
                page.end = min(page.end, boundary)
            continue
        # Check if next group has valid tracking blocks
        next_tracks = [b for page in groups[i + 1][1] for b in (getattr(page, "tracking_blocks", None) or []) if not _excluded_source(b)]
        if not next_tracks:
            for page in groups[i + 1][1]:
                best = getattr(page, "best_block", None)
                if best and not _excluded_source(best):
                    next_tracks.append(best)

        curr_tracks = [b for page in groups[i][1] for b in (getattr(page, "tracking_blocks", None) or []) if not _excluded_source(b)]
        if not curr_tracks:
            for page in groups[i][1]:
                best = getattr(page, "best_block", None)
                if best and not _excluded_source(best):
                    curr_tracks.append(best)

        next_is_short = len(str(getattr(groups[i + 1][1][0], "content", "")).split()) <= 2
        transition_target = boundary
        if not next_is_short and curr_tracks and next_tracks:
            curr_y = (min(float(_block_value(b, "y_pct", 0)) for b in curr_tracks) + max(float(_block_value(b, "max_y_pct", 0)) for b in curr_tracks)) / 2.0
            next_y = (min(float(_block_value(b, "y_pct", 0)) for b in next_tracks) + max(float(_block_value(b, "max_y_pct", 0)) for b in next_tracks)) / 2.0
            if abs(curr_y - next_y) <= 0.05:
                # Same position: bridge to next visual onset, but never exceed next sentence start
                next_visual_start = min(float(_block_value(b, "start", 0)) for b in next_tracks)
                transition_target = min(timedelta(seconds=next_visual_start), boundary)
            else:
                # Position changed: end when next group starts to prevent dialogue text collision
                transition_target = boundary
        else:
            transition_target = boundary

        for page in groups[i][1]:
            if _is_visual_segment(page):
                continue
            gap = (transition_target - page.end).total_seconds()
            if 0.0 < gap <= 0.5:
                page.end = transition_target
            else:
                page.end = min(page.end, transition_target)
    dialogue_segments = [s for s in dialogue_segments if s.end > s.start]
    held_tracks = _held_tracking_blocks(dialogue_segments, video_duration)
    return dialogue_segments, held_tracks


def _paginate_ass_dialogue(dialogue_segments, c):
    if clean_incomplete_segment_stops is not None:
        dialogue_segments = clean_incomplete_segment_stops(dialogue_segments)
    processed_segments = []
    if split_subtitle_sentences is not None and split_segment_text is not None:
        try:
            for seg in dialogue_segments:
                blist = [b for b in (getattr(seg, "tracking_blocks", None) or []) if not _excluded_source(b)]
                if not blist and getattr(seg, "best_block", None):
                    bb = getattr(seg, "best_block")
                    if not _excluded_source(bb):
                        blist = [bb]
                seg_y_val = (
                    (float(_block_value(blist[0], "y_pct", 0.0)) + float(_block_value(blist[0], "max_y_pct", 0.0))) * 0.5
                    if blist else float(getattr(seg, "y_pct", c.main_y_pct) or c.main_y_pct)
                )
                effective_split_w = c.safe_bottom_text_max_w if (c.is_landscape and seg_y_val >= 0.80) else c.text_max_w
                parts = []
                for sentence in split_subtitle_sentences(seg.content):
                    lines = c.wrap_text(sentence, effective_split_w)
                    sentence_parts = list(
                        " ".join(lines[offset : offset + 2])
                        for offset in range(0, len(lines), 2)
                    )
                    # Rebalance a dangling last page such as "xếp.".
                    if len(sentence_parts) > 1 and len(sentence_parts[-1].split()) <= 3:
                        words = (sentence_parts[-2] + " " + sentence_parts[-1]).split()
                        choices = []
                        for cut in range(3, len(words) - 2):
                            a, b = " ".join(words[:cut]), " ".join(words[cut:])
                            if len(c.wrap_text(a, effective_split_w)) <= 2 and len(c.wrap_text(b, effective_split_w)) <= 2:
                                choices.append((abs(cut - len(words) / 2), a, b))
                        if choices:
                            _, a, b = min(choices)
                            sentence_parts[-2:] = [a, b]
                    parts.extend(sentence_parts)
                if getattr(c, "strip_terminal_punctuation", False):
                    parts = [_strip_terminal_punct(part) for part in parts]
                pages = split_segment_text(seg, parts)
                if len(pages) == 2:
                    raw_tracks = [b for b in (getattr(seg, "tracking_blocks", None) or []) if not _excluded_source(b)]
                    merged_tracks = _merge_contiguous_tracking_blocks(raw_tracks)
                    if len(merged_tracks) == 2:
                        t_split = float(_block_value(merged_tracks[0], "end", 0.0))
                        seg_s = seg.start.total_seconds()
                        seg_e = seg.end.total_seconds()
                        if seg_s + 0.30 <= t_split <= seg_e - 0.30:
                            pages[0].start = timedelta(seconds=seg_s)
                            pages[0].end = timedelta(seconds=t_split)
                            pages[1].start = timedelta(seconds=t_split)
                            pages[1].end = timedelta(seconds=seg_e)
                            pages[0].tracking_blocks = [copy.copy(merged_tracks[0])]
                            pages[0].best_block = copy.copy(merged_tracks[0])
                            pages[1].tracking_blocks = [copy.copy(merged_tracks[1])]
                            pages[1].best_block = copy.copy(merged_tracks[1])
                processed_segments.extend(pages)
        except Exception:
            processed_segments = list(dialogue_segments)
    else:
        processed_segments = list(dialogue_segments)
    return processed_segments


def _lock_ass_band(processed_segments, dialogue_segments, c):
    # Collect baseline centers for the global subtitle band to lock vertical jitter across sentences
    main_track_centers = []
    main_track_heights = []
    for s in processed_segments:
        blist = [b for b in (getattr(s, "tracking_blocks", None) or []) if not _excluded_source(b)]
        if not blist and getattr(s, "best_block", None):
            bb = getattr(s, "best_block")
            if not _excluded_source(bb):
                blist = [bb]
        for b in blist:
            y1 = float(_block_value(b, "y_pct", 0.0))
            y2 = float(_block_value(b, "max_y_pct", 0.0))
            if 0.0 < y1 < y2 < 1.0:
                cy = (y1 + y2) * 0.5
                if 0.05 <= cy <= 0.95:
                    main_track_centers.append(cy)
                    main_track_heights.append(y2 - y1)

    video_has_source = any(getattr(seg, "best_block", None) or getattr(seg, "tracking_blocks", None) for seg in dialogue_segments)

    if main_track_centers:
        sorted_centers = sorted(main_track_centers)
        sorted_heights = sorted(main_track_heights)
        n_centers = len(sorted_centers)
        if n_centers % 2 == 1:
            med_center_pct = sorted_centers[n_centers // 2]
            med_h_pct = sorted_heights[len(sorted_heights) // 2]
        else:
            med_center_pct = (sorted_centers[n_centers // 2 - 1] + sorted_centers[n_centers // 2]) * 0.5
            med_h_pct = (sorted_heights[len(sorted_heights) // 2 - 1] + sorted_heights[len(sorted_heights) // 2]) * 0.5
        locked_center_y = round(med_center_pct * c.canvas_y)
        locked_track_h = max(c.line_h, int(med_h_pct * c.canvas_y))
        locked_band_tops = []
        locked_band_bottoms = []
        for s in processed_segments:
            blist = [b for b in (getattr(s, "tracking_blocks", None) or []) if not _excluded_source(b)]
            if not blist and getattr(s, "best_block", None):
                bb = getattr(s, "best_block")
                if not _excluded_source(bb):
                    blist = [bb]
            for b in blist:
                y1 = float(_block_value(b, "y_pct", 0.0))
                y2 = float(_block_value(b, "max_y_pct", 0.0))
                if 0.0 < y1 < y2 < 1.0 and abs(((y1 + y2) * 0.5) - med_center_pct) <= 0.015:
                    locked_band_tops.append(y1)
                    locked_band_bottoms.append(y2)
        locked_band_min_y = min(locked_band_tops, default=med_center_pct - med_h_pct * 0.5)
        locked_band_max_y = max(locked_band_bottoms, default=med_center_pct + med_h_pct * 0.5)
        max_band_h = max((c.line_h * 2.2) / max(1, c.canvas_y), med_h_pct * 1.35)
        if (locked_band_max_y - locked_band_min_y) > max_band_h:
            half_h = max_band_h * 0.5
            locked_band_min_y = max(0.0, med_center_pct - half_h)
            locked_band_max_y = min(1.0, med_center_pct + half_h)
    else:
        med_center_pct = c.main_y_pct + (c.line_h / (2.0 * max(1, c.canvas_y)))
        locked_center_y = int(c.main_y_pct * c.canvas_y) + (c.line_h // 2)
        locked_track_h = c.line_h
        locked_band_min_y = None
        locked_band_max_y = None
    return _AssBand(main_track_centers, med_center_pct, locked_center_y, locked_band_min_y, locked_band_max_y, video_has_source)


def _relevant_ass_tracks(seg, held_tracks, seg_s, seg_e, c):
    verified = _verified_geometry(seg)
    # Determine seg's own vertical anchor region so we never merge unrelated subtitles/titles
    # from completely different parts of the screen (e.g. top title at y~0.25 vs bottom speech at y~0.85).
    seg_blist = [b for b in (getattr(seg, "tracking_blocks", None) or []) if not _excluded_source(b)]
    if not verified and not seg_blist and getattr(seg, "best_block", None):
        bb = getattr(seg, "best_block")
        if not _excluded_source(bb):
            seg_blist = [bb]

    if seg_blist:
        seg_cy = sum((float(_block_value(b, "y_pct", 0.0)) + float(_block_value(b, "max_y_pct", 0.0))) * 0.5 for b in seg_blist) / len(seg_blist)
        seg_top = min(float(_block_value(b, "y_pct", 0.0)) for b in seg_blist)
        seg_bottom = max(float(_block_value(b, "max_y_pct", 0.0)) for b in seg_blist)
    else:
        seg_cy = float(getattr(seg, "y_pct", c.main_y_pct) or c.main_y_pct)
        seg_top = seg_cy - 0.03
        seg_bottom = seg_cy + 0.03

    blocks = getattr(seg, "tracking_blocks", None) or []
    if not verified and not blocks and getattr(seg, "best_block", None):
        bb = getattr(seg, "best_block")
        if not _excluded_source(bb):
            blocks = [dict(
                start=float(_block_value(bb, "start", seg_s)),
                end=float(_block_value(bb, "end", seg_e)),
                x_pct=float(_block_value(bb, "x_pct", 0.0)),
                max_x_pct=float(_block_value(bb, "max_x_pct", 1.0)),
                y_pct=float(_block_value(bb, "y_pct", c.main_y_pct)),
                max_y_pct=float(_block_value(bb, "max_y_pct", c.main_y_pct + 0.05)),
            )]
    blocks = sorted(blocks, key=lambda b: _block_value(b, "start", 0.0))
    blocks = [b for b in blocks if not _excluded_source(b)]
    raw_relevant = [b for b in held_tracks if b['end'] > seg_s and b['start'] < seg_e]
    relevant = []
    for b in raw_relevant:
        b_y1 = float(_block_value(b, "y_pct", 0.0))
        b_y2 = float(_block_value(b, "max_y_pct", 0.0))
        b_cy = (b_y1 + b_y2) * 0.5
        if seg_blist:
            for sb in seg_blist:
                sb_y1 = float(_block_value(sb, "y_pct", 0.0))
                sb_y2 = float(_block_value(sb, "max_y_pct", 0.0))
                sb_cy = (sb_y1 + sb_y2) * 0.5
                if abs(b_cy - sb_cy) <= 0.12 and (max(b_y2, sb_y2) - min(b_y1, sb_y1)) <= max(0.24, (sb_y2 - sb_y1) + 0.06):
                    relevant.append(b)
                    break
        else:
            b_span = max(b_y2, seg_bottom) - min(b_y1, seg_top)
            if abs(b_cy - seg_cy) <= 0.12 and b_span <= 0.16:
                relevant.append(b)
    return seg_blist, seg_cy, blocks, relevant, verified


def _ass_segment_events(seg, held_tracks, seg_s, seg_e, c):
    sub_events = []
    seg_blist, seg_cy, blocks, relevant, verified = _relevant_ass_tracks(seg, held_tracks, seg_s, seg_e, c)
    if verified:
        blocks = []
    if relevant:
        # Concurrent source tracks must remain covered even when the
        # translated text has already switched to the next page.
        boundaries = sorted({seg_s, seg_e} | {
            max(seg_s, min(seg_e, b[k])) for b in relevant for k in ('start', 'end')})
        blocks = []
        for left, right in zip(boundaries, boundaries[1:]):
            active = [b for b in relevant if b['start'] < right and b['end'] > left]
            if active:
                # Extra guard: If multiple active blocks span > 0.16 vertically,
                # retain only the ones closest to the active segment block at mid_slice.
                span_active = max(float(_block_value(b, "max_y_pct", 0.0)) for b in active) - min(float(_block_value(b, "y_pct", 0.0)) for b in active)
                if len(active) > 1 and span_active > max(0.16, max(float(_block_value(b, "max_y_pct", 0.0)) - float(_block_value(b, "y_pct", 0.0)) for b in active) + 0.04):
                    mid_slice = (left + right) * 0.5
                    ref_cy = seg_cy
                    if seg_blist:
                        best_sb = min(
                            seg_blist,
                            key=lambda sb: 0.0 if (float(_block_value(sb, "start", seg_s)) <= mid_slice <= float(_block_value(sb, "end", seg_e))) else min(abs(mid_slice - float(_block_value(sb, "start", seg_s))), abs(mid_slice - float(_block_value(sb, "end", seg_e))))
                        )
                        ref_cy = (float(_block_value(best_sb, "y_pct", 0.0)) + float(_block_value(best_sb, "max_y_pct", 0.0))) * 0.5
                    active = [min(active, key=lambda b: abs((float(_block_value(b, "y_pct", 0.0)) + float(_block_value(b, "max_y_pct", 0.0))) * 0.5 - ref_cy))]
                blocks.append(_copy_visual_colors(dict(start=left, end=right,
                    x_pct=min(float(_block_value(b, 'x_pct', 0.0)) for b in active),
                    max_x_pct=max(float(_block_value(b, 'max_x_pct', 0.0)) for b in active),
                    y_pct=min(float(_block_value(b, 'y_pct', 0.0)) for b in active),
                    max_y_pct=max(float(_block_value(b, 'max_y_pct', 0.0)) for b in active),
                    **({'geometry_verified': True} if verified or all(_block_value(b, 'geometry_verified', False) for b in active) else {})), *active))
    blocks = _transition_cover_blocks(blocks)
    if not verified and blocks:
        if seg_s < float(_block_value(blocks[0], "start", seg_s)):
            if isinstance(blocks[0], dict):
                blocks[0]["start"] = seg_s
            else:
                setattr(blocks[0], "start", seg_s)
        if float(_block_value(blocks[-1], "end", seg_e)) < seg_e:
            if isinstance(blocks[-1], dict):
                blocks[-1]["end"] = seg_e
            else:
                setattr(blocks[-1], "end", seg_e)
        for b_idx in range(len(blocks) - 1):
            curr_e = float(_block_value(blocks[b_idx], "end", seg_s))
            next_s = float(_block_value(blocks[b_idx + 1], "start", seg_e))
            if curr_e < next_s:
                mid_t = (curr_e + next_s) * 0.5
                if isinstance(blocks[b_idx], dict):
                    blocks[b_idx]["end"] = mid_t
                else:
                    setattr(blocks[b_idx], "end", mid_t)
                if isinstance(blocks[b_idx + 1], dict):
                    blocks[b_idx + 1]["start"] = mid_t
                else:
                    setattr(blocks[b_idx + 1], "start", mid_t)
    cursor = seg_s
    seg_is_visual = _is_visual_segment(seg)
    for b in blocks:
        left = max(seg_s, float(_block_value(b, "start", seg_s)))
        right = min(seg_e, float(_block_value(b, "end", seg_e)))
        if right <= left or right <= cursor:
            continue
        if left > cursor and not seg_is_visual:
            sub_events.append({"start": cursor, "end": left, "block": None})
        sub_events.append({"start": left, "end": right, "block": b})
        cursor = right
    if cursor < seg_e and not seg_is_visual:
        sub_events.append({
            "start": cursor,
            "end": seg_e,
            "block": None,
        })
    return sub_events, verified, seg_is_visual


def _format_ass_event_text(text, is_near_bottom, c):
    if getattr(c, "strip_terminal_punctuation", False):
        text = " ".join(_strip_terminal_punct(part) for part in str(text or "").split(r"\N") if part.strip()) or str(text or "")
    effective_text_w = c.safe_bottom_text_max_w if is_near_bottom else c.text_max_w
    lines = c.wrap_text(text, effective_text_w)
    if len(lines) > 2:
        # Enforce strictly maximum 2 lines per subtitle event
        lines = [lines[0], " ".join(lines[1:])]
    num_lines = len(lines)

    scale_x = 100
    if num_lines == 2 and c.measure(lines[1]) > c.max_allowed_w:
        scale_x = max(70, min(100, int((c.max_allowed_w / c.measure(lines[1])) * 100)))
        escaped_l0 = lines[0].replace("\\", "／").replace("{", "｛").replace("}", "｝")
        escaped_l1 = lines[1].replace("\\", "／").replace("{", "｛").replace("}", "｝")
        formatted_text = f"{escaped_l0}\\N{{\\fscx{scale_x}}}{escaped_l1}"
        actual_text_w = math.ceil(max(c.measure(lines[0]), c.measure(lines[1]) * (scale_x / 100.0)))
    else:
        formatted_text = "\\N".join(line.replace("\\", "／").replace("{", "｛").replace("}", "｝") for line in lines)
        actual_text_w = math.ceil(
            max((c.measure(line) for line in lines), default=0)
        )
    required_text_h = num_lines * c.line_h
    glyph_bounds = None
    if c.cover_style == "localized_blur":
        ink = []
        for index, line in enumerate(lines):
            line = line.replace("\\", "／").replace("{", "｛").replace("}", "｝")
            left, top, right, bottom = c.measure.glyph_bounds(line)
            sx = scale_x / 100.0 if index == 1 else 1.0
            offset_y = index * c.measure.line_height
            ink.append((left * sx, top + offset_y, right * sx, bottom + offset_y))
        if ink:
            glyph_bounds = (min(b[0] for b in ink), min(b[1] for b in ink),
                            max(b[2] for b in ink), max(b[3] for b in ink))
    return _AssText(num_lines, formatted_text, actual_text_w, required_text_h, glyph_bounds)


def _record_text_blur(c, times, metrics, center_x, top_y):
    if c.cover_style != "localized_blur" or metrics.glyph_bounds is None:
        return
    left, top, right, bottom = metrics.glyph_bounds
    # Outline, shadow and antialiasing, before configured blur padding.
    left = max(0., center_x + left - 3)
    right = min(c.canvas_x, center_x + right + 4)
    top = max(0., top_y + top - 3)
    bottom = min(c.canvas_y, top_y + bottom + 4)
    if right <= left or bottom <= top:
        return

    def seconds(raw):
        hours, minutes, seconds = raw.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    c.blur_text_boxes.append(dict(
        start=seconds(times[0]), end=seconds(times[1]),
        x_pct=left / c.canvas_x, max_x_pct=right / c.canvas_x,
        y_pct=top / c.canvas_y, max_y_pct=bottom / c.canvas_y))


def _source_ass_sticker(
    c,
    band_layout,
    times,
    bounds,
    metrics,
    is_near_bottom,
    bg_override=None,
    fg_override=None,
    render_text=True,
):
    ass_content = ""
    start_str, end_str = times
    raw_y_pct, raw_max_y_pct, source_left_pct, source_right_pct = bounds
    chinese_center_x = c.canvas_x / 2.0
    raw_cy_pct = (raw_y_pct + raw_max_y_pct) * 0.5
    raw_h = int((raw_max_y_pct - raw_y_pct) * c.canvas_y)
    is_in_locked_band = (
        abs(raw_cy_pct - band_layout.med_center_pct) <= 0.015
        and band_layout.locked_band_min_y is not None
        and band_layout.locked_band_max_y is not None
    )
    pad_x = max(10, int(c.canvas_x * 0.015))
    pad_y = max(4, int(c.canvas_y * 0.005))

    if is_in_locked_band:
        chinese_center_y = band_layout.locked_center_y
        src_top = max(0, min(int(band_layout.locked_band_min_y * c.canvas_y), int(raw_y_pct * c.canvas_y)) - pad_y)
        src_bottom = min(c.canvas_y, max(math.ceil(band_layout.locked_band_max_y * c.canvas_y), math.ceil(raw_max_y_pct * c.canvas_y)) + pad_y)
    else:
        chinese_center_y = round(raw_cy_pct * c.canvas_y)
        src_top = max(0, int(raw_y_pct * c.canvas_y) - pad_y)
        src_bottom = min(c.canvas_y, math.ceil(raw_max_y_pct * c.canvas_y) + pad_y)

    edge_source = (source_left_pct == 0 or source_right_pct == 1
                   or raw_y_pct == 0 or raw_max_y_pct == 1)

    source_box_x1 = max(0, math.floor(source_left_pct * c.canvas_x) - pad_x)
    source_box_x2 = min(c.canvas_x, math.ceil(source_right_pct * c.canvas_x) + pad_x)
    source_box_w = max(0, source_box_x2 - source_box_x1)
    source_box_h = max(0, src_bottom - src_top)

    text_cover_w = metrics.actual_text_w + (c.sticker_padding_x * 2)

    # Subtitle stickers must remain horizontally symmetric and centered.
    # Calculate required width symmetrically from screen center:
    if source_box_w > 0:
        dist_left = max(0.0, chinese_center_x - source_box_x1)
        dist_right = max(0.0, source_box_x2 - chinese_center_x)
        source_symmetric_w = 2.0 * max(dist_left, dist_right)
        target_visible_w = max(text_cover_w, source_symmetric_w)
    else:
        source_symmetric_w = 0.0
        target_visible_w = text_cover_w

    min_margin = math.ceil(c.canvas_x * 0.05)
    # Span full-width ribbon ONLY when the source OCR box itself touches screen edges
    # or spans >= 90% of canvas width; wide Vietnamese translations remain rounded cards.
    if edge_source or (source_box_w > 0 and source_symmetric_w >= (c.canvas_x - (min_margin * 2))):
        draw_w = c.canvas_x
        draw_x = 0
    else:
        draw_w = max(4, target_visible_w - (c.outline * 2))
        draw_w = min(c.max_allowed_w, 2 * math.ceil(draw_w / 2.0))
        draw_x = round(chinese_center_x - (draw_w / 2.0))
        draw_x = max(min_margin, min(draw_x, c.canvas_x - min_margin - draw_w))

    if is_near_bottom and c.watermark_box is not None and draw_x + draw_w > c.watermark_x_limit:
        draw_x = max(min_margin, c.watermark_x_limit - draw_w)

    if metrics.num_lines == 1:
        text_box_h = metrics.required_text_h + (18 if c.is_landscape else 20)
    else:
        text_box_h = metrics.required_text_h + (20 if c.is_landscape else 24)

    target_visible_h = max(text_box_h, source_box_h)
    draw_h = max(4, target_visible_h - (c.outline * 2))
    draw_y = chinese_center_y - (draw_h // 2)

    if source_box_h > 0:
        if draw_y > src_top:
            draw_h += (draw_y - src_top)
            draw_y = src_top
        if draw_y + draw_h < src_bottom:
            draw_h = src_bottom - draw_y

    draw_x = max(0, draw_x)
    draw_y = max(0, min(draw_y, c.canvas_y - draw_h - (c.outline * 2)))

    bg_color_tag = f"\\1c{bg_override}\\3c{bg_override}" if bg_override else ""
    fg_color_tag = f"\\1c{fg_override}" if fg_override else ""
    box_radius = 0 if (edge_source or (draw_x == 0 and draw_w == c.canvas_x)) else 12
    draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h, radius=box_radius) + "{\\p0}"
    bg_line = f"{{\\an7\\pos({draw_x:g},{draw_y}){bg_color_tag}}}{draw_cmd}"
    if c.cover_style != "localized_blur":
        ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

    text_cx = max(min_margin + metrics.actual_text_w / 2.0, min(
        draw_x + (draw_w / 2.0), c.canvas_x - min_margin - metrics.actual_text_w / 2.0))
    text_cy = draw_y + max(0, (draw_h - metrics.required_text_h) // 2)
    if render_text:
        _record_text_blur(c, times, metrics, text_cx, text_cy)
        text_line = f"{{\\an8\\pos({text_cx:g},{text_cy}){fg_color_tag}}}{metrics.formatted_text}"
        ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"
    return ass_content


def _fallback_ass_sticker(c, band_layout, times, metrics, verified, seg_is_visual):
    ass_content = ""
    start_str, end_str = times
    inner_margin = c.min_margin + 12
    max_cover_w = c.canvas_x - 2 * inner_margin
    text_cover_w = metrics.actual_text_w + (c.sticker_padding_x * 2)
    draw_w = min(max_cover_w, 2 * math.ceil(text_cover_w / 2))
    draw_w = max(4, draw_w)
    draw_x = round((c.canvas_x - draw_w) / 2.0)
    draw_x = max(inner_margin, min(draw_x, c.canvas_x - inner_margin - draw_w))

    seg_padding_y = c.sticker_padding_y
    draw_h = max(4, metrics.required_text_h + (seg_padding_y * 2))

    if band_layout.main_track_centers:
        chinese_center_y = band_layout.locked_center_y
        draw_y = chinese_center_y - (draw_h // 2)
    else:
        desired_y = int(c.main_y_pct * c.canvas_y)
        draw_y = desired_y - seg_padding_y

    draw_y = max(c.min_margin, min(draw_y, c.canvas_y - c.min_margin - draw_h))
    text_cy = draw_y + max(0, (draw_h - metrics.required_text_h) // 2)
    text_cx = draw_x + (draw_w / 2.0)

    draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h, radius=12) + "{\\p0}"
    bg_line = f"{{\\an7\\pos({draw_x:g},{draw_y})}}{draw_cmd}"
    if c.cover_style != "localized_blur" and not verified:
        ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

    text_line = f"{{\\an8\\pos({text_cx:g},{text_cy})}}{metrics.formatted_text}"
    _record_text_blur(c, times, metrics, text_cx, text_cy)
    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"
    return ass_content


def _render_ass_event(event, text, c, band_layout, verified, seg_is_visual, render_text=True):
    ev_start = timedelta(seconds=event["start"])
    ev_end = timedelta(seconds=event["end"])
    start_str = _format_ass_time(ev_start)
    end_str = _format_ass_time(ev_end)
    if start_str == end_str:
        return ""

    b = event["block"]
    if b and not _excluded_source(b):
        raw_y_pct = float(_block_value(b, "y_pct", 0.0))
        raw_max_y_pct = float(_block_value(b, "max_y_pct", 0.0))
        source_left_pct = float(_block_value(b, "x_pct", 0.0))
        source_right_pct = float(_block_value(b, "max_x_pct", 0.0))
        has_source = (
            0.0 <= source_left_pct < source_right_pct <= 1.0
            and 0.0 <= raw_y_pct < raw_max_y_pct <= 1.0
        )
    else:
        has_source = False
        raw_y_pct = None
        raw_max_y_pct = None
        source_left_pct = None
        source_right_pct = None

    is_near_bottom = False
    if c.is_landscape:
        if has_source:
            raw_cy_pct = (raw_y_pct + raw_max_y_pct) * 0.5
            is_near_bottom = (
                raw_cy_pct >= 0.80
                or (abs(raw_cy_pct - band_layout.med_center_pct) <= 0.04 and (band_layout.locked_center_y / max(1, c.canvas_y)) >= 0.80)
            )
        else:
            is_near_bottom = c.main_y_pct >= 0.80

    metrics = _format_ass_event_text(text, is_near_bottom, c) if render_text else _AssText(0, "", 0, 0)
    if has_source:
        bg_override = None
        fg_override = None
        if c.cover_style == "adaptive_box" and b:
            raw_bg = _block_value(b, "bg_color_ass")
            raw_fg = _block_value(b, "text_contrast_color_ass")
            if raw_bg and re.fullmatch(r"&H[0-9A-Fa-f]{8}", str(raw_bg).strip()):
                bg_override = str(raw_bg).strip()
            if raw_fg and re.fullmatch(r"&H[0-9A-Fa-f]{8}", str(raw_fg).strip()):
                fg_override = str(raw_fg).strip()
        return _source_ass_sticker(
            c,
            band_layout,
            (start_str, end_str),
            (raw_y_pct, raw_max_y_pct, source_left_pct, source_right_pct),
            metrics,
            is_near_bottom,
            bg_override=bg_override,
            fg_override=fg_override,
            render_text=render_text,
        )
    return _fallback_ass_sticker(c, band_layout, (start_str, end_str), metrics, verified, seg_is_visual)


def _render_ass_dialogue(processed_segments, held_tracks, c, band_layout):
    ass_content = ""
    source_events = []
    for seg in processed_segments:
        start_time = seg.start
        end_time = seg.end
        text = str(seg.content).replace("\n", " ").strip()
        if not text:
            continue

        sub_events = []
        seg_s, seg_e = start_time.total_seconds(), end_time.total_seconds()
        if seg_e <= seg_s:
            continue

        sub_events, verified, seg_is_visual = _ass_segment_events(seg, held_tracks, seg_s, seg_e, c)
        for event in sub_events:
            ass_content += _render_ass_event(event, text, c, band_layout, verified, seg_is_visual)
            if event["block"]:
                source_events.append(event)
    # A verified caption can begin before speech or remain after it. Render
    # only the portions not already covered by a dialogue event, without text.
    for track in held_tracks:
        if not track.get("geometry_verified"):
            continue
        covered = []
        for event in source_events:
            block = event["block"]
            if all(float(_block_value(block, key, 0.0)) <= track[key] + 1e-6
                   for key in ("x_pct", "y_pct")) and all(
                    float(_block_value(block, key, 0.0)) >= track[key] - 1e-6
                    for key in ("max_x_pct", "max_y_pct")):
                covered.append((event["start"], event["end"]))
        cursor, end = max(0.0, track["start"]), track["end"]
        for left, right in sorted(covered) + [(end, end)]:
            if right <= cursor:
                continue
            if left > cursor:
                event = {"start": cursor, "end": min(left, end), "block": track}
                ass_content += _render_ass_event(event, "", c, band_layout, True, True, render_text=False)
                source_events.append(event)
            cursor = max(cursor, right)
            if cursor >= end:
                break
    return ass_content
