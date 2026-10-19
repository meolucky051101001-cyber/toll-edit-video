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
    height: int = CANVAS_H,
    prefer_satori: Optional[bool] = None
) -> Image.Image:
    """
    Tạo khung ảnh tĩnh 720x1280 chuẩn BẢNG TRẮNG NỀN TRẮNG KHUNG ĐEN (Black on White):
    - Toàn bộ nền video là Màu Trắng (#FFFFFF)
    - Band A (0 .. 420): Khung tiêu đề chữ đen in hoa đậm, viền đen thanh lịch
      (dùng engine Satori nếu được bật — fail-open về PIL khi lỗi/thiếu Node)
    - Band B (420 .. 840): Khung bảng trắng để video bàn tay vẽ bút dạ đen đè lên
    - Band C (840 .. 1280): Khung phụ đề chữ đen rõ nét trên nền trắng
    - Khung viền đen 4 góc bao quanh toàn bộ video chuẩn tranh vẽ bảng trắng
    """
    img = Image.new("RGB", (width, height), color=COLOR_WHITE)
    draw = ImageDraw.Draw(img)

    # 1. BAND A: TIÊU ĐỀ NỀN TRẮNG CHỮ ĐEN (0 .. 420)
    band_a_done = False
    use_satori = prefer_satori
    if use_satori is None:
        use_satori = os.getenv("SCRIPT_SATORI_BAND_A", "0").strip().lower() in ("1", "true", "on", "yes")
    if use_satori:
        try:
            from ai.satori_renderer import is_available as _satori_available
            from ai.satori_renderer import render_band_a_png as _satori_band_a_png

            if _satori_available():
                ba_img = _satori_band_a_png(title=title, keyword_accent=keyword_accent, width=width, height=BAND_A_H)
                if ba_img is not None:
                    img.paste(ba_img, (0, 0), ba_img)
                    band_a_done = True
        except Exception as e_satori:  # noqa: BLE001 — fail-open về PIL
            logger.warning(f"Satori Band A (whiteboard) không khả dụng, dùng PIL dự phòng: {e_satori}")

    if not band_a_done:
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


def _encode_rgb_frames_to_mp4(frames: List[np.ndarray], out_mp4: Path, fps: int = 24) -> Path:
    """Encode a list of RGB uint8 numpy frames into an MP4 clip using NVENC (with libx264 fallback)."""
    if not frames:
        raise ValueError("No frames to encode")
    h, w = frames[0].shape[:2]
    for use_nvenc in (True, False):
        codec = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "24"] if use_nvenc else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{w}x{h}", "-pix_fmt", "rgb24",
            "-r", str(fps), "-i", "-",
            *codec,
            "-pix_fmt", "yuv420p",
            "-an",
            str(out_mp4)
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for fr in frames:
                proc.stdin.write(memoryview(np.ascontiguousarray(fr, dtype=np.uint8)).cast("B"))
            proc.stdin.close()
            proc.wait()
            if proc.returncode == 0 and out_mp4.exists():
                return out_mp4
        except Exception as e:
            logger.debug(f"Encode frames (nvenc={use_nvenc}) failed: {e}")
    raise RuntimeError(f"Failed to encode live action frames to {out_mp4}")


