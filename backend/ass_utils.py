import codecs
import math
import re
import textwrap
import copy
from datetime import timedelta

try:
    from .subtitle_layout import subtitle_measure, wrap_subtitle_text
    from .subtitle_text import split_segment_text, split_subtitle_sentences
except ImportError:
    try:
        from subtitle_layout import subtitle_measure, wrap_subtitle_text
        from subtitle_text import split_segment_text, split_subtitle_sentences
    except ImportError:
        subtitle_measure = None
        wrap_subtitle_text = None
        split_segment_text = None
        split_subtitle_sentences = None


def _block_value(block, key, default=None):
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


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
):
    """Generate an ASS subtitle file.
    
    When source subtitles are selected, uses full X-Y bounding boxes to cover them.
    Missing OCR retains a text-sized Vietnamese card.
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
    sticker_padding_y = 16
    max_allowed_w = int(canvas_x * 0.90)
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
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        centiseconds = int(td.microseconds / 10000)
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    if dialogue_segments:
        # Align only outer translated-page boundaries to the source's visual
        # lifetime. Keep internal page timing and all audio timing untouched.
        dialogue_segments = copy.deepcopy(list(dialogue_segments))
        groups = []
        for seg in dialogue_segments:
            source_id = getattr(seg, "source_segment_id", None) or seg.index
            if not groups or groups[-1][0] != source_id:
                groups.append((source_id, []))
            groups[-1][1].append(seg)
        for _, pages in groups:
            tracks = [b for page in pages for b in
                      (getattr(page, "tracking_blocks", None) or [])]
            if tracks:
                first = min(float(_block_value(b, "start", 0)) for b in tracks)
                last = max(float(_block_value(b, "end", 0)) for b in tracks) + 1.0
                if pages[0].start.total_seconds() - 0.30001 <= first:
                    pages[0].start = timedelta(seconds=max(0, min(
                        pages[0].start.total_seconds(), first)))
                pages[-1].end = timedelta(seconds=last)
        for i in range(len(groups) - 1):
            boundary = groups[i + 1][1][0].start
            for page in groups[i][1]:
                page.end = min(page.end, boundary)
        dialogue_segments = [s for s in dialogue_segments if s.end > s.start]
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

        for seg in processed_segments:
            start_time = seg.start
            end_time = seg.end
            text = str(seg.content).replace("\n", " ").strip()
            if not text:
                continue


            sub_events = []
            seg_s, seg_e = start_time.total_seconds(), end_time.total_seconds()
            blocks = sorted(getattr(seg, "tracking_blocks", None) or [],
                            key=lambda b: _block_value(b, "start", 0.0))
            cursor = seg_s
            for b in blocks:
                left = max(seg_s, float(_block_value(b, "start", seg_s)))
                right = min(seg_e, float(_block_value(b, "end", seg_e)) + 1.0)
                # Switch directly to the next detected position, without overlap.
                following = next((float(_block_value(n, "start", seg_e))
                                  for n in blocks
                                  if float(_block_value(n, "start", seg_e)) >
                                  float(_block_value(b, "start", seg_s))), None)
                if following is not None:
                    right = min(right, following)
                if right <= left or right <= cursor:
                    continue
                # Keep both layers during the one-second disappearance grace.
                left = max(left, cursor)
                sub_events.append({"start": left, "end": right, "block": b})
                cursor = right
            if cursor < seg_e and not blocks:
                sub_events.append({"start": cursor, "end": seg_e,
                                   "block": getattr(seg, "best_block", None) if not blocks else None})

            for event in sub_events:
                ev_start = timedelta(seconds=event["start"])
                ev_end = timedelta(seconds=event["end"])
                start_str = format_time(ev_start)
                end_str = format_time(ev_end)

                b = event["block"]
                if b:
                    raw_y_pct = (
                        b.get("y_pct")
                        if isinstance(b, dict)
                        else getattr(b, "y_pct", None)
                    )
                    raw_max_y_pct = (
                        b.get("max_y_pct")
                        if isinstance(b, dict)
                        else getattr(b, "max_y_pct", None)
                    )
                    source_left_pct = (
                        b.get("x_pct")
                        if isinstance(b, dict)
                        else getattr(b, "x_pct", None)
                    )
                    source_right_pct = (
                        b.get("max_x_pct")
                        if isinstance(b, dict)
                        else getattr(b, "max_x_pct", None)
                    )
                else:
                    raw_y_pct = None
                    raw_max_y_pct = None
                    source_left_pct = None
                    source_right_pct = None

                has_source = (
                    b is not None
                    and source_left_pct is not None
                    and source_right_pct is not None
                    and raw_y_pct is not None
                    and raw_max_y_pct is not None
                    and 0.0 <= source_left_pct < source_right_pct <= 1.0
                    and 0.0 <= raw_y_pct < raw_max_y_pct <= 1.0
                )

                lines = _wrap_text(text, text_max_w)
                formatted_text = "\\N".join(line.replace("\\", "／").replace("{", "｛").replace("}", "｝") for line in lines)
                num_lines = len(lines)
                actual_text_w = math.ceil(
                    max((measure(line) for line in lines), default=0)
                )
                required_text_h = num_lines * line_h

                if has_source:
                    chinese_w = int((source_right_pct - source_left_pct) * canvas_x)
                    chinese_h = int((raw_max_y_pct - raw_y_pct) * canvas_y)
                    # Keep the card centered on the video, not on OCR's X center.
                    chinese_center_x = canvas_x // 2
                    chinese_center_y = int(
                        ((raw_y_pct + raw_max_y_pct) * 0.5) * canvas_y
                    )

                    # Cover box dimensions:
                    # Must cover at least the entire Chinese text with small padding
                    min_cover_w = math.ceil(2 * max(
                        chinese_center_x - source_left_pct * canvas_x,
                        source_right_pct * canvas_x - chinese_center_x,
                    )) + (sticker_padding_x * 2)
                    # OCR already includes an outer glyph margin. Do not add
                    # the full text padding again around a tall source box.
                    min_cover_h = chinese_h + 8

                    # Expand if translated Vietnamese text is wider or taller (supports 2 lines)
                    text_cover_w = actual_text_w + (sticker_padding_x * 2)
                    text_cover_h = required_text_h + (sticker_padding_y * 2)

                    target_visible_w = min(
                        math.ceil(max(min_cover_w, text_cover_w) * 1.10), canvas_x
                    )
                    target_visible_h = min(canvas_y, max(min_cover_h, text_cover_h))

                    # Because BgStyle has Outline=outline, ASS drawing dimensions
                    # produce a visible box of size (draw_w + outline*2, draw_h + outline*2)
                    draw_w = max(4, target_visible_w - (outline * 2))
                    # Even widths allow exact integer-pixel centering.
                    draw_w = min(canvas_x, 2 * math.ceil(draw_w / 2))
                    draw_h = max(4, target_visible_h - (outline * 2))

                    # Lock horizontal position; only Y follows the subtitle track.
                    draw_x = chinese_center_x - (draw_w // 2)
                    draw_y = chinese_center_y - (draw_h // 2)

                    # Clamp to screen margins
                    min_margin = 0
                    if draw_x < min_margin:
                        draw_x = min_margin
                    if draw_x + draw_w > canvas_x - min_margin:
                        draw_x = max(min_margin, canvas_x - min_margin - draw_w)

                    draw_y = max(0, min(draw_y, canvas_y - draw_h - (outline * 2)))

                    draw_cmd = "{\\p1}" + _rounded_box(draw_w, draw_h) + "{\\p0}"
                    bg_line = f"{{\\an7\\pos({draw_x},{draw_y})}}{draw_cmd}"
                    ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{bg_line}\n"

                    text_cx = draw_x + (draw_w // 2)
                    text_cy = draw_y + max(0, (draw_h - required_text_h) // 2)
                    text_line = f"{{\\an8\\pos({text_cx},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"
                else:
                    # Missing OCR geometry must not remove the Vietnamese card.
                    # This is a text background, not an inferred scene-text mask.
                    text_cx = canvas_x // 2
                    card_w = min(canvas_x, 2 * math.ceil((actual_text_w + 2 * sticker_padding_x) * 1.10 / 2))
                    card_h = min(canvas_y, required_text_h + 2 * sticker_padding_y)
                    card_x = (canvas_x - card_w) // 2
                    card_y = max(0, min(int(main_y_pct * canvas_y) - sticker_padding_y, canvas_y - card_h))
                    drawing = "{\\p1}" + _rounded_box(card_w, card_h) + "{\\p0}"
                    background = f"{{\\an7\\pos({card_x},{card_y})}}{drawing}"
                    ass_content += f"Dialogue: 0,{start_str},{end_str},BgStyle,,0,0,0,,{background}\n"
                    text_cy = card_y + sticker_padding_y
                    text_line = f"{{\\an8\\pos({text_cx},{text_cy})}}{formatted_text}"
                    ass_content += f"Dialogue: 1,{start_str},{end_str},TextStyle,,0,0,0,,{text_line}\n"

    with codecs.open(output_path, "w", "utf-8-sig") as f:
        f.write(ass_content)
