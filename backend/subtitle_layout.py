"""Measure and greedily wrap subtitles in ASS script coordinates."""

import logging
import os
import struct
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _installed_fonts():
    roots = [
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts",
        Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
        Path("/System/Library/Fonts"), Path("/Library/Fonts"),
    ]
    entries = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix.lower() not in {".ttf", ".ttc", ".otf"}:
                continue
            try:
                family, style = ImageFont.truetype(str(path), 16).getname()
                entries.append((family.casefold(), style.casefold(), str(path)))
            except (OSError, ValueError):
                continue
    return entries


def _ass_em_scale(font):
    # libass uses OS/2 Win ascent + descent for REAL_DIM font sizing, rather
    # than the em size Pillow accepts. Read just these metrics, without adding
    # another font-library dependency. See libass/ass_font.c:set_font_metrics.
    try:
        data = Path(font.path).read_bytes()
        offset = struct.unpack_from(">I", data, 12)[0] if data[:4] == b"ttcf" else 0
        count = struct.unpack_from(">H", data, offset + 4)[0]
        tables = {}
        for index in range(count):
            tag, _, position, _ = struct.unpack_from(">4sIII", data, offset + 12 + index * 16)
            tables[tag] = position
        units = struct.unpack_from(">H", data, tables[b"head"] + 18)[0]
        ascent, descent = struct.unpack_from(">hh", data, tables[b"OS/2"] + 74)
        if units > 0 and ascent + descent > 0:
            return units / (ascent + descent)
    except (OSError, KeyError, TypeError, struct.error):
        pass
    ascent, descent = font.getmetrics()
    return font.size / max(ascent + descent, 1)


@lru_cache(maxsize=32)
def subtitle_measure(font_name="Arial", font_size=38, bold=True):
    requested = font_name.casefold()
    matches = [item for item in _installed_fonts() if item[0] == requested]
    matches.sort(key=lambda item: (
        ("bold" in item[1] or "black" in item[1]) != bool(bold),
        "italic" in item[1] or "oblique" in item[1],
    ))
    candidates = [item[2] for item in matches]
    candidates += [font_name, "arialbd.ttf" if bold else "arial.ttf",
                   "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    for candidate in candidates:
        try:
            # Oversampling keeps rounding in small fonts from moving a word
            # onto another line. BASIC disables kerning, as in the ASS style.
            face = ImageFont.truetype(candidate, font_size * 8, layout_engine=ImageFont.Layout.BASIC)
            break
        except (OSError, ValueError):
            continue
    else:
        raise RuntimeError("No readable subtitle font is installed")
    if not matches:
        logger.warning("Measuring unavailable subtitle font %s with %s", font_name, face.getname()[0])
    scale = _ass_em_scale(face) / 8

    @lru_cache(maxsize=4096)
    def measure(text):
        left, _, right, _ = face.getbbox(text)
        return max(face.getlength(text), right - min(left, 0)) * scale

    return measure


def wrap_subtitle_text(text, max_width, measure):
    """Fill the current line before wrapping; never balance line lengths."""
    lines = []
    line = ""
    for word in text.split():
        candidate = (line + " " + word).strip()
        if measure(candidate) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        # Only a single token wider than the complete frame may be broken.
        for char in word:
            if line and measure(line + char) > max_width:
                lines.append(line)
                line = ""
            line += char
    if line:
        lines.append(line)
    return lines
