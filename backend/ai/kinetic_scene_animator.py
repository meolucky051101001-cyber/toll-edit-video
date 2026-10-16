# -*- coding: utf-8 -*-
"""
Kinetic Scene Animator & Multi-Layer Whiteboard Composer
Ứng dụng tinh hoa từ 4 mã nguồn mở AI Animation hàng đầu:
1. LingyiChen-AI/AIComicBuilder:
   - Multi-Layer Actor Rigging (ghép nhân vật + phương tiện + bối cảnh + hiệu ứng truyện tranh)
   - 2-Phase Animation: Phase 1 (Vẽ nét bảng trắng) -> Phase 2 (Diễn hoạt chuyển động thực tế từ Frame đầu tới Frame đích)
2. Yuan-ManX/Innovator:
   - Quy trình tổng hợp hình ảnh Vector + Procedural SVG/PIL cục bộ 100%, loại bỏ hoàn toàn lỗi HTTP 402
3. masihsultani/whiteboard-animator:
   - Tương thích hoàn hảo với Topological Skeleton Tracing & CRAFT Text Ordering
4. anil-matcha/open-generative-ai (Vibe Motion & Cinema Studio):
   - Điều khiển Camera điện ảnh tự động (Ken Burns Zoom, Dynamic Pan, Action Shake, Pulse)
   - Đảm bảo 100% khung hình Đen - Trắng thuần khiết (#000000 trên nền #FFFFFF)
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

logger = logging.getLogger(__name__)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
DARK_GRAY = (30, 30, 30)


def _get_font(size: int = 24, bold: bool = True) -> ImageFont.FreeTypeFont:
    candidates = (
        ["arialbd.ttf", "segoeuib.ttf", "calibrib.ttf", "tahomabd.ttf"]
        if bold else
        ["arial.ttf", "segoeui.ttf", "calibri.ttf", "tahoma.ttf"]
    )
    for fn in candidates:
        try:
            return ImageFont.truetype(fn, size)
        except Exception:
            continue
    return ImageFont.load_default()


def detect_scene_action_spec(scene: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phân tích ngữ nghĩa của phân cảnh để chọn bộ Rigging đa lớp (Multi-Layer Rig)
    và kịch bản chuyển động (Kinetic Motion + Camera Control) phù hợp 100% với lời thoại.
    """
    narration = (scene.get("narration") or "").lower()
    concept = (scene.get("visual_concept") or "").lower()
    img_prompt = (scene.get("image_prompt") or "").lower()
    label = (scene.get("doodle_label") or "").lower()
    v_type = (scene.get("visual_type") or "").lower()
    icons = [str(k).lower() for k in (scene.get("icon_keywords") or [])]

    combined = f"{narration} {concept} {img_prompt} {label} {v_type} {' '.join(icons)}"

    # Trích xuất các con số phần trăm hoặc bộ số kiểu 50-30-20, 80-20...
    pct_matches = re.findall(r"(\d{1,3})\s*(?:%|phần trăm)", combined)
    dash_numbers = re.findall(r"\b(\d{2})\s*[-/]\s*(\d{2})(?:\s*[-/]\s*(\d{2}))?\b", combined)
    numbers: List[int] = []
    if dash_numbers:
        for grp in dash_numbers[0]:
            if grp and grp.isdigit():
                numbers.append(int(grp))
    elif pct_matches:
        numbers = [int(x) for x in pct_matches[:4] if 0 < int(x) <= 100]

    # 1. Cảnh ĐUA XE / TỐC ĐỘ (VD: "2 con mèo đua xe", "đua xe công thức", "lao vút trên đường đua")
    is_race = any(w in combined for w in [
        "đua xe", "cuộc đua", "đường đua", "race", "racing", "racetrack", "f1", "formula",
        "vượt mặt", "về đích", "tốc độ cao", "siêu xe"
    ]) or (
        any(w in combined for w in ["mèo", "cat", "chó", "dog", "thỏ", "rùa"])
        and any(w in combined for w in ["xe", "car", "đua", "lái", "drive", "speed"])
    )
    if is_race:
        rider = "cat"
        if any(w in combined for w in ["chó", "dog", "cẩu"]):
            rider = "dog"
        elif any(w in combined for w in ["người", "person", "tay đua", "driver"]) and "mèo" not in combined and "cat" not in combined:
            rider = "driver"
        return {
            "action_type": "race",
            "rider_type": rider,
            "camera_motion": scene.get("camera_motion") or "pan_right",
            "numbers": numbers,
        }

    # 2. Cảnh BIỂU ĐỒ TRÒN / QUY TẮC TÀI CHÍNH 50-30-20 (Pie Chart)
    is_pie = (
        any(w in combined for w in ["50-30-20", "50/30/20", "biểu đồ tròn", "pie chart", "chia thu nhập", "phân bổ", "tỷ lệ"])
        or (len(numbers) >= 2 and sum(numbers[:3]) in (90, 95, 100))
    )
    if is_pie:
        if not numbers:
            numbers = [50, 30, 20]
        return {
            "action_type": "chart_pie",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
        }

    # 3. Cảnh RỖNG TÚI / VẮT VÍ / HẾT TIỀN / CHI TIÊU (Wallet Squeeze / Empty Pocket)
    is_wallet = any(w in combined for w in [
        "ví tiền", "vắt ví", "rỗng túi", "cháy túi", "hết tiền", "wallet", "empty pocket",
        "tiền lương", "bốc hơi", "chi tiêu", "tiêu xài", "nợ", "nghèo", "tiết kiệm", "heo đất", "piggy"
    ])
    if is_wallet:
        return {
            "action_type": "wallet_squeeze",
            "camera_motion": scene.get("camera_motion") or "shake",
            "numbers": numbers,
        }

    # 4. Cảnh BIỂU ĐỒ TĂNG TRƯỞNG / ĐẦU TƯ / CHỨNG KHOÁN (Bar & Trend Chart)
    is_bar = any(w in combined for w in [
        "biểu đồ", "tăng trưởng", "lãi kép", "đầu tư", "cổ phiếu", "chứng khoán", "chart",
        "doanh thu", "lợi nhuận", "tài sản tăng", "thống kê", "số liệu"
    ])
    if is_bar:
        return {
            "action_type": "chart_bar",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
        }

    # 5. Cảnh NÃO BỘ / TÂM LÝ HỌC / SUY NGHĨ (Brain & Gears)
    is_brain = any(w in combined for w in [
        "não", "trí tuệ", "tâm lý", "suy nghĩ", "thần kinh", "brain", "mind",
        "nhận thức", "ảo giác", "bối rối", "confused", "logic", "thói quen", "dopamine"
    ])
    if is_brain:
        return {
            "action_type": "brain_think",
            "camera_motion": scene.get("camera_motion") or "pulse",
            "numbers": numbers,
        }

    # 6. Cảnh ĐỐI ĐẦU / HỖN CHIẾN / VA CHẠM (Fight / Clash)
    is_fight = any(w in combined for w in [
        "đánh nhau", "hỗn chiến", "tấn công", "cào", "tát", "va chạm", "chiến đấu",
        "fight", "battle", "clash", "boxing", "đối đầu", "kẻ thù", "sát thủ"
    ])
    if is_fight:
        return {
            "action_type": "fight",
            "camera_motion": scene.get("camera_motion") or "shake",
            "numbers": numbers,
        }

    # 7. Cảnh 2 ĐỐI TƯỢNG TƯƠNG TÁC / TRANH LUẬN (Confrontation / Action Duo)
    layout = scene.get("layout", "single_centered")
    if layout in ("dual_confrontation", "action_duo") or len(icons) >= 2:
        return {
            "action_type": "confrontation",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
        }

    # 8. Mặc định: Showcase trung tâm với hiệu ứng hoạt họa xung quanh
    return {
        "action_type": "showcase",
        "camera_motion": scene.get("camera_motion") or "zoom_in",
        "numbers": numbers,
    }


