import codecs
import math
import os
import re
import textwrap
import copy
from datetime import timedelta

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


def _transition_cover_blocks(blocks):
    """Cover sampling uncertainty at contiguous, nearby caption transitions."""
    keys = ('start', 'end', 'x_pct', 'max_x_pct', 'y_pct', 'max_y_pct')
    originals = [dict({key: _block_value(b, key, 0.) for key in keys},
                      geometry_verified=True) if _block_value(b, "geometry_verified", False)
                 else {key: _block_value(b, key, 0.) for key in keys} for b in blocks]
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
                    unique[values] = dict(zip(keys, values), geometry_verified=True)
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
            values = {k: float(_block_value(block, k, 0)) for k in keys}
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
                unique[values] = dict(zip(keys, values))
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

    return translated_segments


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

    text_outline = 0
    cover_style = str(os.getenv("V1_SUBTITLE_COVER_STYLE", "solid_box")).strip().lower()
    bg_color_ass = "&H50FFFFFF" if cover_style == "frosted_blur" else "&H00FFFFFF"

    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_x}
PlayResY: {canvas_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BgStyle,Arial,{font_size},{bg_color_ass},{bg_color_ass},{bg_color_ass},{bg_color_ass},0,0,0,0,100,100,0,0,1,{outline},0,5,0,0,0,1
Style: TextStyle,{font_name},{font_size},{font_color},&H000000FF,&H00000000,&H00000000,{bold},0,0,0,100,100,0,0,1,{text_outline},0,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def format_time(td):
        ticks = max(0, round(td.total_seconds() * 100))
        total_seconds, centiseconds = divmod(ticks, 100)
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    if dialogue_segments:
        # Align only outer translated-page boundaries to the source's visual
        # lifetime. Keep internal page timing and all audio timing untouched.
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
                tracks = [b for page in pages for b in
                          (getattr(page, "tracking_blocks", None) or []) if not _excluded_source(b)]
                if tracks:
                    last = max(float(_block_value(b, "end", pages[-1].end.total_seconds())) for b in tracks)
                    if last > pages[-1].end.total_seconds():
                        max_allowed_ext = last if video_duration is None else min(last, video_duration)
                        pages[-1].end = timedelta(seconds=max_allowed_ext)
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
                    gap = (boundary - page.end).total_seconds()
                    if 0.0 < gap <= 0.5:
                        page.end = boundary
                    else:
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
                gap = (transition_target - page.end).total_seconds()
                if 0.0 < gap <= 0.5:
                    page.end = transition_target
                else:
                    page.end = min(page.end, transition_target)
        dialogue_segments = [s for s in dialogue_segments if s.end > s.start]
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
                        if blist else float(getattr(seg, "y_pct", main_y_pct) or main_y_pct)
                    )
                    effective_split_w = safe_bottom_text_max_w if (is_landscape and seg_y_val >= 0.80) else text_max_w
                    parts = []
                    for sentence in split_subtitle_sentences(seg.content):
                        lines = _wrap_text(sentence, effective_split_w)
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
                                if len(_wrap_text(a, effective_split_w)) <= 2 and len(_wrap_text(b, effective_split_w)) <= 2:
                                    choices.append((abs(cut - len(words) / 2), a, b))
                            if choices:
                                _, a, b = min(choices)
                                sentence_parts[-2:] = [a, b]
                        parts.extend(sentence_parts)
                    processed_segments.extend(split_segment_text(seg, parts))
            except Exception:
                processed_segments = list(dialogue_segments)
        else:
            processed_segments = list(dialogue_segments)

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
            locked_center_y = int(med_center_pct * canvas_y)
            locked_track_h = max(line_h, int(med_h_pct * canvas_y))
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
            max_band_h = max((line_h * 2.2) / max(1, canvas_y), med_h_pct * 1.35)
            if (locked_band_max_y - locked_band_min_y) > max_band_h:
                half_h = max_band_h * 0.5
                locked_band_min_y = max(0.0, med_center_pct - half_h)
                locked_band_max_y = min(1.0, med_center_pct + half_h)
        else:
            med_center_pct = main_y_pct + (line_h / (2.0 * max(1, canvas_y)))
            locked_center_y = int(main_y_pct * canvas_y) + (line_h // 2)
            locked_track_h = line_h
            locked_band_min_y = None
            locked_band_max_y = None

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

            # Determine seg's own vertical anchor region so we never merge unrelated subtitles/titles
            # from completely different parts of the screen (e.g. top title at y~0.25 vs bottom speech at y~0.85).
            seg_blist = [b for b in (getattr(seg, "tracking_blocks", None) or []) if not _excluded_source(b)]
            if not seg_blist and getattr(seg, "best_block", None):
                bb = getattr(seg, "best_block")
                if not _excluded_source(bb):
                    seg_blist = [bb]

            if seg_blist:
                seg_cy = sum((float(_block_value(b, "y_pct", 0.0)) + float(_block_value(b, "max_y_pct", 0.0))) * 0.5 for b in seg_blist) / len(seg_blist)
                seg_top = min(float(_block_value(b, "y_pct", 0.0)) for b in seg_blist)
                seg_bottom = max(float(_block_value(b, "max_y_pct", 0.0)) for b in seg_blist)
            else:
                seg_cy = float(getattr(seg, "y_pct", main_y_pct) or main_y_pct)
                seg_top = seg_cy - 0.03
                seg_bottom = seg_cy + 0.03

            blocks = getattr(seg, "tracking_blocks", None) or []
            if not blocks and getattr(seg, "best_block", None):
                bb = getattr(seg, "best_block")
                if not _excluded_source(bb):
                    blocks = [dict(
                        start=float(_block_value(bb, "start", seg_s)),
                        end=float(_block_value(bb, "end", seg_e)),
                        x_pct=float(_block_value(bb, "x_pct", 0.0)),
                        max_x_pct=float(_block_value(bb, "max_x_pct", 1.0)),
                        y_pct=float(_block_value(bb, "y_pct", main_y_pct)),
                        max_y_pct=float(_block_value(bb, "max_y_pct", main_y_pct + 0.05)),
                    )]
            blocks = sorted(blocks, key=lambda b: _block_value(b, "start", 0.0))
            blocks = [b for b in blocks if not _excluded_source(b)]
            verified = _verified_geometry(seg)
            raw_relevant = [b for b in held_tracks if b['end'] > seg_s and b['start'] < seg_e]
            relevant = []
            for b in raw_relevant:
                b_cy = (float(_block_value(b, "y_pct", 0.0)) + float(_block_value(b, "max_y_pct", 0.0))) * 0.5
                b_span = max(float(_block_value(b, "max_y_pct", 0.0)), seg_bottom) - min(float(_block_value(b, "y_pct", 0.0)), seg_top)
                # Only keep blocks within reasonable vertical proximity (<= 12% center delta and combined span <= 16%)
                if abs(b_cy - seg_cy) <= 0.12 and b_span <= 0.16:
                    relevant.append(b)
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
                        # retain only the ones closest to seg_cy to prevent monster bounding boxes.
                        span_active = max(float(_block_value(b, "max_y_pct", 0.0)) for b in active) - min(float(_block_value(b, "y_pct", 0.0)) for b in active)
                        if span_active > 0.16:
                            active = [min(active, key=lambda b: abs((float(_block_value(b, "y_pct", 0.0)) + float(_block_value(b, "max_y_pct", 0.0))) * 0.5 - seg_cy))]
                        blocks.append(dict(start=left, end=right,
                            x_pct=min(float(_block_value(b, 'x_pct', 0.0)) for b in active),
                            max_x_pct=max(float(_block_value(b, 'max_x_pct', 0.0)) for b in active),
                            y_pct=min(float(_block_value(b, 'y_pct', 0.0)) for b in active),
                            max_y_pct=max(float(_block_value(b, 'max_y_pct', 0.0)) for b in active),
                            **({'geometry_verified': True} if verified or all(_block_value(b, 'geometry_verified', False) for b in active) else {})))
            blocks = _transition_cover_blocks(blocks)
            if not verified and blocks and seg_s < float(_block_value(blocks[0], "start", seg_s)):
                first_gap = float(_block_value(blocks[0], "start", seg_s)) - seg_s
                if first_gap <= 1.0:
                    if isinstance(blocks[0], dict):
                        blocks[0]["start"] = seg_s
                    else:
                        setattr(blocks[0], "start", seg_s)
            cursor = seg_s
            for b in blocks:
                left = max(seg_s, float(_block_value(b, "start", seg_s)))
                right = min(seg_e, float(_block_value(b, "end", seg_e)))
                if right <= left or right <= cursor:
                    continue
                if left > cursor:
                    sub_events.append({"start": cursor, "end": left, "block": None})
                sub_events.append({"start": left, "end": right, "block": b})
                cursor = right
            if cursor < seg_e:
                sub_events.append({
                    "start": cursor,
                    "end": seg_e,
                    "block": None,
                })

            for event in sub_events:
                ev_start = timedelta(seconds=event["start"])
                ev_end = timedelta(seconds=event["end"])
                start_str = format_time(ev_start)
                end_str = format_time(ev_end)
                if start_str == end_str:
                    continue

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
                if is_landscape:
                    if has_source:
                        raw_cy_pct = (raw_y_pct + raw_max_y_pct) * 0.5
                        is_near_bottom = (
                            raw_cy_pct >= 0.80
                            or (abs(raw_cy_pct - med_center_pct) <= 0.04 and (locked_center_y / max(1, canvas_y)) >= 0.80)
                        )
                    else:
                        is_near_bottom = main_y_pct >= 0.80

                effective_text_w = safe_bottom_text_max_w if is_near_bottom else text_max_w
                lines = _wrap_text(text, effective_text_w)
                if len(lines) > 2:
                    # Enforce strictly maximum 2 lines per subtitle event
                    lines = [lines[0], " ".join(lines[1:])]
                num_lines = len(lines)

                scale_x = 100
                if num_lines == 2 and measure(lines[1]) > max_allowed_w:
                    scale_x = max(70, min(100, int((max_allowed_w / measure(lines[1])) * 100)))
                    escaped_l0 = lines[0].replace("\\", "／").replace("{", "｛").replace("}", "｝")
                    escaped_l1 = lines[1].replace("\\", "／").replace("{", "｛").replace("}", "｝")
                    formatted_text = f"{escaped_l0}\\N{{\\fscx{scale_x}}}{escaped_l1}"
                    actual_text_w = math.ceil(max(measure(lines[0]), measure(lines[1]) * (scale_x / 100.0)))
                else:
                    formatted_text = "\\N".join(line.replace("\\", "／").replace("{", "｛").replace("}", "｝") for line in lines)
                    actual_text_w = math.ceil(
                        max((measure(line) for line in lines), default=0)
                    )
                required_text_h = num_lines * line_h

                if has_source:
                    chinese_center_x = canvas_x / 2.0
                    raw_cy_pct = (raw_y_pct + raw_max_y_pct) * 0.5
                    raw_h = int((raw_max_y_pct - raw_y_pct) * canvas_y)
                    is_in_locked_band = (
                        abs(raw_cy_pct - med_center_pct) <= 0.015
                        and locked_band_min_y is not None
                        and locked_band_max_y is not None
                    )
                    if is_in_locked_band:
                        chinese_center_y = locked_center_y
                        src_top = max(0, min(int(locked_band_min_y * canvas_y), int(raw_y_pct * canvas_y)))
                        src_bottom = min(canvas_y, max(math.ceil(locked_band_max_y * canvas_y), math.ceil(raw_max_y_pct * canvas_y)))
                    else:
                        chinese_center_y = int(raw_cy_pct * canvas_y)
                        src_top = max(0, int(raw_y_pct * canvas_y))
                        src_bottom = min(canvas_y, math.ceil(raw_max_y_pct * canvas_y))

                    edge_source = (source_left_pct == 0 or source_right_pct == 1
                                   or raw_y_pct == 0 or raw_max_y_pct == 1)

                    source_box_x1 = max(0, math.floor(source_left_pct * canvas_x))
                    source_box_x2 = min(canvas_x, math.ceil(source_right_pct * canvas_x))
                    source_box_w = max(0, source_box_x2 - source_box_x1)
                    source_box_h = max(0, src_bottom - src_top)

                    text_cover_w = actual_text_w + (sticker_padding_x * 2)

                    # Subtitle stickers must remain horizontally symmetric and centered.
                    # Calculate required width symmetrically from screen center:
                    if source_box_w > 0:
                        dist_left = max(0.0, chinese_center_x - source_box_x1)
                        dist_right = max(0.0, source_box_x2 - chinese_center_x)
                        source_symmetric_w = 2.0 * max(dist_left, dist_right)
                        target_visible_w = max(text_cover_w, source_symmetric_w)
                    else:
                        target_visible_w = text_cover_w

                    min_margin = math.ceil(canvas_x * 0.05)
                    # When text/source reaches screen borders or covers > 95% of canvas,
                    # span cleanly as a full-width ribbon across the screen.
                    if edge_source or target_visible_w >= (canvas_x - (min_margin * 2)):
                        draw_w = canvas_x
                        draw_x = 0
                    else:
                        draw_w = max(4, target_visible_w - (outline * 2))
                        draw_w = min(max_allowed_w, 2 * math.ceil(draw_w / 2.0))
                        draw_x = round(chinese_center_x - (draw_w / 2.0))
                        draw_x = max(min_margin, min(draw_x, canvas_x - min_margin - draw_w))

                    if is_near_bottom and watermark_box is not None and draw_x + draw_w > watermark_x_limit:
                        draw_x = max(min_margin, watermark_x_limit - draw_w)

                    if num_lines == 1:
                        text_box_h = required_text_h + (18 if is_landscape else 20)
                    else:
                        text_box_h = required_text_h + (20 if is_landscape else 24)

                    target_visible_h = max(text_box_h, source_box_h)
                    draw_h = max(4, target_visible_h - (outline * 2))
                    draw_y = chinese_center_y - (draw_h // 2)

                    if source_box_h > 0:
                        if draw_y > src_top:
                            draw_h += (draw_y - src_top)
                            draw_y = src_top
                        if draw_y + draw_h < src_bottom:
                            draw_h = src_bottom - draw_y

                    draw_x = max(0, draw_x)
                    draw_y = max(0, min(draw_y, canvas_y - draw_h - (outline * 2)))

                    draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h, radius=0 if (edge_source or (draw_x == 0 and draw_w == canvas_x)) else 12) + "{\\p0}"
                    bg_line = f"{{\\an7\\pos({draw_x:g},{draw_y})}}{draw_cmd}"
                    ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

                    text_cx = max(min_margin + actual_text_w / 2.0, min(
                        draw_x + (draw_w / 2.0), canvas_x - min_margin - actual_text_w / 2.0))
                    text_cy = draw_y + max(0, (draw_h - required_text_h) // 2)
                    text_line = f"{{\\an8\\pos({text_cx:g},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"
                else:
                    inner_margin = min_margin + 12
                    max_cover_w = canvas_x - 2 * inner_margin
                    text_cover_w = actual_text_w + (sticker_padding_x * 2)
                    draw_w = min(max_cover_w, 2 * math.ceil(text_cover_w / 2))
                    draw_w = max(4, draw_w)
                    draw_x = round((canvas_x - draw_w) / 2.0)
                    draw_x = max(inner_margin, min(draw_x, canvas_x - inner_margin - draw_w))

                    seg_padding_y = sticker_padding_y
                    draw_h = max(4, required_text_h + (seg_padding_y * 2))

                    if main_track_centers:
                        chinese_center_y = locked_center_y
                        draw_y = chinese_center_y - (draw_h // 2)
                    else:
                        desired_y = int(main_y_pct * canvas_y)
                        draw_y = desired_y - seg_padding_y

                    draw_y = max(min_margin, min(draw_y, canvas_y - min_margin - draw_h))
                    text_cy = draw_y + max(0, (draw_h - required_text_h) // 2)
                    text_cx = draw_x + (draw_w / 2.0)

                    if not video_has_source:
                        draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h, radius=12) + "{\\p0}"
                        bg_line = f"{{\\an7\\pos({draw_x:g},{draw_y})}}{draw_cmd}"
                        ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

                    text_line = f"{{\\an8\\pos({text_cx:g},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"


    with codecs.open(output_path, "w", "utf-8-sig") as f:
        f.write(ass_content)
