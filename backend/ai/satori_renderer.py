# -*- coding: utf-8 -*-
"""
Satori Renderer (Node sidecar) cho Studio Kịch Bản.
====================================================
Render Band A (banner tiêu đề) bằng HTML/CSS layout qua Satori:
    Python (props JSON) -> Node sidecar `tools/render_node/satori/render_title.mjs` -> SVG -> resvg_py -> PIL.Image

Đặc điểm an toàn:
- Mặc định TẮT (cờ môi trường `SCRIPT_SATORI_BAND_A=1`), engine PIL hiện tại vẫn là đường chính.
- Mọi lỗi (thiếu node, thiếu npm install, timeout, SVG hỏng) đều trả `None` để caller tự lùi về PIL.
- Không phụ thuộc runtime lúc import: module này không import gì nặng ngoài PIL/resvg_py vốn đã có trong backend.

Sử dụng:
    from ai.satori_renderer import is_available, render_band_a_png
    img = render_band_a_png(title="...", keyword_accent="...")   # -> PIL.Image RGBA 720x420 hoặc None
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

import resvg_py
from PIL import Image

logger = logging.getLogger(__name__)

# Thư mục sidecar Node (C:\tool v1\tools\render_node)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_SIDECAR_DIR = PROJECT_ROOT / "tools" / "render_node"
RENDER_TITLE_SCRIPT = NODE_SIDECAR_DIR / "satori" / "render_title.mjs"
SATORI_MODULE_DIR = NODE_SIDECAR_DIR / "node_modules" / "satori"

# Font hệ thống Windows (Arial Bold — cùng họ font engine PIL đang dùng để đồng bộ phong cách)
_WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
FONT_BOLD = _WINDIR / "Fonts" / "arialbd.ttf"
FONT_REGULAR = _WINDIR / "Fonts" / "arial.ttf"
FONT_SYMBOL = _WINDIR / "Fonts" / "seguisym.ttf"

# Không hiện cửa sổ console khi gọi node từ tiến trình nền (Windows)
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULT_BADGE_TEXT = "★ KHÁM PHÁ BÍ MẬT"


def satori_enabled() -> bool:
    """Cờ bật/tắt qua biến môi trường (mặc định TẮT để bảo vệ production)."""
    return os.getenv("SCRIPT_SATORI_BAND_A", "0").strip().lower() in ("1", "true", "on", "yes")


def is_available() -> bool:
    """Kiểm tra kỹ thuật: có node trên PATH + sidecar đã `npm install` + script tồn tại."""
    if shutil.which("node") is None:
        return False
    if not RENDER_TITLE_SCRIPT.exists():
        return False
    if not SATORI_MODULE_DIR.exists():
        return False
    return True


def _run_node_svg(payload: Dict[str, Any], timeout: float = 30.0) -> str:
    """Gọi node sidecar, truyền props JSON qua stdin (UTF-8), nhận SVG qua stdout."""
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("Không tìm thấy node trên PATH")

    proc = subprocess.run(
        [node, str(RENDER_TITLE_SCRIPT)],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(NODE_SIDECAR_DIR),
        timeout=timeout,
        check=False,
        creationflags=_CREATE_NO_WINDOW,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")[:400]
        raise RuntimeError(f"Node sidecar lỗi (exit {proc.returncode}): {stderr}")

    svg = proc.stdout.decode("utf-8")
    if not svg.lstrip().startswith("<svg"):
        raise RuntimeError(f"Node sidecar không trả về SVG hợp lệ: {svg[:200]!r}")
    return svg


def render_title_svg(
    title: str,
    keyword_accent: str = "",
    width: int = 720,
    height: int = 420,
    badge_text: str = DEFAULT_BADGE_TEXT,
    accent_color: str = "#000000",
    title_font_size: Optional[int] = None,
    timeout: float = 30.0,
) -> str:
    """Render banner tiêu đề Band A -> chuỗi SVG (ném lỗi nếu thất bại)."""
    payload: Dict[str, Any] = {
        "title": (title or "").strip().upper(),
        "keywordAccent": (keyword_accent or "").strip().upper(),
        "badgeText": badge_text,
        "accentColor": accent_color,
        "width": int(width),
        "height": int(height),
        "fonts": {
            "bold": str(FONT_BOLD) if FONT_BOLD.exists() else "",
            "regular": str(FONT_REGULAR) if FONT_REGULAR.exists() else "",
            "symbol": str(FONT_SYMBOL) if FONT_SYMBOL.exists() else "",
        },
    }
    if title_font_size:
        payload["titleFontSize"] = int(title_font_size)
    return _run_node_svg(payload, timeout=timeout)


def render_band_a_png(
    title: str,
    keyword_accent: str = "",
    width: int = 720,
    height: int = 420,
    badge_text: str = DEFAULT_BADGE_TEXT,
    accent_color: str = "#000000",
    title_font_size: Optional[int] = None,
    timeout: float = 30.0,
) -> Optional[Image.Image]:
    """
    Render Band A -> PIL.Image RGBA. Trả None nếu bất kỳ bước nào thất bại
    (caller tự lùi về bộ vẽ PIL hiện tại).
    """
    try:
        svg = render_title_svg(
            title=title,
            keyword_accent=keyword_accent,
            width=width,
            height=height,
            badge_text=badge_text,
            accent_color=accent_color,
            title_font_size=title_font_size,
            timeout=timeout,
        )
        png_bytes = resvg_py.svg_to_bytes(svg_string=svg, width=int(width), height=int(height))
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        return img
    except Exception as exc:  # noqa: BLE001 — fail-open: trả None để dùng PIL dự phòng
        logger.warning("Satori Band A thất bại, sẽ dùng bộ vẽ PIL dự phòng: %s", exc)
        return None