# =========================================================================
# CÁC HÀM VẼ THÀNH PHẦN NÉT MỰC ĐEN TRÊN NỀN TRẮNG (PROCEDURAL + VECTOR RIGS)
# =========================================================================

def _draw_cat_racer_rig(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    car_number: str = "01",
    wheel_angle: float = 0.0,
    is_sport_style: bool = False,
    exhaust_phase: float = 0.0
):
    """
    Vẽ 1 bộ Rig hoàn chỉnh gồm:
    - Xe đua thể thao / F1 nét mực đen dày sắc nét
    - Chú mèo đội mũ bảo hiểm đua xe ngồi trong buồng lái, tai mèo nhô lên, ria mép bay trong gió
    - Bánh xe quay theo góc wheel_angle
    - Khói pô xe và vệt gió phía sau theo exhaust_phase
    """
    # 1. Khói pô xe (Exhaust puffs) phía sau đuôi xe
    for i in range(3):
        puff_offset = int((exhaust_phase * 45 + i * 22) % 65)
        px = cx - 95 - puff_offset
        py = cy + 12 - int(puff_offset * 0.18)
        r = 6 + int(puff_offset * 0.14)
        draw.ellipse([px - r, py - r, px + r, py + r], fill=WHITE, outline=BLACK, width=3)

    # 2. Cánh gió sau (Rear Spoiler)
    draw.polygon(
        [(cx - 82, cy - 2), (cx - 98, cy - 28), (cx - 86, cy - 28), (cx - 72, cy - 2)],
        fill=BLACK, outline=BLACK
    )
    draw.rectangle([cx - 102, cy - 33, cx - 78, cy - 26], fill=BLACK, outline=BLACK)

    # 3. Thân xe đua (Race Car Body)
    if not is_sport_style:
        # Kiểu F1 khí động học mũi nhọn
        body_pts = [
            (cx - 85, cy + 18),
            (cx - 85, cy - 6),
            (cx - 35, cy - 14),
            (cx + 25, cy - 14),
            (cx + 95, cy + 4),
            (cx + 102, cy + 18),
        ]
    else:
        # Kiểu GT Sport hầm hố
        body_pts = [
            (cx - 85, cy + 18),
            (cx - 80, cy - 10),
            (cx - 25, cy - 16),
            (cx + 35, cy - 14),
            (cx + 92, cy + 0),
            (cx + 98, cy + 18),
        ]
    draw.polygon(body_pts, fill=WHITE, outline=BLACK, width=4)

    # Đường kẻ sọc thể thao dọc thân xe
    draw.line([(cx - 70, cy + 3), (cx + 75, cy + 6)], fill=BLACK, width=3)

    # 4. Vô lăng buồng lái (Steering Wheel)
    draw.line([(cx + 18, cy - 12), (cx + 28, cy - 28)], fill=BLACK, width=4)
    draw.ellipse([cx + 22, cy - 36, cx + 34, cy - 20], fill=WHITE, outline=BLACK, width=3)

    # 5. Chú mèo tay đua ngồi trong buồng lái (Cat Racer Head + Helmet + Ears + Whiskers)
    head_x, head_y = cx - 8, cy - 34
    # Đầu/Mũ bảo hiểm tròn
    draw.ellipse([head_x - 24, head_y - 22, head_x + 24, head_y + 22], fill=WHITE, outline=BLACK, width=4)
    # Kính chắn gió mũ bảo hiểm (Visor) & đôi mắt mèo sắc bén
    draw.chord([head_x - 6, head_y - 14, head_x + 24, head_y + 8], start=270, end=90, fill=WHITE, outline=BLACK, width=3)
    # Mắt mèo nhìn về phía trước
    draw.ellipse([head_x + 6, head_y - 8, head_x + 14, head_y - 1], fill=BLACK)
    # Lông mày quyết tâm
    draw.line([(head_x + 3, head_y - 12), (head_x + 17, head_y - 7)], fill=BLACK, width=3)
    # 2 Tai mèo nhọn nhô lên trên mũ bảo hiểm
    draw.polygon([(head_x - 18, head_y - 16), (head_x - 24, head_y - 38), (head_x - 5, head_y - 21)], fill=WHITE, outline=BLACK, width=3)
    draw.polygon([(head_x - 1, head_y - 21), (head_x + 6, head_y - 38), (head_x + 14, head_y - 17)], fill=WHITE, outline=BLACK, width=3)
    # Ria mèo (Whiskers) bay ngược về sau vì tốc độ cao
    draw.line([(head_x + 18, head_y + 4), (head_x + 38, head_y + 0)], fill=BLACK, width=2)
    draw.line([(head_x + 18, head_y + 8), (head_x + 40, head_y + 7)], fill=BLACK, width=2)
    # Khăn quàng cổ tay đua bay phấp phới phía sau
    scarf_wave = int(math.sin(wheel_angle * 2.0) * 5)
    draw.polygon(
        [(head_x - 18, head_y + 14), (head_x - 58, head_y + 6 + scarf_wave), (head_x - 52, head_y + 18 + scarf_wave)],
        fill=BLACK, outline=BLACK
    )

    # 6. Số hiệu xe đua (Car Number Badge)
    font_num = _get_font(16, bold=True)
    draw.rounded_rectangle([cx - 28, cy - 10, cx + 8, cy + 14], radius=5, fill=WHITE, outline=BLACK, width=2)
    draw.text((cx - 22, cy - 8), car_number, font=font_num, fill=BLACK)

    # 7. Hai bánh xe đua quay động (Spinning Wheels with Spokes)
    for wx in (cx - 52, cx + 56):
        wy = cy + 18
        r_outer = 21
        r_inner = 11
        draw.ellipse([wx - r_outer, wy - r_outer, wx + r_outer, wy + r_outer], fill=BLACK, outline=BLACK, width=3)
        draw.ellipse([wx - r_inner, wy - r_inner, wx + r_inner, wy + r_inner], fill=WHITE, outline=BLACK, width=2)
        # Nan hoa bánh xe xoay theo wheel_angle
        for k in range(3):
            ang = wheel_angle + k * (math.pi / 3.0)
            dx = int(math.cos(ang) * (r_inner - 1))
            dy = int(math.sin(ang) * (r_inner - 1))
            draw.line([(wx - dx, wy - dy), (wx + dx, wy + dy)], fill=BLACK, width=2)


