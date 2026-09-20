import codecs
import math
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
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _excluded_source(block):
    return (_block_value(block, "is_subtitle") is False
            or _block_value(block, "is_packaging") is True
            or _block_value(block, "is_static") is True
            or _block_value(block, "in_subtitle_band") is False
            or str(_block_value(block, "type", "")).lower() in
            ("packaging", "background", "logo", "watermark"))


def _transition_cover_blocks(blocks):
    """Cover sampling uncertainty at contiguous, nearby caption transitions."""
    keys = ('start', 'end', 'x_pct', 'max_x_pct', 'y_pct', 'max_y_pct')
    originals = [{key: _block_value(b, key, 0.) for key in keys} for b in blocks]
    result = [dict(b) for b in originals]
    for i, a in enumerate(originals[:-1]):
        b = originals[i+1]
        if (abs(b['start']-a['end']) > .02
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
                nearby = (abs((low + high) - (values['y_pct'] + values['max_y_pct'])) <= .04
                          and max(high, values['max_y_pct']) - min(low, values['y_pct']) <= .10
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
        following = [b['start'] for b in blocks if b['start'] > block['start'] + .0001]
        end = block['end'] + 1.0
        if following:
            end = max(block['end'], min(end, min(following)))
        if video_duration is not None:
            end = min(end, video_duration)
        held.append(dict(block, end=end))
    return held


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
):
    """Generate an ASS subtitle file.
    
    When source subtitles are selected, uses full X-Y bounding boxes to cover them.
    If no source subtitles are present (has_source is False), no cover box is drawn
    (no default box in the middle of the screen).
    Supports both landscape (horizontal) and portrait (vertical) videos.
    """
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

    sticker_padding_x = 8
    sticker_padding_y = 6
    min_margin = math.ceil(canvas_x * 0.05)
    max_allowed_w = canvas_x - 2 * min_margin
    text_max_w = max_allowed_w - (outline * 2) - (sticker_padding_x * 2)

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
                lines = wrap_subtitle_text(text, max_w, measure)
                if len(lines) > 1 and len(lines[-1].split()) < 3:
                    previous, tail = lines[-2].split(), lines[-1].split()
                    while len(tail) < 3 and len(previous) > 3:
                        candidate = [previous[-1]] + tail
                        if measure(" ".join(candidate)) > max_w:
                            break
                        tail = candidate
                        previous.pop()
                    lines[-2:] = [" ".join(previous), " ".join(tail)]
                return lines
            except Exception:
                pass
        chars = max(1, int(max_w / (font_size * 0.55)))
        return textwrap.wrap(text, width=chars) or [text]

    text_outline = 2

    ass_content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {canvas_x}
PlayResY: {canvas_y}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BgStyle,Arial,{font_size},&H00F0F0F0,&H00F0F0F0,&H00F0F0F0,&H00F0F0F0,0,0,0,0,100,100,0,0,1,{outline},0,5,0,0,0,1
Style: TextStyle,{font_name},{font_size},{font_color},&H000000FF,&H00FFFFFF,&H00000000,{bold},0,0,0,100,100,0,0,1,{text_outline},0,5,10,10,10,1

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
        groups = []
        held_tracks = _held_tracking_blocks(dialogue_segments, video_duration)
        for seg in dialogue_segments:
            source_id = getattr(seg, "source_segment_id", None) or seg.index
            if not groups or groups[-1][0] != source_id:
                groups.append((source_id, []))
            groups[-1][1].append(seg)
        for _, pages in groups:
            tracks = [b for page in pages for b in
                      (getattr(page, "tracking_blocks", None) or [])]
            if not tracks:
                for page in pages:
                    best = getattr(page, "best_block", None)
                    if best and not _excluded_source(best):
                        tracks.append(best)
            if tracks:
                first = min(float(_block_value(b, "start", 0)) for b in tracks)
                last = max(float(_block_value(b, "end", 0)) for b in tracks)
                if pages[0].start.total_seconds() - 0.30001 <= first:
                    pages[0].start = timedelta(seconds=max(0, min(
                        pages[0].start.total_seconds(), first)))
                held_end = max((b['end'] for b in held_tracks
                    if any(abs(b['start'] - float(_block_value(t, 'start', 0))) < .0001
                           for t in tracks)), default=last + 1.0)
                pages[-1].end = timedelta(seconds=held_end)
        for i in range(len(groups) - 1):
            boundary = groups[i + 1][1][0].start
            for page in groups[i][1]:
                gap = (boundary - page.end).total_seconds()
                if 0.0 < gap <= 0.5:
                    page.end = boundary
                else:
                    page.end = min(page.end, boundary)
        dialogue_segments = [s for s in dialogue_segments if s.end > s.start]
        if clean_incomplete_segment_stops is not None:
            dialogue_segments = clean_incomplete_segment_stops(dialogue_segments)
        processed_segments = []
        if split_subtitle_sentences is not None and split_segment_text is not None:
            try:
                for seg in dialogue_segments:
                    parts = []
                    for sentence in split_subtitle_sentences(seg.content):
                        lines = _wrap_text(sentence, text_max_w)
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
                                if len(_wrap_text(a, text_max_w)) <= 2 and len(_wrap_text(b, text_max_w)) <= 2:
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
                    if cy >= 0.45:
                        main_track_centers.append(cy)
                        main_track_heights.append(y2 - y1)

        if main_track_centers:
            sorted_centers = sorted(main_track_centers)
            sorted_heights = sorted(main_track_heights)
            med_center_pct = sorted_centers[len(sorted_centers) // 2]
            med_h_pct = sorted_heights[len(sorted_heights) // 2]
            locked_center_y = int(med_center_pct * canvas_y)
            locked_track_h = max(line_h, int(med_h_pct * canvas_y))
        else:
            med_center_pct = main_y_pct + (line_h / (2.0 * max(1, canvas_y)))
            locked_center_y = int(main_y_pct * canvas_y) + (line_h // 2)
            locked_track_h = line_h

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

            blocks = sorted(getattr(seg, "tracking_blocks", None) or [],
                            key=lambda b: _block_value(b, "start", 0.0))
            blocks = [b for b in blocks if not _excluded_source(b)]
            relevant = [b for b in held_tracks if b['end'] > seg_s and b['start'] < seg_e]
            if relevant:
                # Concurrent source tracks must remain covered even when the
                # translated text has already switched to the next page.
                boundaries = sorted({seg_s, seg_e} | {
                    max(seg_s, min(seg_e, b[k])) for b in relevant for k in ('start', 'end')})
                blocks = []
                for left, right in zip(boundaries, boundaries[1:]):
                    active = [b for b in relevant if b['start'] < right and b['end'] > left]
                    if active:
                        blocks.append(dict(start=left, end=right,
                            x_pct=min(b['x_pct'] for b in active),
                            max_x_pct=max(b['max_x_pct'] for b in active),
                            y_pct=min(b['y_pct'] for b in active),
                            max_y_pct=max(b['max_y_pct'] for b in active)))
            blocks = _transition_cover_blocks(blocks)
            cursor = seg_s
            for b in blocks:
                left = max(seg_s, float(_block_value(b, "start", seg_s)))
                right = min(seg_e, float(_block_value(b, "end", seg_e)))
                if right <= left or right <= cursor:
                    continue
                # A remaining gap is longer than the one-second visual hold.
                left = max(left, cursor)
                sub_events.append({"start": left, "end": right, "block": b})
                cursor = right
            if cursor < seg_e:
                last_b = blocks[-1] if blocks else getattr(seg, "best_block", None)
                sub_events.append({
                    "start": cursor,
                    "end": seg_e,
                    "block": last_b if not _excluded_source(last_b) else None,
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

                lines = _wrap_text(text, text_max_w)
                formatted_text = "\\N".join(line.replace("\\", "／").replace("{", "｛").replace("}", "｝") for line in lines)
                num_lines = len(lines)
                actual_text_w = math.ceil(
                    max((measure(line) for line in lines), default=0)
                )
                required_text_h = num_lines * line_h

                if has_source:
                    chinese_center_x = canvas_x // 2
                    raw_cy_pct = (raw_y_pct + raw_max_y_pct) * 0.5
                    raw_h = int((raw_max_y_pct - raw_y_pct) * canvas_y)
                    if abs(raw_cy_pct - med_center_pct) <= 0.04:
                        chinese_center_y = locked_center_y
                        chinese_h = max(raw_h, locked_track_h)
                    else:
                        chinese_center_y = int(raw_cy_pct * canvas_y)
                        chinese_h = raw_h

                    min_cover_w = math.ceil(2 * max(
                        chinese_center_x - source_left_pct * canvas_x,
                        source_right_pct * canvas_x - chinese_center_x,
                    )) + (sticker_padding_x * 2)
                    min_cover_h = chinese_h + (sticker_padding_y * 2)

                    text_cover_w = actual_text_w + (sticker_padding_x * 2)
                    text_cover_h = required_text_h + (sticker_padding_y * 2)

                    max_source_cover_w = max_allowed_w
                    target_visible_w = min(
                        max(min_cover_w, text_cover_w), max_source_cover_w
                    )
                    target_visible_h = min(canvas_y, max(min_cover_h, text_cover_h))

                    draw_w = max(4, target_visible_w - (outline * 2))
                    draw_w = min(max_source_cover_w, 2 * math.ceil(draw_w / 2))
                    draw_h = max(4, target_visible_h - (outline * 2))

                    draw_x = chinese_center_x - draw_w / 2
                    draw_y = chinese_center_y - (draw_h // 2)

                    min_margin = math.ceil(canvas_x * 0.05)
                    if draw_x < min_margin:
                        draw_x = min_margin
                    if draw_x + draw_w > canvas_x - min_margin:
                        draw_x = max(min_margin, canvas_x - min_margin - draw_w)

                    draw_y = max(0, min(draw_y, canvas_y - draw_h - (outline * 2)))

                    draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h) + "{\\p0}"
                    bg_line = f"{{\\an7\\pos({draw_x:g},{draw_y})}}{draw_cmd}"
                    ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

                    text_cx = chinese_center_x
                    text_cy = draw_y + max(0, (draw_h - required_text_h) // 2)
                    text_line = f"{{\\an8\\pos({text_cx:g},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"
                else:
                    text_cx = canvas_x // 2
                    text_cy = max(0, min(int(main_y_pct * canvas_y), canvas_y - required_text_h))
                    text_line = f"{{\\an8\\pos({text_cx:g},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"

    with codecs.open(output_path, "w", "utf-8-sig") as f:
        f.write(ass_content)
