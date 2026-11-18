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
import struct
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


def _load_subtitle_blur():
    try:
        from subtitle_blur import (
            ass_blur_metadata,
            blur_regions,
            read_ass_blur_metadata,
            resolve_blur_settings,
        )
    except ImportError:
        from backend.subtitle_blur import (
            ass_blur_metadata,
            blur_regions,
            read_ass_blur_metadata,
            resolve_blur_settings,
        )
    return ass_blur_metadata, blur_regions, read_ass_blur_metadata, resolve_blur_settings


_VIETNAMESE_PROBE_CODEPOINTS = tuple(ord(c) for c in "ĂăÂâĐđÊêÔôƠơƯưẤấỆệỮữ")
_SYMBOL_FONT_KEYWORDS = ("symbol", "wingdings", "webdings", "dingbats", "emoji", "math")


def _load_subtitle_layout():
    try:
        from subtitle_layout import _installed_fonts, subtitle_measure
    except ImportError:
        from backend.subtitle_layout import _installed_fonts, subtitle_measure
    return subtitle_measure, _installed_fonts


def _ttf_supports_codepoints(
    ttf_path: str | Path,
    codepoints: tuple[int, ...] = _VIETNAMESE_PROBE_CODEPOINTS,
) -> bool:
    """Check TrueType/OpenType cmap (Format 12 / Format 4) for non-zero Vietnamese glyph IDs."""
    try:
        data = Path(ttf_path).read_bytes()
        offset = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        num_tables = struct.unpack_from(">H", data, offset + 4)[0]
        cmap_offset = None
        for idx in range(num_tables):
            tag, _, pos, _ = struct.unpack_from(">4sIII", data, offset + 12 + idx * 16)
            if tag == b"cmap":
                cmap_offset = pos
                break
        if cmap_offset is None:
            return False
        _, num_sub = struct.unpack_from(">HH", data, cmap_offset)
        subtables = []
        for idx in range(num_sub):
            plat_id, enc_id, rel_off = struct.unpack_from(">HHI", data, cmap_offset + 4 + idx * 8)
            if (plat_id == 3 and enc_id in (1, 10)) or plat_id == 0:
                subtables.append(cmap_offset + rel_off)
        ordered = sorted(
            subtables,
            key=lambda sub_pos: 0 if struct.unpack_from(">H", data, sub_pos)[0] == 12 else 1,
        )
        for sub_pos in ordered:
            fmt = struct.unpack_from(">H", data, sub_pos)[0]
            if fmt == 12:
                n_groups = struct.unpack_from(">I", data, sub_pos + 12)[0]

                def _has_cp12(cp: int) -> bool:
                    for g in range(n_groups):
                        s_code, e_code, s_glyph = struct.unpack_from(">III", data, sub_pos + 16 + g * 12)
                        if s_code <= cp <= e_code:
                            return (s_glyph + (cp - s_code)) != 0
                    return False

                if all(_has_cp12(cp) for cp in codepoints):
                    return True
            elif fmt == 4:
                seg_count = struct.unpack_from(">H", data, sub_pos + 6)[0] // 2
                end_pos = sub_pos + 14
                start_pos = end_pos + seg_count * 2 + 2
                delta_pos = start_pos + seg_count * 2
                range_pos = delta_pos + seg_count * 2

                def _has_cp4(cp: int) -> bool:
                    if cp > 0xFFFF:
                        return False
                    for s in range(seg_count):
                        end_c = struct.unpack_from(">H", data, end_pos + s * 2)[0]
                        if cp > end_c:
                            continue
                        start_c = struct.unpack_from(">H", data, start_pos + s * 2)[0]
                        if cp < start_c:
                            return False
                        delta = struct.unpack_from(">h", data, delta_pos + s * 2)[0]
                        ro_off = range_pos + s * 2
                        ro = struct.unpack_from(">H", data, ro_off)[0]
                        if ro == 0:
                            gid = (cp + delta) & 0xFFFF
                        else:
                            glyph_addr = ro_off + ro + (cp - start_c) * 2
                            if glyph_addr + 2 > len(data):
                                return False
                            gid = struct.unpack_from(">H", data, glyph_addr)[0]
                            if gid != 0:
                                gid = (gid + delta) & 0xFFFF
                        return gid != 0
                    return False

                if all(_has_cp4(cp) for cp in codepoints):
                    return True
        return False
    except (OSError, struct.error, ValueError):
        return False


