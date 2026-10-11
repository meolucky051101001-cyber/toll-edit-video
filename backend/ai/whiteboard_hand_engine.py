# -*- coding: utf-8 -*-
"""
Whiteboard Hand Engine for Script Studio
Tích hợp bộ diễn hoạt bảng trắng vẽ tay siêu thực từ letuandat131/whiteboard-animation-vi:
- Diễn hoạt bàn tay người thật cầm bút vẽ nét mực đen và tô màu marker (Moving Hand Overlay)
- Tính toán đường đi phân cụm màu CIELAB và phân tách nét vẽ
- Tăng tốc phần cứng CUDA + NVIDIA NVENC trên RTX 4050
- Hỗ trợ chuẩn 3 dải (3-Band Format 9:16) và chuẩn bảng trắng toàn khung hình (Full Canvas)
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import torch
try:
    torch.backends.cudnn.enabled = False
except Exception:
    pass

from handdraw.settings import Settings
from handdraw.render import render_video

logger = logging.getLogger(__name__)

CANVAS_W = 720
CANVAS_H = 1280
BAND_A_H = 420
BAND_B_H = 420
BAND_C_H = 440

COLOR_BANNER_BG = (10, 13, 47)      # Xanh navy đậm #0A0D2F
COLOR_YELLOW = (252, 235, 0)        # Vàng tươi #FCEB00
COLOR_RED = (255, 30, 30)           # Đỏ tươi #FF1E1E
COLOR_WHITE = (255, 255, 255)       # Trắng #FFFFFF
COLOR_BLACK = (0, 0, 0)             # Đen thuần #000000
COLOR_LINE = (30, 30, 30)           # Nét mực vẽ đen đậm #1E1E1E


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


def get_system_font(size: int = 32, bold: bool = True) -> ImageFont.FreeTypeFont:
    """Tìm nạp font chữ trên Windows."""
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


def wrap_text_lines(text: str, font: ImageFont.FreeTypeFont, max_width: int, draw: ImageDraw.ImageDraw) -> List[str]:
    """Tự động xuống dòng giữ nguyên từ ngữ, tránh tràn khung."""
    words = text.split()
    lines: List[str] = []
    curr_line: List[str] = []
    for w in words:
        test_line = " ".join(curr_line + [w])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            curr_line.append(w)
        else:
            if curr_line:
                lines.append(" ".join(curr_line))
                curr_line = [w]
            else:
                lines.append(w)
                curr_line = []
    if curr_line:
        lines.append(" ".join(curr_line))
    return lines


def compose_band_b_image(scene: Dict[str, Any], width: int = CANVAS_W, height: int = BAND_B_H) -> Image.Image:
    """
    Tạo riêng tấm ảnh minh họa Band B (kích thước 720x420) chuẩn Whiteboard nền trắng tinh (#FFFFFF)
    với mọi chi tiết vẽ ra là MÀU ĐEN (#000000).
    """
    from ai.doodle_engine import draw_doodle_illustration

    bg_rgb = (255, 255, 255)
    img = Image.new("RGB", (width, height), color=bg_rgb)
    draw = ImageDraw.Draw(img)

    draw_doodle_illustration(
        img=img,
        draw=draw,
        x_offset=0,
        y_offset=0,
        width=width,
        height=height,
        scene=scene,
        bg_color=bg_rgb
    )
    return img


def compose_band_a_c_base(
    title: str,
    keyword_accent: str,
    scene: Dict[str, Any],
    width: int = CANVAS_W,
    height: int = CANVAS_H
) -> Image.Image:
    """
    Tạo khung ảnh tĩnh 720x1280 chuẩn BẢNG TRẮNG NỀN TRẮNG KHUNG ĐEN (Black on White):
    - Toàn bộ nền video là Màu Trắng (#FFFFFF)
    - Band A (0 .. 420): Khung tiêu đề chữ đen in hoa đậm, viền đen thanh lịch
    - Band B (420 .. 840): Khung bảng trắng để video bàn tay vẽ bút dạ đen đè lên
    - Band C (840 .. 1280): Khung phụ đề chữ đen rõ nét trên nền trắng
    - Khung viền đen 4 góc bao quanh toàn bộ video chuẩn tranh vẽ bảng trắng
    """
    img = Image.new("RGB", (width, height), color=COLOR_WHITE)
    draw = ImageDraw.Draw(img)

    # 1. BAND A: TIÊU ĐỀ NỀN TRẮNG CHỮ ĐEN (0 .. 420)
    font_badge = get_system_font(size=20, bold=True)
    draw.rounded_rectangle([30, 24, 250, 64], radius=8, fill=(245, 245, 245), outline=COLOR_BLACK, width=2)
    draw.text((45, 33), "★ KHÁM PHÁ BÍ MẬT", font=font_badge, fill=COLOR_BLACK)

    font_title = get_system_font(size=44, bold=True)
    title_clean = title.strip().upper()
    title_lines = wrap_text_lines(title_clean, font_title, width - 60, draw)
    curr_y = 85

    for line in title_lines[:4]:
        line_bbox = draw.textbbox((0, 0), line, font=font_title)
        line_w = line_bbox[2] - line_bbox[0]
        curr_x = (width - line_w) // 2
        draw.text((curr_x, curr_y), line, font=font_title, fill=COLOR_BLACK)
        curr_y += 54

    # Đường phân cách viền đen giữa Band A và B
    draw.line([(0, BAND_A_H), (width, BAND_A_H)], fill=COLOR_BLACK, width=4)

    # Đường phân cách viền đen giữa Band B và C
    draw.line([(0, BAND_A_H + BAND_B_H), (width, BAND_A_H + BAND_B_H)], fill=COLOR_BLACK, width=4)

    # 2. BAND C: PHỤ ĐỀ NỀN TRẮNG CHỮ ĐEN (840 .. 1280)
    narration_text = scene.get("narration", "").strip()
    font_sub = get_system_font(size=32, bold=True)
    sub_lines = wrap_text_lines(narration_text, font_sub, width - 90, draw)

    line_height = 48
    total_sub_h = len(sub_lines) * line_height
    sub_y = (BAND_A_H + BAND_B_H) + (BAND_C_H - total_sub_h) // 2

    # Hộp bo viền đen nhẹ bao quanh phụ đề
    box_pad = 18
    draw.rounded_rectangle(
        [30, sub_y - box_pad, width - 30, sub_y + total_sub_h + box_pad],
        radius=12,
        fill=(250, 250, 250),
        outline=COLOR_BLACK,
        width=2
    )

    for s_line in sub_lines:
        s_bbox = draw.textbbox((0, 0), s_line, font=font_sub)
        s_w = s_bbox[2] - s_bbox[0]
        s_x = (width - s_w) // 2
        draw.text((s_x, sub_y), s_line, font=font_sub, fill=COLOR_BLACK)
        sub_y += line_height

    # Khung viền đen 4 cạnh bao quanh video
    draw.rectangle([0, 0, width - 1, height - 1], outline=COLOR_BLACK, width=5)

    return img


def render_hand_drawn_band_b(
    illustration_img: Image.Image,
    duration: float,
    output_dir: Path,
    fps: int = 24,
    show_hand: bool = True
) -> Path:
    """
    Diễn hoạt bàn tay vẽ thật cho hình minh họa Band B (720x420)
    với 100% nét mực đen trên nền giấy trắng tinh.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    img_arr = np.array(illustration_img.convert("RGB"))

    # Toàn bộ thời gian vẽ là nét bút mực đen của bàn tay thật (ink_percent=98.0)
    settings = Settings(
        mode="d",
        duration=max(1.5, float(duration)),
        draw_percent=72.0,
        ink_percent=98.0,
        width=720,
        height=420,
        fps=fps,
        paper_color="#ffffff",
        device=device,
        encoder="nvenc",
        show_hand=show_hand,
        subject_first=False,
        hand_scale=0.95
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    video_path, report = render_video(img_arr, settings, output_dir=output_dir)
    logger.info(f"Đã render xong phân cảnh vẽ tay Band B nét đen: {video_path} ({duration:.2f}s, wall: {report.get('wall_seconds', 0):.2f}s)")
    return Path(video_path)


# Singleton WhiteboardAnimator instance (Engine mã nguồn mở Kinoslide / masihsultani)
_whiteboard_animator_instance = None

def get_whiteboard_animator():
    global _whiteboard_animator_instance
    if _whiteboard_animator_instance is None:
        from whiteboard_animator.animator import WhiteboardAnimator
        _whiteboard_animator_instance = WhiteboardAnimator()
    return _whiteboard_animator_instance


def render_kinetic_band_b(
    illustration_img: Image.Image,
    duration: float,
    output_dir: Path,
    fps: int = 24
) -> Path:
    """
    Diễn hoạt bảng trắng Kinetic Reveal (từ repo masihsultani/whiteboard-animator)
    - Tách nhận diện chữ viết CRAFT để viết tuần tự từ trái qua phải, trên xuống dưới
    - Truy vết khung xương nét vẽ Topological Skeleton Tracing (không bị loang nét)
    - Vẽ hình bao quanh (container) trước, chi tiết bên trong sau
    - Tăng tốc phần cứng CUDA NVENC siêu tốc
    - Phong cách bảng trắng sạch sẽ, không bị bàn tay che khuất nét vẽ
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = output_dir / f"kinetic_band_b_{uuid.uuid4().hex[:8]}.mp4"
    img_arr = np.array(illustration_img.convert("RGB"))

    total_dur = max(1.5, float(duration))
    draw_dur = max(1.0, round(total_dur * 0.78, 2))

    animator = get_whiteboard_animator()
    t_start = time.perf_counter()
    animator.render_to_file(
        img_array=img_arr,
        draw_duration=draw_dur,
        total_duration=total_dur,
        output_path=str(out_mp4),
        fps=fps,
        use_nvenc=True
    )
    wall_sec = time.perf_counter() - t_start
    logger.info(f"Đã render xong phân cảnh Kinetic Band B: {out_mp4} ({total_dur:.2f}s, wall: {wall_sec:.2f}s)")
    return out_mp4


def render_scene_band_b(
    illustration_img: Image.Image,
    duration: float,
    output_dir: Path,
    fps: int = 24,
    show_hand: bool = True,
    whiteboard_mode: str = "3band"
) -> Path:
    """
    Điều phối bộ dựng Band B dựa theo tùy chọn của người dùng:
    - Nếu whiteboard_mode == 'kinetic' hoặc show_hand is False: dùng engine Kinetic Reveal (Kinoslide)
    - Nếu whiteboard_mode in ['hand', '3band']: dùng engine Hand-Drawn (Bàn tay người thật vẽ nét dạ đen)
    """
    mode_clean = (whiteboard_mode or "").lower().strip()
    if mode_clean in ["kinetic", "reveal"] or not show_hand:
        return render_kinetic_band_b(illustration_img, duration, output_dir, fps=fps)
    else:
        return render_hand_drawn_band_b(illustration_img, duration, output_dir, fps=fps, show_hand=True)


def composite_hand_drawn_scene_3band(
    band_b_video: Path,
    base_frame_png: Path,
    audio_file: Path,
    duration: float,
    output_mp4: Path
) -> Path:
    """
    Ghép video diễn hoạt bàn tay vẽ Band B vào khung hình 3-Band 720x1280 bằng FFmpeg hardware acceleration.
    """
    output_mp4.parent.mkdir(parents=True, exist_ok=True)

    # Thử encode bằng h264_nvenc trên RTX 4050
    cmd_nvenc = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", str(base_frame_png),
        "-i", str(band_b_video),
        "-i", str(audio_file),
        "-filter_complex", "[0:v][1:v]overlay=0:420:shortest=1[v]",
        "-map", "[v]",
        "-map", "2:a",
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-cq", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        str(output_mp4)
    ]
    try:
        subprocess.run(cmd_nvenc, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return output_mp4
    except Exception as e_nvenc:
        logger.warning(f"NVENC composite thất bại ({e_nvenc}), chuyển sang libx264...")
        cmd_cpu = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(base_frame_png),
            "-i", str(band_b_video),
            "-i", str(audio_file),
            "-filter_complex", "[0:v][1:v]overlay=0:420:shortest=1[v]",
            "-map", "[v]",
            "-map", "2:a",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output_mp4)
        ]
        subprocess.run(cmd_cpu, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return output_mp4