def render_hand_drawn_band_b(
    illustration_img: Image.Image,
    duration: float,
    output_dir: Path,
    fps: int = 24,
    show_hand: bool = True,
    scene: Optional[Dict[str, Any]] = None
) -> Path:
    """
    Diễn hoạt 2 giai đoạn cho Band B (720x420):
    - Giai đoạn 1 (56% thời lượng): Bàn tay thật cầm bút dạ đen vẽ bức tranh lên bảng trắng.
    - Giai đoạn 2 (44% thời lượng): Bức tranh sau khi vẽ xong chuyển động sống động (2 mèo đua xe chạy trên đường đua, biểu đồ tách lớp, bánh răng xoay, Camera Zoom/Shake).
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"
    img_arr = np.array(illustration_img.convert("RGB"))
    output_dir.mkdir(parents=True, exist_ok=True)

    total_dur = max(1.8, float(duration))
    has_live_motion = scene is not None and not scene.get("custom_image_path")

    if has_live_motion:
        draw_phase_dur = max(1.2, round(total_dur * 0.56, 2))
        live_phase_dur = max(0.5, round(total_dur - draw_phase_dur, 2))
        draw_pct = 94.0
    else:
        draw_phase_dur = total_dur
        live_phase_dur = 0.0
        draw_pct = 75.0

    settings = Settings(
        mode="d",
        duration=draw_phase_dur,
        draw_percent=draw_pct,
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

    video_path, report = render_video(img_arr, settings, output_dir=output_dir)
    phase1_mp4 = Path(video_path)

    if has_live_motion and live_phase_dur >= 0.35:
        try:
            from ai.kinetic_scene_animator import generate_live_action_frames
            live_frames_count = max(8, int(round(live_phase_dur * fps)))
            live_frames = generate_live_action_frames(scene, num_frames=live_frames_count, width=720, height=420)
            if live_frames:
                uid = uuid.uuid4().hex[:8]
                phase2_mp4 = output_dir / f"live_phase2_{uid}.mp4"
                _encode_rgb_frames_to_mp4(live_frames, phase2_mp4, fps=fps)

                combined_mp4 = output_dir / f"hand_live_combined_{uid}.mp4"
                concat_list = output_dir / f"concat_{uid}.txt"
                with open(concat_list, "w", encoding="utf-8") as cf:
                    cf.write(f"file '{phase1_mp4.name}'\n")
                    cf.write(f"file '{phase2_mp4.name}'\n")

                cmd_concat = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list), "-c", "copy", str(combined_mp4)
                ]
                subprocess.run(cmd_concat, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, cwd=str(output_dir))
                logger.info(
                    f"Đã render 2-Phase Hand-Drawn + Live Motion Band B: {combined_mp4.name} "
                    f"(Draw: {draw_phase_dur:.2f}s + Live Motion: {live_phase_dur:.2f}s)"
                )
                return combined_mp4
        except Exception as e_live:
            logger.warning(f"Ghép Phase 2 Live Motion cho Hand-Drawn thất bại ({e_live}), dùng Phase 1.")

    logger.info(f"Đã render xong phân cảnh vẽ tay Band B nét đen: {phase1_mp4} ({total_dur:.2f}s)")
    return phase1_mp4


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
    fps: int = 24,
    scene: Optional[Dict[str, Any]] = None
) -> Path:
    """
    Diễn hoạt 2 giai đoạn Kinetic Reveal + Live Action Motion:
    - Giai đoạn 1 (52% thời lượng): Truy vết khung xương nét vẽ Topological Skeleton + CRAFT Text Reveal
    - Giai đoạn 2 (48% thời lượng): Diễn hoạt chuyển động sống động (2 mèo đua xe phóng vút, biểu đồ tách lớp, bánh răng xoay, Camera Zoom/Shake) ngay trong cùng 1 luồng FFmpeg pipe siêu tốc!
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = output_dir / f"kinetic_band_b_{uuid.uuid4().hex[:8]}.mp4"
    img_arr = np.array(illustration_img.convert("RGB"))

    total_dur = max(1.8, float(duration))
    has_live_motion = scene is not None and not scene.get("custom_image_path")

    if has_live_motion:
        draw_dur = max(0.95, round(total_dur * 0.52, 2))
        live_dur = max(0.45, total_dur - draw_dur - 0.12)
        live_frames_count = max(10, int(round(live_dur * fps)))
        from ai.kinetic_scene_animator import generate_live_action_frames
        extra_frames = generate_live_action_frames(scene, num_frames=live_frames_count, width=720, height=420)
    else:
        draw_dur = max(1.0, round(total_dur * 0.78, 2))
        extra_frames = None

    animator = get_whiteboard_animator()
    t_start = time.perf_counter()
    animator.render_to_file(
        img_array=img_arr,
        draw_duration=draw_dur,
        total_duration=total_dur,
        output_path=str(out_mp4),
        fps=fps,
        use_nvenc=True,
        extra_frames=extra_frames
    )
    wall_sec = time.perf_counter() - t_start
    logger.info(
        f"Đã render xong 2-Phase Kinetic + Live Motion Band B: {out_mp4.name} "
        f"({total_dur:.2f}s, live_frames={len(extra_frames) if extra_frames else 0}, wall: {wall_sec:.2f}s)"
    )
    return out_mp4


def render_scene_band_b(
    illustration_img: Image.Image,
    duration: float,
    output_dir: Path,
    fps: int = 24,
    show_hand: bool = True,
    whiteboard_mode: str = "3band",
    scene: Optional[Dict[str, Any]] = None
) -> Path:
    """
    Điều phối bộ dựng Band B 2 giai đoạn (Vẽ bảng trắng + Chuyển động thực tế) dựa theo tùy chọn người dùng:
    - Nếu whiteboard_mode == 'kinetic' hoặc show_hand is False: dùng engine Kinetic Reveal + Live Action
    - Nếu whiteboard_mode in ['hand', '3band']: dùng engine Hand-Drawn + Live Action
    """
    mode_clean = (whiteboard_mode or "").lower().strip()
    if mode_clean in ["kinetic", "reveal"] or not show_hand:
        return render_kinetic_band_b(illustration_img, duration, output_dir, fps=fps, scene=scene)
    else:
        return render_hand_drawn_band_b(illustration_img, duration, output_dir, fps=fps, show_hand=True, scene=scene)


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