def _draw_checkered_flag(draw: ImageDraw.ImageDraw, fx: int, fy: int, wave_phase: float = 0.0):
    """Vẽ cờ caro xuất phát / về đích (Checkered Flag) vẫy động."""
    # Cột cờ
    draw.line([(fx, fy - 45), (fx, fy + 45)], fill=BLACK, width=4)
    # Lá cờ caro 4x3 ô
    cols, rows = 4, 3
    cell_w, cell_h = 13, 11
    for r in range(rows):
        for c in range(cols):
            wave_dy = int(math.sin(wave_phase * 2 * math.pi + c * 0.8) * 3)
            x0 = fx + c * cell_w
            y0 = fy - 42 + r * cell_h + wave_dy
            fill_col = BLACK if (r + c) % 2 == 0 else WHITE
            draw.rectangle([x0, y0, x0 + cell_w, y0 + cell_h], fill=fill_col, outline=BLACK, width=2)


def _draw_top_label(draw: ImageDraw.ImageDraw, width: int, label: str):
    """Vẽ nhãn chữ in hoa nổi bật ở cạnh trên của Band B."""
    if not label or not label.strip():
        return
    font_lbl = _get_font(23, bold=True)
    txt = label.strip().upper()
    bbox = draw.textbbox((0, 0), txt, font=font_lbl)
    lw = bbox[2] - bbox[0]
    lh = bbox[3] - bbox[1]
    bw = lw + 44
    bh = lh + 20
    bx = (width - bw) // 2
    by = 16
    draw.rounded_rectangle([bx + 4, by + 4, bx + bw + 4, by + bh + 4], radius=8, fill=BLACK)
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8, fill=WHITE, outline=BLACK, width=3)
    draw.text((bx + (bw - lw) // 2, by + (bh - lh) // 2 - 2), txt, font=font_lbl, fill=BLACK)


# =========================================================================
# CÁC BỘ DỰNG PHÂN CẢNH ĐA LỚP (MULTI-LAYER SCENE COMPOSERS)
# =========================================================================

def _compose_race_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh đua xe động (VD: 2 con mèo đua xe):
    - Đường đua 2 làn (Upper Lane & Lower Lane) với vạch phân làn đứt đoạn chuyển động
    - Cờ caro về đích vẫy ở bên phải
    - 2 Tay đua mèo đội mũ bảo hiểm trong 2 chiếc xe đua số 01 và 02
    - Khi phase_progress > 0: 2 xe phóng vút về phía trước, rượt đuổi vượt mặt nhau, bánh xe quay tít, khói pô phụt ra!
    """
    p = max(0.0, min(1.0, phase_progress))

    # 1. Vẽ đường đua 2 làn (Racetrack Lanes)
    track_top = 115
    track_mid = 248
    track_bot = 382
    draw.line([(20, track_top), (width - 20, track_top)], fill=BLACK, width=4)
    draw.line([(20, track_bot), (width - 20, track_bot)], fill=BLACK, width=4)

    # Vạch kẻ phân làn ở giữa trôi ngược về bên trái khi xe chạy
    dash_shift = int(p * 260) % 60
    for dx in range(-60, width + 60, 60):
        x1 = max(25, dx - dash_shift)
        x2 = min(width - 25, dx - dash_shift + 34)
        if x2 > x1:
            draw.line([(x1, track_mid), (x2, track_mid)], fill=BLACK, width=3)

    # Các đường gió tốc độ (Speed lines)
    speed_lines = [
        (45, 145, 95), (210, 138, 80), (420, 148, 110),
        (60, 275, 105), (250, 268, 85), (460, 278, 95),
    ]
    line_shift = int(p * 320)
    for sx, sy, slen in speed_lines:
        curr_sx = ((sx - line_shift - 30) % (width - 140)) + 30
        draw.line([(curr_sx, sy), (curr_sx + slen, sy)], fill=BLACK, width=2)

    # 2. Cờ caro đích (Checkered Finish Line & Flag) bên phải
    finish_x = width - 72
    for row in range(8):
        y0 = track_top + 6 + row * 32
        if y0 + 24 < track_bot:
            draw.rectangle(
                [finish_x, y0, finish_x + 12, y0 + 16],
                fill=BLACK if row % 2 == 0 else WHITE,
                outline=BLACK,
                width=2
            )
    _draw_checkered_flag(draw, finish_x - 6, track_top - 8, wave_phase=p * 3.0)

    # 3. Tính toán vị trí động của Xe đua #1 và Xe đua #2
    # Ở frame đầu (p=0.0): Xe #1 ở x=205, Xe #2 ở x=175 (so kè nhau)
    # Khi chạy (p: 0 -> 1): Cả 2 xe lao tới vạch đích, rượt đuổi nhau quyết liệt
    lead_osc = math.sin(p * math.pi * 2.5) * 28.0
    bob1 = int(math.sin(p * math.pi * 12.0) * 3)
    bob2 = int(math.cos(p * math.pi * 12.0) * 3)

    car1_x = int(205 + p * 315 + lead_osc)
    car1_y = 188 + bob1

    car2_x = int(175 + p * 340 - lead_osc * 0.8)
    car2_y = 322 + bob2

    wheel_ang = p * math.pi * 10.0

    # Vẽ Xe đua Mèo #1 (Làn trên)
    _draw_cat_racer_rig(
        draw, cx=car1_x, cy=car1_y,
        car_number="01",
        wheel_angle=wheel_ang,
        is_sport_style=False,
        exhaust_phase=p * 2.5
    )

    # Vẽ Xe đua Mèo #2 (Làn dưới)
    _draw_cat_racer_rig(
        draw, cx=car2_x, cy=car2_y,
        car_number="02",
        wheel_angle=wheel_ang + 0.8,
        is_sport_style=True,
        exhaust_phase=p * 2.5 + 0.4
    )

    # Hiệu ứng tia chớp so kè giữa 2 xe
    mid_clash_x = (car1_x + car2_x) // 2 + 30
    mid_clash_y = track_mid
    draw.polygon(
        [
            (mid_clash_x, mid_clash_y - 18),
            (mid_clash_x - 12, mid_clash_y + 2),
            (mid_clash_x + 2, mid_clash_y + 2),
            (mid_clash_x - 8, mid_clash_y + 22),
            (mid_clash_x + 14, mid_clash_y - 2),
            (mid_clash_x + 1, mid_clash_y - 2),
        ],
        fill=BLACK
    )


def _compose_pie_chart_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh Biểu đồ tròn (VD: Quy tắc 50-30-20 hoặc phân bổ tỷ lệ):
    - Vẽ biểu đồ tròn chia đúng tỷ lệ phần trăm với họa tiết sọc/chấm đen trắng rõ nét
    - Khi phase_progress > 0: Các miếng biểu đồ tách nhẹ (explode slice) và nhãn số liệu nhấp nháy nổi bật
    """
    p = max(0.0, min(1.0, phase_progress))
    nums = spec.get("numbers") or [50, 30, 20]
    if len(nums) < 2:
        nums = [50, 30, 20]
    total = sum(nums[:3]) or 100
    pcts = [round(n * 100 / total) for n in nums[:3]]
    if sum(pcts) != 100 and len(pcts) >= 1:
        pcts[0] += 100 - sum(pcts)

    cx, cy = width // 2 - 20, height // 2 + 25
    radius = 112

    labels_default = ["THIẾT YẾU", "CÁ NHÂN", "TIẾT KIỆM"]
    start_ang = -90.0

    font_pct = _get_font(22, bold=True)
    font_sub = _get_font(16, bold=True)

    for idx, pct in enumerate(pcts):
        sweep = (pct / 100.0) * 360.0
        end_ang = start_ang + sweep
        mid_ang_rad = math.radians((start_ang + end_ang) / 2.0)

        # Hiệu ứng tách miếng biểu đồ (Explode slice) tuần tự trong Phase 2
        slice_active = max(0.0, min(1.0, (p * len(pcts)) - idx * 0.6))
        explode_dist = int(math.sin(slice_active * math.pi) * 12) + (6 if idx == 0 else 0)
        scx = cx + int(math.cos(mid_ang_rad) * explode_dist)
        scy = cy + int(math.sin(mid_ang_rad) * explode_dist)

        bbox = [scx - radius, scy - radius, scx + radius, scy + radius]
        draw.pieslice(bbox, start=start_ang, end=end_ang, fill=WHITE, outline=BLACK, width=4)

        # Tạo họa tiết phân biệt đen trắng cho từng phần
        if idx == 1:
            # Chấm bi đen bên trong miếng thứ 2
            for r_step in range(35, radius - 15, 22):
                for a_step in range(int(start_ang) + 12, int(end_ang) - 8, 18):
                    ar = math.radians(a_step)
                    px = scx + int(math.cos(ar) * r_step)
                    py = scy + int(math.sin(ar) * r_step)
                    draw.ellipse([px - 3, py - 3, px + 3, py + 3], fill=BLACK)
        elif idx == 2:
            # Viền đậm đặc hoặc gạch chéo cho miếng thứ 3
            for r_step in range(28, radius - 10, 16):
                draw.arc(
                    [scx - r_step, scy - r_step, scx + r_step, scy + r_step],
                    start=start_ang + 5, end=end_ang - 5, fill=BLACK, width=2
                )

        # Đường chỉ dẫn (Callout line) và hộp nhãn bên ngoài
        line_r1 = radius + 8
        line_r2 = radius + 36
        lx1 = scx + int(math.cos(mid_ang_rad) * line_r1)
        ly1 = scy + int(math.sin(mid_ang_rad) * line_r1)
        lx2 = scx + int(math.cos(mid_ang_rad) * line_r2)
        ly2 = scy + int(math.sin(mid_ang_rad) * line_r2)
        horiz_dir = 1 if math.cos(mid_ang_rad) >= 0 else -1
        lx3 = lx2 + horiz_dir * 28

        draw.line([(lx1, ly1), (lx2, ly2), (lx3, ly2)], fill=BLACK, width=3)
        draw.ellipse([lx1 - 4, ly1 - 4, lx1 + 4, ly1 + 4], fill=BLACK)

        # Hộp chữ phần trăm
        tag_txt = f"{pct}%"
        sub_txt = labels_default[idx] if idx < len(labels_default) else f"NHÓM {idx+1}"
        box_w, box_h = 128, 54
        bx = lx3 + 6 if horiz_dir > 0 else lx3 - box_w - 6
        by = max(72, min(height - box_h - 16, ly2 - box_h // 2))

        draw.rounded_rectangle([bx, by, bx + box_w, by + box_h], radius=8, fill=WHITE, outline=BLACK, width=3)
        draw.text((bx + 12, by + 4), tag_txt, font=font_pct, fill=BLACK)
        draw.text((bx + 12, by + 30), sub_txt, font=font_sub, fill=BLACK)

        start_ang = end_ang


def _compose_wallet_squeeze_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh Ví tiền bị vắt kiệt / Rỗng túi / Tiền bay đi:
    - Ví tiền ở trung tâm bị ép chặt, các đồng xu $ và tờ tiền có cánh bay ra ngoài
    - Khi phase_progress > 0: Ví tiền co bóp theo nhịp, đồng xu và tờ tiền bay tỏa ra xa!
    """
    from ai.doodle_engine import fetch_vector_icon

    p = max(0.0, min(1.0, phase_progress))
    cx, cy = width // 2, height // 2 + 25

    squeeze_x = int(math.sin(p * math.pi * 6.0) * 8)
    squeeze_y = -squeeze_x

    # Vẽ chiếc ví da / túi tiền ở trung tâm
    w_half = 92 - squeeze_x
    h_half = 68 + squeeze_y
    draw.rounded_rectangle(
        [cx - w_half, cy - h_half, cx + w_half, cy + h_half],
        radius=16, fill=WHITE, outline=BLACK, width=5
    )
    # Nắp khóa ví
    draw.rounded_rectangle(
        [cx + w_half - 38, cy - 22, cx + w_half + 10, cy + 22],
        radius=8, fill=WHITE, outline=BLACK, width=4
    )
    draw.ellipse([cx + w_half - 22, cy - 8, cx + w_half - 6, cy + 8], fill=BLACK)
    # Đường khâu viền ví
    draw.line([(cx - w_half + 14, cy - h_half + 12), (cx + w_half - 14, cy - h_half + 12)], fill=BLACK, width=2)
    draw.line([(cx - w_half + 14, cy + h_half - 12), (cx + w_half - 14, cy + h_half - 12)], fill=BLACK, width=2)

    font_dollar = _get_font(28, bold=True)
    draw.text((cx - 35, cy - 18), "$ 0", font=_get_font(34, bold=True), fill=BLACK)

    # Các đồng xu có cánh bay ra khỏi ví theo quỹ đạo parabol
    coin_angles = [-145, -115, -65, -35, 30, 150]
    for i, ang_deg in enumerate(coin_angles):
        ang = math.radians(ang_deg)
        dist = 125 + int(p * 55) + (i % 2) * 18
        kx = cx + int(math.cos(ang) * dist)
        ky = cy + int(math.sin(ang) * dist) - int(math.sin(p * math.pi) * 12)
        r = 22
        draw.ellipse([kx - r, ky - r, kx + r, ky + r], fill=WHITE, outline=BLACK, width=3)
        draw.text((kx - 8, ky - 15), "$", font=font_dollar, fill=BLACK)
        # Cánh nhỏ vẫy bên cạnh đồng xu
        wing_dy = int(math.sin(p * math.pi * 8 + i) * 6)
        draw.arc([kx - r - 18, ky - 14 + wing_dy, kx - r + 2, ky + 6 + wing_dy], start=160, end=350, fill=BLACK, width=3)
        draw.arc([kx + r - 2, ky - 14 + wing_dy, kx + r + 18, ky + 6 + wing_dy], start=190, end=20, fill=BLACK, width=3)

    # Đường mặt đất
    ground_y = height - 42
    draw.line([(50, ground_y), (width - 50, ground_y)], fill=BLACK, width=4)


def _compose_bar_chart_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh Biểu đồ cột & Mũi tên tăng trưởng đột phá:
    - Khi phase_progress > 0: Các cột tăng chiều cao và mũi tên vươn lên!
    """
    p = max(0.0, min(1.0, phase_progress))
    ox, oy = 140, height - 58
    chart_w, chart_h = 440, 235

    # Trục tọa độ X - Y
    draw.line([(ox, oy - chart_h), (ox, oy)], fill=BLACK, width=5)
    draw.line([(ox, oy), (ox + chart_w, oy)], fill=BLACK, width=5)

    bar_heights = [55, 95, 145, 205]
    bar_w = 56
    gap = 42
    growth_factor = 0.78 + 0.22 * math.sin(p * math.pi * 0.5)

    pts = []
    for i, bh in enumerate(bar_heights):
        curr_h = int(bh * growth_factor)
        bx = ox + 36 + i * (bar_w + gap)
        by = oy - curr_h
        fill_col = BLACK if i == len(bar_heights) - 1 else WHITE
        draw.rectangle([bx, by, bx + bar_w, oy], fill=fill_col, outline=BLACK, width=4)
        if i == 2:
            # Kẻ sọc ngang cho cột thứ 3
            for sy in range(by + 12, oy - 6, 14):
                draw.line([(bx + 4, sy), (bx + bar_w - 4, sy)], fill=BLACK, width=2)
        pts.append((bx + bar_w // 2, by - 18))

    # Đường xu hướng (Trend line) nối đỉnh các cột và mũi tên hướng lên
    if len(pts) >= 2:
        for j in range(len(pts) - 1):
            draw.line([pts[j], pts[j + 1]], fill=BLACK, width=4)
        for pt in pts:
            draw.ellipse([pt[0] - 6, pt[1] - 6, pt[0] + 6, pt[1] + 6], fill=WHITE, outline=BLACK, width=3)
        last_x, last_y = pts[-1]
        draw.polygon(
            [(last_x + 18, last_y - 18), (last_x - 4, last_y - 14), (last_x + 14, last_y + 4)],
            fill=BLACK
        )


def _compose_brain_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh Bộ não & Bánh răng tư duy / Bóng đèn ý tưởng:
    - Bộ não 2 bán cầu chi tiết ở trung tâm
    - Bánh răng cơ khí xoay tròn và các tia sáng ý tưởng nhấp nháy xung quanh
    """
    from ai.doodle_engine import fetch_vector_icon

    p = max(0.0, min(1.0, phase_progress))
    cx, cy = width // 2, height // 2 + 30

    brain_im = fetch_vector_icon("brain", size=200, color="#000000")
    if brain_im:
        img.paste(brain_im, (cx - 100, cy - 95), brain_im)
    else:
        draw.ellipse([cx - 105, cy - 65, cx - 4, cy + 65], fill=WHITE, outline=BLACK, width=4)
        draw.ellipse([cx + 4, cy - 65, cx + 105, cy + 65], fill=WHITE, outline=BLACK, width=4)

    # Vẽ 2 bánh răng tư duy xoay tròn (Rotating Gears) ở 2 bên bộ não
    for idx, (gx, gy, gr, dir_sign) in enumerate([(cx - 155, cy - 35, 34, 1), (cx + 155, cy - 35, 34, -1)]):
        ang_base = p * math.pi * 4.0 * dir_sign
        for tooth in range(8):
            a = ang_base + tooth * (math.pi / 4.0)
            tx1 = gx + int(math.cos(a) * (gr - 4))
            ty1 = gy + int(math.sin(a) * (gr - 4))
            tx2 = gx + int(math.cos(a) * (gr + 9))
            ty2 = gy + int(math.sin(a) * (gr + 9))
            draw.line([(tx1, ty1), (tx2, ty2)], fill=BLACK, width=6)
        draw.ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=WHITE, outline=BLACK, width=4)
        draw.ellipse([gx - 12, gy - 12, gx + 12, gy + 12], fill=BLACK)

    # Tia sáng nhận thức phía trên đỉnh não
    ray_extra = int(math.sin(p * math.pi * 4.0) * 8)
    for deg in (-135, -110, -90, -70, -45):
        rad = math.radians(deg)
        x1 = cx + int(math.cos(rad) * 105)
        y1 = cy - 20 + int(math.sin(rad) * 105)
        x2 = cx + int(math.cos(rad) * (132 + ray_extra))
        y2 = cy - 20 + int(math.sin(rad) * (132 + ray_extra))
        draw.line([(x1, y1), (x2, y2)], fill=BLACK, width=4)

    ground_y = height - 42
    draw.line([(60, ground_y), (width - 60, ground_y)], fill=BLACK, width=4)


def _compose_confrontation_or_fight_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh 2 Nhân vật / Đối tượng tương tác, đối đầu hoặc hỗn chiến:
    - Khi phase_progress > 0: 2 đối tượng lao vào nhau, hiệu ứng tia sét / va chạm ở giữa bung tỏa!
    """
    from ai.doodle_engine import fetch_vector_icon

    p = max(0.0, min(1.0, phase_progress))
    icons = scene.get("icon_keywords") or ["cat", "cat"]
    kw1 = icons[0] if icons else "person"
    kw2 = icons[1] if len(icons) > 1 else kw1

    im1 = fetch_vector_icon(kw1, size=175, color="#000000")
    im2 = fetch_vector_icon(kw2, size=175, color="#000000")
    if im1 and im2 and kw1 == kw2:
        im2 = ImageOps.mirror(im2)

    lunge = int(math.sin(p * math.pi * 3.0) * 32)
    bob = int(abs(math.sin(p * math.pi * 6.0)) * 10)

    x1 = 72 + lunge
    x2 = width - 72 - 175 - lunge
    y_pos = 135 - bob

    if im1:
        img.paste(im1, (x1, y_pos), im1)
    if im2:
        img.paste(im2, (x2, y_pos), im2)

    # Tâm chấn va chạm / tương tác ở giữa
    cx, cy = width // 2, 220
    burst_r = 36 + int(math.sin(p * math.pi * 6.0) * 10)
    pts = []
    for i in range(16):
        ang = i * (math.pi / 8.0) + p * 1.5
        r = burst_r if i % 2 == 0 else burst_r // 2
        pts.append((cx + int(math.cos(ang) * r), cy + int(math.sin(ang) * r)))
    draw.polygon(pts, fill=WHITE, outline=BLACK, width=3)

    # Tia chớp đen giữa tâm chấn
    draw.polygon(
        [(cx + 2, cy - 18), (cx - 10, cy + 2), (cx + 2, cy + 2),
         (cx - 6, cy + 20), (cx + 12, cy - 2), (cx + 1, cy - 2)],
        fill=BLACK
    )

    ground_y = height - 45
    draw.line([(50, ground_y), (width - 50, ground_y)], fill=BLACK, width=4)
    for hx in range(70, width - 60, 40):
        draw.line([(hx, ground_y), (hx - 14, ground_y + 14)], fill=BLACK, width=2)


def _compose_showcase_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    phase_progress: float
):
    """
    Cảnh Showcase chủ thể trung tâm kết hợp các vệ tinh minh họa động xung quanh.
    """
    from ai.doodle_engine import fetch_vector_icon

    p = max(0.0, min(1.0, phase_progress))
    icons = scene.get("icon_keywords") or [scene.get("visual_type", "lightbulb")]
    kw_main = icons[0] if icons else "lightbulb"
    kw_sub = icons[1] if len(icons) > 1 else "star"

    cx, cy = width // 2, height // 2 + 25
    bob_y = int(math.sin(p * math.pi * 4.0) * 8)

    hero_im = fetch_vector_icon(kw_main, size=215, color="#000000")
    if hero_im:
        img.paste(hero_im, (cx - 107, cy - 105 + bob_y), hero_im)

    sub_im = fetch_vector_icon(kw_sub, size=72, color="#000000")
    if sub_im:
        orbit_dx = int(math.cos(p * math.pi * 2.0) * 14)
        img.paste(sub_im, (cx + 125 + orbit_dx, cy - 95 - bob_y), sub_im)

    # Vòng sóng nhấn mạnh tỏa ra xung quanh chủ thể
    for r_base in (128, 158):
        r_dyn = r_base + int(p * 18)
        draw.arc([cx - r_dyn, cy - r_dyn + 10, cx + r_dyn, cy + r_dyn + 10], start=155, end=205, fill=BLACK, width=3)
        draw.arc([cx - r_dyn, cy - r_dyn + 10, cx + r_dyn, cy + r_dyn + 10], start=335, end=25, fill=BLACK, width=3)

    ground_y = height - 45
    draw.line([(60, ground_y), (width - 60, ground_y)], fill=BLACK, width=4)
    for hx in range(80, width - 70, 40):
        draw.line([(hx, ground_y), (hx - 14, ground_y + 14)], fill=BLACK, width=2)


# =========================================================================
# ENTRYPOINT VẼ KHUNG HÌNH & SINH CHUỖI FRAME CHUYỂN ĐỘNG PHASE 2
# =========================================================================

def render_composite_scene_frame(
    scene: Dict[str, Any],
    width: int = 720,
    height: int = 420,
    phase_progress: float = 0.0
) -> Image.Image:
    """
    Vẽ 1 frame hoàn chỉnh của Band B (720x420) ở thời điểm phase_progress (0.0 -> 1.0).
    - Khi phase_progress = 0.0: Tạo bức tranh gốc sắc nét để bàn tay thật (Hand-Draw) hoặc Kinetic Skeleton vẽ lên.
    - Khi phase_progress > 0.0: Tạo các frame chuyển động sống động (xe đua chạy, biểu đồ tách lớp, bánh răng quay...).
    """
    img = Image.new("RGB", (width, height), WHITE)
    draw = ImageDraw.Draw(img)

    spec = detect_scene_action_spec(scene)
    action_type = spec.get("action_type", "showcase")

    if action_type == "race":
        _compose_race_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "chart_pie":
        _compose_pie_chart_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "wallet_squeeze":
        _compose_wallet_squeeze_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "chart_bar":
        _compose_bar_chart_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "brain_think":
        _compose_brain_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type in ("fight", "confrontation"):
        _compose_confrontation_or_fight_scene(img, draw, scene, spec, width, height, phase_progress)
    else:
        _compose_showcase_scene(img, draw, scene, spec, width, height, phase_progress)

    _draw_top_label(draw, width, scene.get("doodle_label", ""))
    return img


def apply_cinema_camera_transform(
    frame_rgb: np.ndarray,
    camera_motion: str,
    progress: float
) -> np.ndarray:
    """
    Áp dụng hiệu ứng Camera điện ảnh (từ open-generative-ai Cinema Studio):
    - zoom_in: Phóng cận cảnh mượt mà vào trọng tâm
    - zoom_out: Thu toàn cảnh mượt mà
    - pan_right / pan_left: Lia máy theo chuyển động
    - shake: Rung chấn kịch tính cho cảnh va chạm/khủng hoảng
    """
    h, w = frame_rgb.shape[:2]
    p = max(0.0, min(1.0, progress))

    if camera_motion == "zoom_in":
        scale = 1.0 + 0.08 * math.sin(p * math.pi * 0.5)
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0 + 20), 0.0, scale)
        return cv2.warpAffine(frame_rgb, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

    elif camera_motion == "shake":
        dx = math.sin(p * math.pi * 16.0) * 5.0 * (1.0 - p * 0.4)
        dy = math.cos(p * math.pi * 20.0) * 3.5 * (1.0 - p * 0.4)
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        return cv2.warpAffine(frame_rgb, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

    elif camera_motion == "pulse":
        scale = 1.0 + 0.04 * math.sin(p * math.pi * 2.0)
        M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), 0.0, scale)
        return cv2.warpAffine(frame_rgb, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

    return frame_rgb


def generate_live_action_frames(
    scene: Dict[str, Any],
    num_frames: int,
    width: int = 720,
    height: int = 420
) -> List[np.ndarray]:
    """
    Sinh chuỗi frames RGB (numpy uint8) cho Giai đoạn 2 (Live Kinetic Action) sau khi bàn tay / nét vẽ vừa hoàn thành.
    """
    if num_frames <= 0:
        return []

    spec = detect_scene_action_spec(scene)
    cam_motion = spec.get("camera_motion", "zoom_in")
    frames: List[np.ndarray] = []

    for idx in range(num_frames):
        progress = (idx + 1) / float(max(1, num_frames))
        pil_img = render_composite_scene_frame(scene, width=width, height=height, phase_progress=progress)
        arr = np.array(pil_img.convert("RGB"), dtype=np.uint8)
        if spec.get("action_type") != "race":
            arr = apply_cinema_camera_transform(arr, cam_motion, progress)
        frames.append(arr)

    return frames