def _extract_measure_font_path(measure_fn) -> str | None:
    target = getattr(measure_fn, "__wrapped__", measure_fn)
    for cell in getattr(target, "__closure__", None) or ():
        try:
            val = cell.cell_contents
        except ValueError:
            continue
        path_val = getattr(val, "path", None)
        if path_val:
            return str(path_val)
    return None


def _stage_extra_font_dirs(font_dirs: tuple[str | Path, ...]) -> None:
    """Expose nearby bundled fonts (e.g. ./fonts/arial*.ttf) when called standalone outside setup_env."""
    import os
    import shutil

    staged_any = False
    cwd_resolved = Path.cwd().resolve()
    for raw_dir in font_dirs:
        if not raw_dir:
            continue
        fdir = Path(raw_dir)
        if not fdir.is_dir():
            continue
        try:
            fdir_resolved = fdir.resolve()
        except OSError:
            fdir_resolved = fdir
        font_files = [
            p for p in sorted(fdir.iterdir())
            if p.is_file() and p.suffix.lower() in {".ttf", ".otf", ".ttc"}
        ]
        for font_file in font_files:
            if fdir_resolved.parent == cwd_resolved:
                cwd_target = cwd_resolved / font_file.name
                if not cwd_target.exists():
                    try:
                        os.symlink(font_file, cwd_target)
                        staged_any = True
                    except OSError:
                        try:
                            shutil.copy2(font_file, cwd_target)
                            staged_any = True
                        except OSError:
                            pass
            if os.name != "nt":
                sys_dir = Path("/usr/local/share/fonts/autodub")
                try:
                    sys_dir.mkdir(parents=True, exist_ok=True)
                    sys_target = sys_dir / font_file.name
                    if not sys_target.exists() or sys_target.stat().st_size != font_file.stat().st_size:
                        shutil.copy2(font_file, sys_target)
                        staged_any = True
                except OSError:
                    pass
    if staged_any:
        try:
            try:
                import subtitle_layout as _sl
            except ImportError:
                from backend import subtitle_layout as _sl
            for fn_name in ("_installed_fonts", "subtitle_measure"):
                fn = getattr(_sl, fn_name, None)
                clear_fn = getattr(fn, "cache_clear", None)
                if callable(clear_fn):
                    clear_fn()
        except Exception:
            pass


