# -*- coding: utf-8 -*-
"""
Doodle Video Engine (3-Band Whiteboard Explainer)
Sản xuất video dọc 9:16 (720x1280) tự động theo chuẩn 3 dải:
- Band A (0 - 420px): Banner tiêu đề giật gân, chữ to nổi bật (#FCEB00 + từ nhấn đỏ #FF1E1E)
- Band B (420 - 840px): Khung hình Doodle minh họa ĐA DẠNG theo ĐÚNG Ý TƯỞNG CẢNH (Vector Icons + Whiteboard Art)
- Band C (840 - 1280px): Vùng an toàn nền đen #000000 chứa phụ đề chữ lớn căn giữa

Tích hợp tự động tìm kiếm Iconify Vector Icons với thư viện 200,000+ biểu tượng,
render siêu tốc bằng resvg-py, hỗ trợ ảnh tùy chỉnh tải lên, và bộ vẽ procedural dự phòng.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
import resvg_py

from ai.script_tts_service import (
    generate_single_tts,
    CAPCUT_VOICES,
)

logger = logging.getLogger(__name__)

# Thông số hình học chuẩn (3-Band Format 720x1280)
CANVAS_W = 720
CANVAS_H = 1280
BAND_A_H = 420   # y: 0 .. 420
BAND_B_H = 420   # y: 420 .. 840
BAND_C_H = 440   # y: 840 .. 1280

COLOR_BANNER_BG = (255, 255, 255)   # Trắng thuần #FFFFFF
COLOR_YELLOW = (0, 0, 0)            # Chuyển thành Đen #000000
COLOR_RED = (0, 0, 0)               # Chuyển thành Đen #000000
COLOR_WHITE = (255, 255, 255)       # Trắng #FFFFFF
COLOR_BLACK = (0, 0, 0)             # Đen thuần #000000
COLOR_LINE = (0, 0, 0)              # Nét mực vẽ đen đậm #000000
COLOR_ACCENT_LIME = (0, 0, 0)       # Chuyển thành Đen #000000

ICON_CACHE_DIR = Path(r"C:\tool v1\workspace\icon_cache")
ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_system_font(size: int = 32, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Tìm nạp font chữ đẹp trên Windows, ưu tiên Arial Bold / Segoe UI."""
    candidates = (
        ["arialbd.ttf", "segoeuib.ttf", "calibrib.ttf", "tahomabd.ttf"]
        if bold else
        ["arial.ttf", "segoeui.ttf", "calibri.ttf", "tahoma.ttf"]
    )
    for font_name in candidates:
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def hex_to_rgb(hex_code: str) -> Tuple[int, int, int]:
    """Chuyển đổi mã hex (VD #5FBCE4) sang RGB tuple."""
    if not hex_code:
        return (255, 255, 255)
    hex_code = hex_code.lstrip('#')
    if len(hex_code) == 3:
        hex_code = ''.join([c*2 for c in hex_code])
    if len(hex_code) == 6:
        try:
            return tuple(int(hex_code[i:i+2], 16) for i in (0, 2, 4))
        except Exception:
            pass
    return (255, 255, 255)


def get_audio_duration(file_path: Path) -> float:
    """Đo thời lượng file audio bằng ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path)
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"Lỗi đo ffprobe cho {file_path}: {e}, dùng mặc định 3.5s")
        return 3.5


def wrap_text_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """Chia văn bản thành các dòng không vượt quá max_width."""
    words = text.split()
    lines = []
    current_line = []

    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]

    if current_line:
        lines.append(" ".join(current_line))
    return lines


# =========================================================================
# TÌM KIẾM & RENDER VECTOR ICONS TỪ ICONIFY
# =========================================================================

def fetch_vector_icon(query: str, size: int = 180, color: str = "#1e1e1e") -> Optional[Image.Image]:
    """
    Tìm kiếm và nạp icon vector từ Iconify (200,000+ icons), render thành ảnh PNG với nền trong suốt qua resvg-py.
    Lưu bộ nhớ đệm (disk cache) để đạt tốc độ tức thì (<1ms) cho các lần sau.
    """
    clean_q = re.sub(r"[^a-zA-Z0-9_-]", "", query.strip().lower())
    if not clean_q:
        return None

    safe_color = color.replace("#", "")
    cache_file = ICON_CACHE_DIR / f"{clean_q}_{size}_{safe_color}.png"
    if cache_file.exists():
        try:
            return Image.open(cache_file).convert("RGBA")
        except Exception:
            pass

    try:
        search_url = f"https://api.iconify.design/search?query={urllib.parse.quote(clean_q)}&limit=6"
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        icons = data.get("icons", [])
        if not icons:
            return None

        # Ưu tiên các bộ icon nét vẽ rõ ràng, tối giản
        chosen = icons[0]
        for ic in icons:
            if any(ic.startswith(p) for p in ["lucide:", "tabler:", "mdi:", "ph:", "solar:", "streamline:"]):
                chosen = ic
                break

        prefix, iname = chosen.split(":", 1)
        encoded_color = urllib.parse.quote(color)
        svg_url = f"https://api.iconify.design/{prefix}/{iname}.svg?color={encoded_color}"

        req_svg = urllib.request.Request(svg_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_svg, timeout=3.5) as resp:
            svg_code = resp.read().decode("utf-8")

        png_bytes = resvg_py.svg_to_bytes(svg_string=svg_code, width=size, height=size)
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        img.save(cache_file, format="PNG")
        return img
    except Exception as e:
        logger.debug(f"Không thể nạp icon '{query}' từ Iconify: {e}")
        return None


# Thư mục cache tranh phác thảo Whiteboard Line Art
SKETCH_CACHE_DIR = Path(r"C:\tool v1\workspace\doodle_cache\sketches")
SKETCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def fetch_ai_sketch(scene: Dict[str, Any], width: int = 720, height: int = 420) -> Optional[Image.Image]:
    """
    Tổng hợp bức tranh Multi-Layer Whiteboard Line Art (Đen - Trắng) chi tiết 100% đúng theo
    nội dung kịch bản (VD: 2 con mèo đội mũ bảo hiểm đua xe F1 trên đường đua, biểu đồ 50-30-20,
    vắt ví tiền, bộ não bánh răng tư duy...) bằng Kinetic Scene Composer cục bộ siêu tốc,
    loại bỏ hoàn toàn lỗi HTTP 402 từ API bên ngoài.
    """
    try:
        from ai.kinetic_scene_animator import render_composite_scene_frame
        return render_composite_scene_frame(scene, width=width, height=height, phase_progress=0.0)
    except Exception as e:
        logger.warning(f"Lỗi render_composite_scene_frame: {e}")
        return None


def _draw_doodle_label(draw: ImageDraw.ImageDraw, x_offset: int, y_offset: int, width: int, label: str):
    """Vẽ nhãn chữ in hoa phong cách Doodle badge viền trắng căn giữa, chống tràn chữ tiếng Việt."""
    if not label or not label.strip():
        return

    font_label = get_system_font(size=24, bold=True)
    label_text = label.strip().upper()
    bbox = draw.textbbox((0, 0), label_text, font=font_label)
    lw = bbox[2] - bbox[0]
    lh = bbox[3] - bbox[1]

    lbl_box_w = lw + 48
    lbl_box_h = lh + 22
    lbl_box_x = x_offset + (width - lbl_box_w) // 2
    lbl_box_y = y_offset + 25

    # Đổ bóng nhẹ phía sau khung nhãn
    draw.rounded_rectangle(
        [lbl_box_x + 4, lbl_box_y + 4, lbl_box_x + lbl_box_w + 4, lbl_box_y + lbl_box_h + 4],
        radius=8, fill=(40, 40, 40, 100), outline=None
    )
    # Khung viền nhãn chính màu trắng
    draw.rounded_rectangle(
        [lbl_box_x, lbl_box_y, lbl_box_x + lbl_box_w, lbl_box_y + lbl_box_h],
        radius=8, fill=(255, 255, 255), outline=COLOR_LINE, width=3
    )

    # Chữ trong nhãn căn giữa tuyệt đối
    text_x = lbl_box_x + (lbl_box_w - lw) // 2
    text_y = lbl_box_y + (lbl_box_h - lh) // 2 - 2
    draw.text((text_x, text_y), label_text, font=font_label, fill=COLOR_LINE)


# =========================================================================
# BỘ VẼ DOODLE TRỰC QUAN CHO BAND B (HYBRID VECTOR + PROCEDURAL)
# =========================================================================

def draw_doodle_illustration(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    x_offset: int,
    y_offset: int,
    width: int,
    height: int,
    scene: Dict[str, Any],
    bg_color: Tuple[int, int, int]
):
    """
    Vẽ hình hoạt họa phong cách Doodle / Whiteboard chuyên biệt theo ĐÚNG Ý TƯỞNG của từng phân cảnh:
    1. Ưu tiên kiểm tra ảnh tùy chỉnh người dùng upload (nếu có).
    2. Tổng hợp tranh Multi-Layer Whiteboard Line Art chi tiết (Kinetic Scene Composer).
    3. Nạp các Vector Icons chính xác theo icon_keywords & layout (dự phòng).
    """
    center_x = x_offset + width // 2
    center_y = y_offset + height // 2
    label = scene.get("doodle_label", "")

    # 1. ẢNH TÙY CHỈNH NẾU CÓ
    custom_img_path = scene.get("custom_image_path")
    if custom_img_path and Path(custom_img_path).exists():
        try:
            c_img = Image.open(custom_img_path).convert("RGBA")
            c_img.thumbnail((460, 310), Image.Resampling.LANCZOS)
            cx = x_offset + (width - c_img.width) // 2
            cy = y_offset + 90 + (height - 110 - c_img.height) // 2
            img.paste(c_img, (cx, cy), c_img if c_img.mode == "RGBA" else None)
            _draw_doodle_label(draw, x_offset, y_offset, width, label)
            return
        except Exception as e:
            logger.warning(f"Lỗi nạp ảnh tùy chỉnh {custom_img_path}: {e}")

    # 2. TỔNG HỢP TRANH MULTI-LAYER WHITEBOARD COMPOSER (CHUẨN ĐEN TRẮNG 100%)
    ai_sketch = fetch_ai_sketch(scene, width=width, height=height)
    if ai_sketch:
        img.paste(ai_sketch, (x_offset, y_offset))
        return

    # 3. XỬ LÝ THEO VECTOR ICONS (DỰ PHÒNG KHI OFFLINE)
    icon_keywords = scene.get("icon_keywords") or []
    layout = scene.get("layout", "single_centered")

    # Bố cục 2 đối tượng đối đầu (VD: 2 con mèo, người vs cá mập, 2 bên tranh luận)
    if layout == "dual_confrontation" or len(icon_keywords) >= 2:
        kw1 = icon_keywords[0] if icon_keywords else "cat"
        kw2 = icon_keywords[1] if len(icon_keywords) > 1 else kw1
        kw_act = icon_keywords[2] if len(icon_keywords) > 2 else "flash"

        im1 = fetch_vector_icon(kw1, size=180, color="#000000")
        im2 = fetch_vector_icon(kw2, size=180, color="#000000")

        if im1 and im2:
            if kw1 == kw2:
                im2 = ImageOps.mirror(im2)

            img.paste(im1, (x_offset + 70, y_offset + 130), im1)
            img.paste(im2, (x_offset + width - 70 - 180, y_offset + 130), im2)

            # Biểu tượng va chạm/tia chớp ở giữa nét đen
            im_act = fetch_vector_icon(kw_act, size=95, color="#000000")
            if im_act:
                img.paste(im_act, (x_offset + (width - 95) // 2, y_offset + 165), im_act)
            else:
                lx = center_x
                draw.polygon(
                    [(lx, y_offset + 150), (lx - 16, y_offset + 195), (lx + 6, y_offset + 195),
                     (lx - 10, y_offset + 245), (lx + 22, y_offset + 190), (lx, y_offset + 190)],
                    fill=COLOR_BLACK
                )

            # Đường mặt đất vẽ tay nét đen
            ground_y = y_offset + height - 55
            draw.line([(x_offset + 50, ground_y), (x_offset + width - 50, ground_y)], fill=COLOR_LINE, width=4)
            for hx in range(x_offset + 70, x_offset + width - 60, 40):
                draw.line([(hx, ground_y), (hx - 15, ground_y + 15)], fill=COLOR_LINE, width=2)

            _draw_doodle_label(draw, x_offset, y_offset, width, label)
            return

    # Bố cục đối tượng + hành động (VD: Mèo cào móng vuốt, người làm việc, bác sĩ)
    elif layout == "action_duo":
        kw1 = icon_keywords[0] if icon_keywords else "person"
        kw2 = icon_keywords[1] if len(icon_keywords) > 1 else "target"

        im1 = fetch_vector_icon(kw1, size=180, color="#000000")
        im2 = fetch_vector_icon(kw2, size=180, color="#000000")

        if im1 and im2:
            img.paste(im1, (x_offset + 90, y_offset + 130), im1)
            img.paste(im2, (x_offset + width - 90 - 180, y_offset + 130), im2)

            # Mũi tên hành động màu đen ở giữa
            mx = center_x
            my = y_offset + 215
            draw.line([(mx - 35, my), (mx + 25, my)], fill=COLOR_BLACK, width=5)
            draw.polygon([(mx + 25, my - 12), (mx + 45, my), (mx + 25, my + 12)], fill=COLOR_BLACK)

            # Đường đất
            ground_y = y_offset + height - 55
            draw.line([(x_offset + 50, ground_y), (x_offset + width - 50, ground_y)], fill=COLOR_LINE, width=4)
            for hx in range(x_offset + 70, x_offset + width - 60, 40):
                draw.line([(hx, ground_y), (hx - 15, ground_y + 15)], fill=COLOR_LINE, width=2)

            _draw_doodle_label(draw, x_offset, y_offset, width, label)
            return

    # Bố cục 1 đối tượng lớn ở trung tâm
    elif layout == "single_centered" or icon_keywords:
        kw = icon_keywords[0] if icon_keywords else scene.get("visual_type", "idea")
        hero_im = fetch_vector_icon(kw, size=240, color="#000000")
        if hero_im:
            hx = x_offset + (width - 240) // 2
            hy = y_offset + 105
            img.paste(hero_im, (hx, hy), hero_im)

            # Các ngôi sao & dấu chấm phá doodle màu đen
            draw.text((hx - 45, hy + 20), "✦", font=get_system_font(26, bold=True), fill=COLOR_BLACK)
            draw.text((hx + 255, hy + 20), "✦", font=get_system_font(26, bold=True), fill=COLOR_BLACK)
            draw.text((hx - 35, hy + 175), "✦", font=get_system_font(22, bold=True), fill=COLOR_BLACK)
            draw.text((hx + 245, hy + 175), "✦", font=get_system_font(22, bold=True), fill=COLOR_BLACK)

            # Đường mặt đất
            ground_y = y_offset + height - 55
            draw.line([(x_offset + 60, ground_y), (x_offset + width - 60, ground_y)], fill=COLOR_LINE, width=4)
            for hx_line in range(x_offset + 80, x_offset + width - 70, 40):
                draw.line([(hx_line, ground_y), (hx_line - 15, ground_y + 15)], fill=COLOR_LINE, width=2)

            _draw_doodle_label(draw, x_offset, y_offset, width, label)
            return

    # =========================================================================
    # 3. BỘ VẼ PROCEDURAL NÉT ĐEN NỀN TRẮNG
    # =========================================================================
    v_type = (scene.get("visual_type") or "").strip().lower()
    low_concept = (scene.get("visual_concept") or "").lower()

    if "whale" in v_type or "cá voi" in low_concept:
        # Cá voi sát thủ vẽ nét đen
        wave_y = y_offset + height - 55
        for i in range(x_offset + 20, x_offset + width - 20, 70):
            draw.arc([i, wave_y, i + 70, wave_y + 25], start=180, end=0, fill=COLOR_LINE, width=3)
        wx, wy = center_x + 50, center_y + 20
        draw.ellipse([wx - 170, wy - 55, wx + 110, wy + 55], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.chord([wx - 150, wy - 5, wx + 80, wy + 50], start=0, end=180, fill=(0, 0, 0), outline=COLOR_LINE, width=2)
        draw.polygon([(wx - 25, wy - 55), (wx - 45, wy - 120), (wx - 5, wy - 55)], fill=(0, 0, 0), outline=COLOR_LINE, width=4)
        draw.polygon([(wx + 100, wy), (wx + 155, wy - 35), (wx + 145, wy), (wx + 155, wy + 35)], fill=(0, 0, 0), outline=COLOR_LINE, width=4)
        draw.ellipse([wx - 130, wy - 28, wx - 95, wy - 12], fill=(0, 0, 0))
        draw.arc([wx - 85, wy - 95, wx - 50, wy - 50], start=180, end=360, fill=COLOR_LINE, width=3)
        # Người que
        px, py = center_x - 170, center_y - 10
        draw.ellipse([px - 14, py - 40, px + 14, py - 12], outline=COLOR_LINE, fill=(255, 255, 255), width=3)
        draw.line([(px, py - 12), (px, py + 25)], fill=COLOR_LINE, width=3)
        draw.line([(px, py), (px - 22, py - 20)], fill=COLOR_LINE, width=3)
        draw.line([(px, py), (px + 22, py - 20)], fill=COLOR_LINE, width=3)

    elif "cat" in v_type or "mèo" in low_concept:
        # Chú mèo nét đen
        cx, cy = center_x - 140, center_y + 40
        draw.ellipse([cx - 45, cy - 25, cx + 45, cy + 25], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.ellipse([cx + 25, cy - 50, cx + 75, cy], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.polygon([(cx + 35, cy - 45), (cx + 40, cy - 70), (cx + 52, cy - 48)], fill=(255, 255, 255), outline=COLOR_LINE, width=3)
        draw.polygon([(cx + 58, cy - 48), (cx + 70, cy - 70), (cx + 75, cy - 45)], fill=(255, 255, 255), outline=COLOR_LINE, width=3)
        draw.line([(cx + 70, cy - 25), (cx + 98, cy - 35)], fill=COLOR_LINE, width=3)
        draw.line([(cx + 70, cy - 20), (cx + 98, cy - 15)], fill=COLOR_LINE, width=3)

        rx, ry = center_x + 140, center_y + 40
        draw.ellipse([rx - 45, ry - 25, rx + 45, ry + 25], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.ellipse([rx - 75, ry - 50, rx - 25, ry], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.polygon([(rx - 52, ry - 48), (rx - 40, ry - 70), (rx - 35, ry - 45)], fill=(255, 255, 255), outline=COLOR_LINE, width=3)
        draw.polygon([(rx - 75, ry - 45), (rx - 70, ry - 70), (rx - 58, ry - 48)], fill=(255, 255, 255), outline=COLOR_LINE, width=3)
        draw.line([(rx - 70, ry - 25), (rx - 98, ry - 35)], fill=COLOR_LINE, width=3)
        draw.line([(rx - 70, ry - 20), (rx - 98, ry - 15)], fill=COLOR_LINE, width=3)

        # Tia chớp nét đen
        draw.polygon([(center_x, center_y), (center_x - 14, center_y + 35), (center_x + 5, center_y + 35), (center_x - 8, center_y + 75), (center_x + 18, center_y + 30), (center_x, center_y + 30)], fill=COLOR_BLACK)

        ground_y = y_offset + height - 55
        draw.line([(x_offset + 50, ground_y), (x_offset + width - 50, ground_y)], fill=COLOR_LINE, width=4)

    elif "brain" in v_type or "não" in low_concept:
        # Bộ não nét đen
        bx, by = center_x, center_y - 10
        draw.ellipse([bx - 110, by - 60, bx - 5, by + 60], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.ellipse([bx + 5, by - 60, bx + 110, by + 60], fill=(255, 255, 255), outline=COLOR_LINE, width=4)
        draw.arc([bx - 80, by - 40, bx - 30, by + 10], start=30, end=200, fill=COLOR_LINE, width=3)
        draw.arc([bx + 30, by - 40, bx + 80, by + 10], start=340, end=150, fill=COLOR_LINE, width=3)
        # Bóng đèn nét đen
        lx, ly = bx, by - 95
        draw.ellipse([lx - 20, ly - 20, lx + 20, ly + 20], fill=(255, 255, 255), outline=COLOR_LINE, width=3)

    elif "sonar" in v_type or "sóng âm" in low_concept:
        source_x = center_x - 120
        source_y = center_y + 20
        draw.ellipse([source_x - 20, source_y - 20, source_x + 20, source_y + 20], fill=COLOR_BLACK, outline=COLOR_LINE, width=3)
        for r in [50, 90, 130, 170, 210]:
            draw.arc([source_x - r, source_y - r, source_x + r, source_y + r], start=300, end=60, fill=COLOR_LINE, width=4)

    elif "chart" in v_type or "biểu đồ" in low_concept:
        cx, cy = center_x - 100, center_y + 60
        draw.line([(cx, cy), (cx + 220, cy)], fill=COLOR_LINE, width=4)
        draw.line([(cx, cy), (cx, cy - 140)], fill=COLOR_LINE, width=4)
        draw.rectangle([cx + 25, cy - 40, cx + 60, cy], fill=(255, 255, 255), outline=COLOR_LINE, width=3)
        draw.rectangle([cx + 85, cy - 80, cx + 120, cy], fill=(240, 240, 240), outline=COLOR_LINE, width=3)
        draw.rectangle([cx + 145, cy - 125, cx + 180, cy], fill=COLOR_BLACK, outline=COLOR_LINE, width=3)
        draw.line([(cx + 40, cy - 50), (cx + 100, cy - 90), (cx + 165, cy - 145)], fill=COLOR_LINE, width=4)

    else:
        # Stick figure nét đen
        stick_x = center_x
        stick_y = center_y + 30
        draw.ellipse([stick_x - 20, stick_y - 70, stick_x + 20, stick_y - 30], outline=COLOR_LINE, fill=(255, 255, 255), width=4)
        draw.line([(stick_x, stick_y - 30), (stick_x, stick_y + 25)], fill=COLOR_LINE, width=4)
        draw.line([(stick_x, stick_y - 15), (stick_x - 35, stick_y - 45)], fill=COLOR_LINE, width=4)
        draw.line([(stick_x, stick_y - 15), (stick_x + 35, stick_y - 10)], fill=COLOR_LINE, width=4)
        draw.line([(stick_x, stick_y + 25), (stick_x - 25, stick_y + 65)], fill=COLOR_LINE, width=4)
        draw.line([(stick_x, stick_y + 25), (stick_x + 25, stick_y + 65)], fill=COLOR_LINE, width=4)
        draw.text((stick_x + 45, stick_y - 70), "!", font=get_system_font(38, bold=True), fill=COLOR_BLACK)

        ground_y = y_offset + height - 55
        draw.line([(x_offset + 50, ground_y), (x_offset + width - 50, ground_y)], fill=COLOR_LINE, width=4)

    _draw_doodle_label(draw, x_offset, y_offset, width, label)


# =========================================================================
# TẠO KHUNG HÌNH (COMPOSER 3-BAND)
# =========================================================================

def compose_doodle_frame(
    title: str,
    keyword_accent: str,
    scene: Dict[str, Any],
    output_png: Path
):
    """
    Tạo 1 frame ảnh hoàn chỉnh kích thước 720x1280 (chuẩn 9:16) phong cách Whiteboard:
    - Toàn bộ nền là Màu Trắng (#FFFFFF)
    - Khung viền và các dải ngăn cách màu đen (#000000)
    - Band A: Tiêu đề chữ đen in hoa
    - Band B: Hình vẽ Doodle nét đen
    - Band C: Phụ đề chữ đen rõ nét trên nền trắng
    """
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), COLOR_WHITE)
    draw = ImageDraw.Draw(img)

    # 1. BAND A: TIÊU ĐỀ NỀN TRẮNG CHỮ ĐEN (0 .. 420)
    font_badge = get_system_font(size=18, bold=True)
    badge_text = "★ KHÁM PHÁ BÍ MẬT"
    b_bbox = draw.textbbox((0, 0), badge_text, font=font_badge)
    bw = b_bbox[2] - b_bbox[0]
    badge_x = (CANVAS_W - bw) // 2
    draw.rounded_rectangle([badge_x - 12, 25, badge_x + bw + 12, 58], radius=6, fill=(245, 245, 245), outline=COLOR_BLACK, width=2)
    draw.text((badge_x, 32), badge_text, font=font_badge, fill=COLOR_BLACK)

    font_title = get_system_font(size=38, bold=True)
    title_lines = wrap_text_lines(title.strip().upper(), font_title, CANVAS_W - 80, draw)
    title_total_h = len(title_lines) * 54
    title_start_y = 75 + (BAND_A_H - 120 - title_total_h) // 2

    curr_y = title_start_y
    for line in title_lines:
        line_bbox = draw.textbbox((0, 0), line, font=font_title)
        line_w = line_bbox[2] - line_bbox[0]
        curr_x = (CANVAS_W - line_w) // 2
        draw.text((curr_x, curr_y), line, font=font_title, fill=COLOR_BLACK)
        curr_y += 54

    # Đường phân cách viền đen giữa Band A và Band B
    draw.line([(0, BAND_A_H), (CANVAS_W, BAND_A_H)], fill=COLOR_BLACK, width=4)

    # 2. BAND B: DOODLE ILLUSTRATION NỀN TRẮNG NÉT ĐEN (420 .. 840)
    bg_rgb = (255, 255, 255)
    draw.rectangle([0, BAND_A_H, CANVAS_W, BAND_A_H + BAND_B_H], fill=bg_rgb)

    draw_doodle_illustration(
        img=img,
        draw=draw,
        x_offset=0,
        y_offset=BAND_A_H,
        width=CANVAS_W,
        height=BAND_B_H,
        scene=scene,
        bg_color=bg_rgb
    )

    # Đường phân cách viền đen giữa Band B và Band C
    draw.line([(0, BAND_A_H + BAND_B_H), (CANVAS_W, BAND_A_H + BAND_B_H)], fill=COLOR_BLACK, width=4)

    # 3. BAND C: SAFE ZONE & PHỤ ĐỀ NỀN TRẮNG CHỮ ĐEN (840 .. 1280)
    draw.rectangle([0, BAND_A_H + BAND_B_H, CANVAS_W, CANVAS_H], fill=COLOR_WHITE)

    narration_text = scene.get("narration", "").strip()
    font_sub = get_system_font(size=32, bold=True)
    sub_lines = wrap_text_lines(narration_text, font_sub, CANVAS_W - 90, draw)

    line_height = 48
    total_sub_h = len(sub_lines) * line_height
    sub_y = (BAND_A_H + BAND_B_H) + (BAND_C_H - total_sub_h) // 2

    # Khung viền hộp phụ đề
    box_pad = 18
    draw.rounded_rectangle(
        [30, sub_y - box_pad, CANVAS_W - 30, sub_y + total_sub_h + box_pad],
        radius=12,
        fill=(250, 250, 250),
        outline=COLOR_BLACK,
        width=2
    )

    for s_line in sub_lines:
        s_bbox = draw.textbbox((0, 0), s_line, font=font_sub)
        s_w = s_bbox[2] - s_bbox[0]
        s_x = (CANVAS_W - s_w) // 2
        draw.text((s_x, sub_y), s_line, font=font_sub, fill=COLOR_BLACK)
        sub_y += line_height

    # Khung viền đen 4 cạnh bao quanh video 720x1280
    draw.rectangle([0, 0, CANVAS_W - 1, CANVAS_H - 1], outline=COLOR_BLACK, width=5)

    # Lưu frame
    output_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_png), format="PNG")
    logger.debug(f"Đã lưu frame phân cảnh: {output_png}")


# =========================================================================
# PIPELINE RENDER VIDEO HOÀN CHỈNH BẰNG FFMPEG
# =========================================================================

def render_doodle_video(
    script_data: Dict[str, Any],
    voice_id: str = "multi_male_felipe_uranus_bigtts",
    output_video_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    use_hand_draw: bool = True,
    whiteboard_mode: str = "3band",
    fps: int = 24
) -> Dict[str, Any]:
    """
    Render toàn bộ video Doodle từ kịch bản phân cảnh:
    1. Sinh TTS âm thanh từng cảnh (CapCut/Edge TTS)
    2. Diễn hoạt bàn tay vẽ thật (Hand-Drawn Moving Pen Animation) bằng CUDA + NVENC
    3. Ghép khung 3-Band chuẩn 720x1280 (hoặc fullscreen) bằng FFmpeg
    4. Concat thành video MP4 hoàn chỉnh với chuẩn âm lượng -16 LUFS
    """
    job_id = f"doodle_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    work_dir = Path(r"C:\tool v1\workspace\doodle_render") / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    if not output_video_path:
        output_dir = Path(r"C:\tool v1\workspace\output")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_video_path = output_dir / f"{job_id}_final.mp4"

    title = script_data.get("title", "GIẢI MÃ BÍ ẨN")
    keyword_accent = script_data.get("keyword_accent", "")
    scenes = script_data.get("scenes", [])

    if not scenes:
        raise ValueError("Không có phân cảnh nào để dựng video!")

    logger.info(f"Bắt đầu dựng video Doodle '{title}' ({len(scenes)} cảnh, vẽ tay thật={use_hand_draw})...")
    if progress_callback:
        progress_callback(10, f"Đang khởi tạo dựng video ({len(scenes)} cảnh, hiệu ứng vẽ tay={use_hand_draw})...")

    segment_videos: List[Path] = []
    total_scenes = len(scenes)

    # 1. Xử lý từng phân cảnh
    for idx, sc in enumerate(scenes):
        scene_num = idx + 1
        pct = 15 + int((idx / total_scenes) * 65)
        if progress_callback:
            progress_callback(pct, f"Đang tạo cảnh {scene_num}/{total_scenes}: Sinh giọng đọc & diễn hoạt bàn tay vẽ...")

        narration = sc.get("narration", "").strip()
        audio_file = work_dir / f"scene_{scene_num:03d}.mp3"

        # Sinh giọng đọc TTS
        try:
            generate_single_tts(narration, audio_file, voice=voice_id)
        except Exception as e:
            logger.error(f"Lỗi TTS cảnh {scene_num}: {e}")
            try:
                generate_single_tts(narration, audio_file, voice="vi-VN-HoaiMyNeural")
            except Exception as e2:
                raise RuntimeError(f"Lỗi không thể sinh giọng đọc cho cảnh {scene_num}: {e2}")

        # Đo thời lượng thực tế của file âm thanh
        duration = get_audio_duration(audio_file)
        dur_padded = round(duration + 0.25, 2)
        seg_mp4 = work_dir / f"seg_{scene_num:03d}.mp4"

        rendered_with_animation = False
        if use_hand_draw or (whiteboard_mode or "").lower() in ["kinetic", "reveal"]:
            try:
                from ai.whiteboard_hand_engine import (
                    compose_band_b_image,
                    compose_band_a_c_base,
                    render_scene_band_b,
                    composite_hand_drawn_scene_3band
                )
                band_b_img = compose_band_b_image(sc)
                base_img = compose_band_a_c_base(title, keyword_accent, sc)
                base_png = work_dir / f"base_{scene_num:03d}.png"
                base_img.save(base_png)

                clip_mp4 = render_scene_band_b(
                    illustration_img=band_b_img,
                    duration=dur_padded,
                    output_dir=work_dir / f"clips",
                    fps=fps,
                    show_hand=use_hand_draw,
                    whiteboard_mode=whiteboard_mode,
                    scene=sc
                )
                composite_hand_drawn_scene_3band(
                    band_b_video=clip_mp4,
                    base_frame_png=base_png,
                    audio_file=audio_file,
                    duration=dur_padded,
                    output_mp4=seg_mp4
                )
                rendered_with_animation = True
                logger.info(f"Cảnh {scene_num}: Đã render xong hiệu ứng bảng trắng ({whiteboard_mode}).")
            except Exception as e_anim:
                logger.warning(f"Lỗi diễn hoạt bảng trắng cảnh {scene_num}: {e_anim}. Chuyển sang render tĩnh dự phòng.")
                rendered_with_animation = False

        if not rendered_with_animation:
            # Vẽ frame 3-Band và render tĩnh dự phòng
            frame_png = work_dir / f"frame_{scene_num:03d}.png"
            compose_doodle_frame(title, keyword_accent, sc, frame_png)

            cmd_seg = [
                "ffmpeg", "-y",
                "-loop", "1",
                "-t", str(dur_padded),
                "-i", str(frame_png),
                "-i", str(audio_file),
                "-c:v", "libx264",
                "-tune", "stillimage",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",
                str(seg_mp4)
            ]
            subprocess.run(cmd_seg, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

        segment_videos.append(seg_mp4)

    # 2. Ghép tất cả các segment thành video hoàn chỉnh
    if progress_callback:
        progress_callback(80, "Đang ghép nối toàn bộ phân cảnh...")

    concat_txt = work_dir / "concat_list.txt"
    with open(concat_txt, "w", encoding="utf-8") as f:
        for seg in segment_videos:
            f.write(f"file '{seg.name}'\n")

    temp_merged = work_dir / "temp_merged.mp4"
    cmd_concat = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-c", "copy",
        str(temp_merged)
    ]
    subprocess.run(cmd_concat, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, cwd=str(work_dir))

    # 3. Chuẩn hóa âm thanh chuẩn phát sóng -16 LUFS
    if progress_callback:
        progress_callback(92, "Đang chuẩn hóa âm thanh -16 LUFS và hoàn tất video...")

    cmd_loudnorm = [
        "ffmpeg", "-y",
        "-i", str(temp_merged),
        "-af", "loudnorm=I=-16:LRA=11:TP=-1.5",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_video_path)
    ]
    try:
        subprocess.run(cmd_loudnorm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception as e:
        logger.warning(f"Loudnorm failed: {e}, fallback copy temp_merged")
        shutil.copy(temp_merged, output_video_path)

    final_dur = get_audio_duration(output_video_path)
    file_size_mb = round(os.path.getsize(output_video_path) / (1024 * 1024), 2)

    if progress_callback:
        progress_callback(100, "Hoàn tất! Video sẵn sàng.")

    logger.info(f"Dựng xong video Doodle: {output_video_path} ({final_dur}s, {file_size_mb} MB)")
    return {
        "status": "success",
        "job_id": job_id,
        "video_path": str(output_video_path),
        "video_filename": output_video_path.name,
        "download_url": f"/api/script/stream/{output_video_path.name}",
        "duration": final_dur,
        "file_size_mb": file_size_mb,
        "scene_count": len(scenes),
        "use_hand_draw": use_hand_draw,
        "whiteboard_mode": whiteboard_mode
    }