def resolve_cloud_subtitle_font(
    font_name: str = "Arial",
    font_size: int = 38,
    bold: bool = True,
    *,
    extra_font_dirs: tuple[str | Path, ...] | list[str | Path] = (),
) -> str:
    """Resolve an installed font family supporting glyph_bounds and Vietnamese glyphs."""
    cand_dirs = [Path("fonts"), *(Path(d) for d in (extra_font_dirs or ()) if d)]
    _stage_extra_font_dirs(tuple(cand_dirs))
    try:
        subtitle_measure, installed_fonts_fn = _load_subtitle_layout()
    except ImportError as exc:
        raise SubtitleTimingError(f"Không nạp được module đo font phụ đề: {exc}") from exc

    installed_entries = []
    try:
        installed_entries = list(installed_fonts_fn() or ())
    except Exception:
        installed_entries = []

    installed_families = {str(item[0]).casefold() for item in installed_entries if item}
    requested_name = str(font_name or "Arial").strip() or "Arial"
    preferred = [
        requested_name,
        "Arial",
        "Liberation Sans",
        "DejaVu Sans",
        "Noto Sans",
        "FreeSans",
        "Ubuntu",
    ]
    candidates: list[str] = []
    seen_cf: set[str] = set()

    def _add_candidate(raw_name: str) -> None:
        cleaned = str(raw_name or "").strip()
        if not cleaned:
            return
        cf = cleaned.casefold()
        if cf in seen_cf or any(kw in cf for kw in _SYMBOL_FONT_KEYWORDS):
            return
        seen_cf.add(cf)
        candidates.append(cleaned)

    for fam in preferred:
        _add_candidate(fam)
    for item in installed_entries:
        if item and item[0]:
            _add_candidate(str(item[0]))

    if installed_families:
        ordered_candidates = [c for c in candidates if c.casefold() in installed_families] + [
            c for c in candidates if c.casefold() not in installed_families
        ]
    else:
        ordered_candidates = candidates

    for cand in ordered_candidates:
        try:
            measure = subtitle_measure(cand, font_size, bool(bold))
            if not callable(measure) or not hasattr(measure, "glyph_bounds"):
                continue
            bx = measure.glyph_bounds("Đổi từ khóa 9,9 tệ")
            if (
                not isinstance(bx, (tuple, list))
                or len(bx) != 4
                or not all(isinstance(v, (int, float)) and math.isfinite(float(v)) for v in bx)
                or float(bx[2]) <= float(bx[0])
                or float(bx[3]) <= float(bx[1])
            ):
                continue
            resolved_family = str(getattr(measure, "font_family", cand) or cand).strip() or cand
            if any(kw in resolved_family.casefold() for kw in _SYMBOL_FONT_KEYWORDS):
                continue
            face_path = _extract_measure_font_path(measure)
            if face_path and Path(face_path).is_file():
                if not _ttf_supports_codepoints(face_path):
                    continue
            if installed_families and resolved_family.casefold() not in installed_families and cand.casefold() in installed_families:
                return cand
            return resolved_family
        except Exception:
            continue

    raise SubtitleTimingError(
        f"Không đo được font phụ đề tiếng Việt cho vùng blur (yêu cầu: {font_name})"
    )


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
            raw_px_box = [
                max(0, math.floor(x1 * width / 2) * 2),
                max(0, math.floor(y1 * height / 2) * 2),
                min(width // 2 * 2, math.ceil(x2 * width / 2) * 2),
                min(height // 2 * 2, math.ceil(y2 * height / 2) * 2),
            ]
            key = (round(start, 4), round(end, 4), *box)
            if key in seen:
                continue
            seen.add(key)
            color = str(_value(block, "bg_color_ass", "&H00FFFFFF"))
            if not re.fullmatch(r"&H00[0-9A-Fa-f]{6}", color):
                color = "&H00FFFFFF"  # A translucent rectangle cannot cover source text.
            masks.append({"name": f"cloud_mask_{len(masks) + 1}", "cue_id": str(segment["id"]),
                          "start": start, "end": end, "box": box, "color": color,
                          "_raw_px_box": raw_px_box})
    return masks


def _parse_play_res(ass_text):
    rx = re.search(r"^PlayResX:\s*(\d+)", ass_text, re.M)
    ry = re.search(r"^PlayResY:\s*(\d+)", ass_text, re.M)
    return (int(rx.group(1)) if rx else 720), (int(ry.group(1)) if ry else 1280)


def validate_cloud_ass(ass_path, expected_cues, expected_masks, *, video_duration,
                       cover_style=None):
    """Verify actual emitted events, not only input cue counts or OCR hit counts."""
    raw_ass = Path(ass_path).read_text(encoding="utf-8-sig")
    events = _events(raw_ass)
    active_style = str(cover_style or "").strip().lower()
    if not active_style:
        active_style = "localized_blur" if "; ToolV1BlurData: " in raw_ass else "solid_box"
    errors = []
    cue_audit = []
    expected_names = {cue["name"] for cue in expected_cues}
    for event in events:
        if event["end"] > video_duration + .021:
            errors.append(f"ASS event exceeds video: {event['name']}")
        if event["style"] == "TextStyle" and event["name"] not in expected_names:
            errors.append(f"Unexpected ASS text event: {event['name']}")
        if active_style == "localized_blur" and event["style"] == "CloudSourceMask":
            errors.append(f"Unexpected opaque CloudSourceMask in localized_blur ASS: {event['name']}")
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
    if active_style == "localized_blur":
        _, blur_regions, read_ass_blur_metadata, _ = _load_subtitle_blur()
        try:
            blur_meta = read_ass_blur_metadata(raw_ass)
        except Exception as exc:
            blur_meta = None
            errors.append(f"Invalid ToolV1BlurData metadata: {exc}")
        if blur_meta is None:
            errors.append("Missing ToolV1BlurData metadata in localized_blur ASS")
            regions = []
            pad_x = pad_y = 0.0
        else:
            play_w, play_h = _parse_play_res(raw_ass)
            regions = blur_regions(blur_meta["boxes"], play_w, play_h, blur_meta["settings"])
            pad_x = float(blur_meta["settings"].get("sub_blur_padding_x", 0.0))
            pad_y = float(blur_meta["settings"].get("sub_blur_padding_y", 0.0))
        for mask in expected_masks:
            clean_mask = {k: v for k, v in mask.items() if k != "_raw_px_box"}
            expected = mask["box"] if (pad_x >= 0.005 and pad_y >= 0.003) else mask.get("_raw_px_box", mask["box"])
            intervals = []
            for rx, ry, rw, rh, reg_intervals in regions:
                if rx <= expected[0] and ry <= expected[1] and rx + rw >= expected[2] and ry + rh >= expected[3]:
                    intervals.extend(reg_intervals)
            gaps = _interval_gaps(mask["start"], mask["end"], intervals)
            if gaps:
                errors.append(f"Source caption is not fully blurred: {mask['name']}")
            mask_audit.append({**clean_mask, "uncovered_intervals": gaps})
    else:
        for mask in expected_masks:
            clean_mask = {k: v for k, v in mask.items() if k != "_raw_px_box"}
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
            mask_audit.append({**clean_mask, "uncovered_intervals": gaps})

    audit = {"policy": "source_timeline_with_independent_caption_masks_v1", "passed": not errors,
             "cover_style": active_style,
             "cue_count": len(expected_cues), "source_mask_count": len(expected_masks),
             "cues": cue_audit, "source_masks": mask_audit, "errors": errors}
    if errors:
        error = SubtitleTimingError("; ".join(errors))
        error.audit = audit
        raise error
    return audit


def generate_cloud_ass(segments, ass_path, *, video_w=1080, video_h=1920,
                       main_y_pct=.78, video_duration, cover_style="solid_box",
                       blur_settings=None, font_name="Arial", font_color="&H00000000",
                       font_weight=2):
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

    active_style = str(cover_style or "solid_box").strip().lower()
    if active_style not in {"solid_box", "adaptive_box", "frosted_blur", "localized_blur"}:
        raise SubtitleTimingError(f"Unsupported subtitle cover_style: {cover_style}")
    resolved_blur = None
    ass_blur_metadata = read_ass_blur_metadata = None
    if active_style == "localized_blur":
        ass_blur_metadata, _, read_ass_blur_metadata, resolve_blur_settings = _load_subtitle_blur()
        try:
            resolved_blur = resolve_blur_settings(blur_settings)
        except ValueError as exc:
            raise SubtitleTimingError(str(exc)) from exc

    duration = float(video_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise SubtitleTimingError("Positive measured source duration is required")
    copied = copy.deepcopy(list(segments))
    if not copied:
        raise SubtitleTimingError("No subtitles to render")
    scale = 720.0 / min(video_w, video_h)
    width, height = round(video_w * scale), round(video_h * scale)
    for source in copied:
        if source.get("ocr_mask_status") in {"no_caption_evidence", "unresolved", "failed"}:
            raise SubtitleTimingError(f"Source-caption geometry is unverified for cue {source['id']}")
        if source.get("ocr_mask_status") == "verified_no_caption" and source.get("caption_geometry_verified") is not True:
            raise SubtitleTimingError(f"Caption absence is unverified for cue {source['id']}")
    masks = _source_masks(copied, width, height, duration)
    ass_font_dirs = (Path(ass_path).resolve().parent / "fonts",)
    if active_style == "localized_blur":
        resolved_font_name = resolve_cloud_subtitle_font(
            font_name,
            font_size=38,
            bold=(font_weight != 1),
            extra_font_dirs=ass_font_dirs,
        )
    else:
        try:
            resolved_font_name = resolve_cloud_subtitle_font(
                font_name,
                font_size=38,
                bold=(font_weight != 1),
                extra_font_dirs=ass_font_dirs,
            )
        except SubtitleTimingError:
            resolved_font_name = font_name
    expected_cues = []
    ids = set()
    page_lines = []
    blur_text_boxes = []
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
            segment.geometry_verified = bool(
                source.get("caption_geometry_verified") or source.get("geometry_verified")
            )
            segment.ocr_mask_status = (
                source.get("ocr_mask_status", "located")
                if segment.best_block else "no_caption_evidence"
            )
            segment.y_pct = source.get("y_pct", main_y_pct)
            segment.max_y_pct = source.get("max_y_pct", min(.95, main_y_pct + .06))
            try:
                generate_ass_file([segment], [], tmp_path, play_res_x=video_w, play_res_y=video_h,
                                  main_y_pct=main_y_pct, font_name=resolved_font_name, font_color=font_color,
                                  font_weight=font_weight, video_duration=end,
                                  cover_style=active_style, strip_terminal_punctuation=True,
                                  blur_settings=resolved_blur)
            except ValueError as exc:
                raise SubtitleTimingError(str(exc)) from exc
            generated = tmp_path.read_text(encoding="utf-8-sig")
            if header is None:
                header = generated.split("Dialogue:", 1)[0].split("; ToolV1BlurData:", 1)[0]
            if active_style == "localized_blur" and read_ass_blur_metadata is not None:
                cue_meta = read_ass_blur_metadata(generated)
                if cue_meta:
                    for tb in cue_meta.get("text_boxes", []):
                        tb_s = max(start, float(tb["start"]))
                        tb_e = min(end, float(tb["end"]))
                        if tb_e - tb_s >= .005:
                            blur_text_boxes.append({**tb, "start": tb_s, "end": tb_e})
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
    if active_style == "localized_blur":
        blur_sources = []
        for source in copied:
            clamped_tracks = []
            if _source_selected(source):
                for block in (source.get("tracking_blocks") or [source.get("best_block")]):
                    if not _source_selected(block):
                        continue
                    b_start = max(0.0, float(_value(block, "start", source["start"])))
                    b_end = min(duration, float(_value(block, "end", source["end"])))
                    if b_end <= b_start:
                        continue
                    clamped_tracks.append({
                        "start": b_start,
                        "end": b_end,
                        "x_pct": float(_value(block, "x_pct")),
                        "y_pct": float(_value(block, "y_pct")),
                        "max_x_pct": float(_value(block, "max_x_pct")),
                        "max_y_pct": float(_value(block, "max_y_pct")),
                        "is_subtitle": True,
                        "is_packaging": False,
                    })
            blur_sources.append({
                "ocr_mask_status": "located" if clamped_tracks else "no_caption_evidence",
                "geometry_verified": True,
                "tracking_blocks": clamped_tracks,
            })
        blur_footer = ass_blur_metadata(blur_sources, resolved_blur, blur_text_boxes)
        Path(ass_path).write_text(header + "\n".join(page_lines) + "\n" + blur_footer, encoding="utf-8-sig")
    else:
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
    return validate_cloud_ass(ass_path, expected_cues, masks, video_duration=duration, cover_style=active_style)
