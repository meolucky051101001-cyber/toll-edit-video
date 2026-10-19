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
        numbers = list(dict.fromkeys(numbers))  # khử số lặp (VD "90%" xuất hiện 3 lần trong 1 cảnh)
    has_cat_any = ("mèo" in combined) or ("cat" in combined) or any(k == "cat" for k in icons)

    def _word_hit(text: str, word: str) -> bool:
        """Khớp từ nguyên vẹn — tránh 'người xem' dính chữ 'xe'."""
        return re.search(rf"(?<![0-9A-Za-zÀ-ỹ]){re.escape(word)}(?![0-9A-Za-zÀ-ỹ])", text) is not None

    # 1. Cảnh ĐUA XE / TỐC ĐỘ (VD: "2 con mèo đua xe", "đua xe công thức", "lao vút trên đường đua")
    is_race = any(w in combined for w in [
        "đua xe", "cuộc đua", "đường đua", "race", "racing", "racetrack", "f1", "formula",
        "vượt mặt", "về đích", "tốc độ cao", "siêu xe"
    ]) or (
        any(w in combined for w in ["mèo", "cat", "chó", "dog", "thỏ", "rùa"])
        and (any(w in combined for w in ["đua", "lái", "drive", "speed"])
             or _word_hit(combined, "xe") or _word_hit(combined, "car"))
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
        if len(numbers) == 1 and 0 < numbers[0] < 100:
            numbers = [numbers[0], 100 - numbers[0]]
        return {
            "action_type": "chart_pie",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
            "pie_cats": has_cat_any,
            "pie_labels": (["GIẢ VỜ", "ĐÁNH THẬT"] if "dọa" in combined else None),
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

    # 4b. Cảnh KÍNH PHÓNG ĐẠI SOI MÈO (Microscope) — ưu tiên trước cảnh "não/tâm lý"
    is_microscope = has_cat_any and (
        v_type == "microscope"
        or any(w in combined for w in ["kính phóng đại", "phóng đại", "microscope", "soi kỹ", "soi vào"])
    )
    if is_microscope:
        return {
            "action_type": "microscope",
            "camera_motion": scene.get("camera_motion") or "pulse",
            "numbers": numbers,
        }

    # 5. Cảnh NÃO BỘ / TÂM LÝ HỌC / SUY NGHĨ (Brain & Gears)
    is_brain = any(w in combined for w in [
        "não", "trí tuệ", "tâm lý", "suy nghĩ", "thần kinh", "brain", "mind",
        "nhận thức", "ảo giác", "bối rối", "confused", "logic", "thói quen", "dopamine"
    ])
    if is_brain:
        duel = any(w in combined for w in ["hai bộ não", "2 bộ não", "tia chớp", "đấu trí"])
        return {
            "action_type": "brain_think",
            "camera_motion": scene.get("camera_motion") or "pulse",
            "numbers": numbers,
            "brain_variant": "duel" if duel else "single",
        }

    # 5b. Cảnh MÈO + ĐĨA THỨC ĂN (gạt đĩa sang bên chỉ về góc nắng)
    if has_cat_any and any(w in combined for w in ["đĩa thức ăn", "miếng ăn", "gạt đĩa", "gạt đi", "bát ăn"]):
        return {
            "action_type": "cat_food",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
        }

    # 5c. Cảnh MÈO RÚT LUI / TẮM NẮNG (nhường lại góc nắng — kèm đồng hồ 30 phút)
    if has_cat_any and any(w in combined for w in ["rút lui", "lẳng lặng", "tắm nắng", "nằm dài", "nhường lại", "đi ra xa"]):
        return {
            "action_type": "cat_retreat",
            "camera_motion": scene.get("camera_motion") or "zoom_in",
            "numbers": numbers,
            "cat_mode": "retreat",
        }

    # 5d. Cảnh MÈO MỜI BÌNH LUẬN / CHIA SẺ (CTA khung bình luận)
    if has_cat_any and any(w in combined for w in ["bình luận", "comment", "chia sẻ", "khung bình luận"]):
        return {
            "action_type": "cat_comment",
            "camera_motion": scene.get("camera_motion") or "pan_right",
            "numbers": numbers,
        }

    # 6. Cảnh ĐỐI ĐẦU / HỖN CHIẾN / VA CHẠM (Fight / Clash)
    is_fight = any(w in combined for w in [
        "đánh nhau", "hỗn chiến", "tấn công", "cào", "tát", "va chạm", "chiến đấu",
        "fight", "battle", "clash", "boxing", "đối đầu", "kẻ thù", "sát thủ",
        "đe dọa", "đại chiến", "nảy lửa", "so găng", "gầm gừ", "gầm ghè",
    ])
    if is_fight:
        spec_out = {
            "action_type": "fight",
            "camera_motion": scene.get("camera_motion") or "shake",
            "numbers": numbers,
        }
        if has_cat_any:
            if any(w in combined for w in ["trượt", "hụt", "vụng về"]):
                spec_out["cat_mode"] = "sloppy"
            elif any(w in combined for w in ["đại chiến", "nảy lửa", "đánh nhau", "hỗn chiến"]):
                spec_out["cat_mode"] = "battle"
            elif any(w in combined for w in ["dọa", "giả vờ", "thủ thế", "xù lông", "gườm"]):
                spec_out["cat_mode"] = "threat"
            else:
                spec_out["cat_mode"] = "battle"
        return spec_out

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


def _draw_cat_fighter_rig(
    facing: int = 1,
    variant: str = "tabby",
    attack_phase: float = 0.0,
    lean: float = 0.0,
    tail_flick: float = 0.0,
    claw_power: float = 0.0,
    ear_flick: float = 0.0,
    shout: float = 0.0
) -> Image.Image:
    """
    Vẽ 1 chiến binh mèo nét mực đen-trắng cho cảnh HỖN CHIẾN MÈO (không bàn tay vẽ):
    - variant='tabby': mèo vàng (thân trắng viền đen + vằn lưng/đuôi/trán)
    - variant='black': mèo mun (thân đen, viền trắng tách khối, mắt/mũi/miệng trắng)
    - attack_phase: 0.0 = rút chân sau lưng (thủ thế), 1.0 = tát hết tầm tới trước
    - lean: độ nghiêng thân theo cú lao (dương = ngả trái / ngược kim đồng hồ)
    - tail_flick: -1..1 vẫy đuôi
    - claw_power: 0..1 móng vuốt bung (đạo diễn trận đấu điều khiển)
    - ear_flick: 0..1 tai dẹp sát đầu vì căng thẳng
    - shout: 0..1 độ há miệng gầm gừ
    Trả về sprite RGBA 240x224, chân chạm mốc y=208 (để paste theo mặt đất).
    """
    W, H, FEET_Y = 240, 224, 208
    spr = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dr = ImageDraw.Draw(spr)

    is_black = variant == "black"
    body = BLACK if is_black else WHITE
    accent = WHITE if is_black else BLACK
    edge = WHITE if is_black else BLACK  # viền tách khối: mun viền trắng, vàng viền đen

    def segment(p1, p2, w):
        """Chi/đuôi dày có viền tách khối (vàng: lõi trắng viền đen / mun: lõi đen viền trắng)."""
        r = w // 2
        under = WHITE if is_black else BLACK
        core = BLACK if is_black else WHITE
        dr.line([p1, p2], fill=under, width=w + 5)
        dr.line([p1, p2], fill=core, width=w if is_black else max(1, w - 1))
        for pt in (p1, p2):
            rr = (w + 5) // 2
            dr.ellipse([pt[0] - rr, pt[1] - rr, pt[0] + rr, pt[1] + rr], fill=under)
            dr.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=core)

    # 1) Đuôi cong dựng đứng phía sau, vẫy theo tail_flick
    tail_a, tail_b, tail_c = (58, 174), (30, 140), (26, 106)
    tail_tip = (34 + int(tail_flick * 9), 82)
    segment(tail_a, tail_b, 9)
    segment(tail_b, tail_c, 9)
    segment(tail_c, tail_tip, 9)
    if not is_black:
        dr.line([(20, 132), (40, 128)], fill=BLACK, width=4)
        dr.line([(22, 114), (42, 110)], fill=BLACK, width=4)

    # 2) Hông sau vồng lên + 3) thân trước đổ tới đối thủ (2 ellipse chồng nhau)
    dr.ellipse([36, 126, 118, 210], fill=body, outline=edge, width=4)
    dr.ellipse([84, 94, 190, 188], fill=body, outline=edge, width=4)

    # 4) Chân sau chạm đất + bàn chân
    segment((80, 182), (94, FEET_Y - 6), 12)
    dr.ellipse([72, FEET_Y - 14, 120, FEET_Y + 4], fill=body, outline=edge, width=4)

    # 5) Chân trước trụ vững
    segment((148, 166), (154, FEET_Y - 8), 12)
    dr.ellipse([134, FEET_Y - 16, 180, FEET_Y + 2], fill=body, outline=edge, width=4)

    # 6) Đầu mèo hung hãn nghiêng về phía đối thủ
    hx, hy, hr = 178, 102, 37
    dr.ellipse([hx - hr, hy - hr, hx + hr, hy + hr], fill=body, outline=edge, width=4)

    # Tai (dẹp dần theo ear_flick khi căng thẳng)
    ef = max(0.0, min(1.0, ear_flick))
    dr.polygon([(150, 82), (134 + int(9 * ef), 44 + int(12 * ef)), (170, 70)], fill=body, outline=edge, width=3)
    dr.polygon([(186, 76), (200 - int(4 * ef), 40 + int(13 * ef)), (214, 74)], fill=body, outline=edge, width=3)

    # Mắt trợn + đồng tử nhìn thẳng đối thủ
    for ex, ey in ((164, 98), (194, 94)):
        dr.ellipse([ex - 9, ey - 9, ex + 9, ey + 9], fill=WHITE, outline=accent, width=3)
        dr.ellipse([ex - 2, ey - 4, ex + 6, ey + 4], fill=BLACK)
    # Lông mày cau
    dr.line([(150, 84), (172, 92)], fill=accent, width=4)
    dr.line([(184, 88), (208, 80)], fill=accent, width=4)

    # Mũi + miệng há to dần theo shout (gầm gừ)
    nose_y = 114
    sh = max(0.0, min(1.0, shout))
    dr.polygon([(196, nose_y - 5), (207, nose_y - 5), (202, nose_y + 2)], fill=accent)
    mouth = [(190, nose_y + 5), (213, nose_y + 5),
             (209, nose_y + 21 + int(9 * sh)), (194, nose_y + 21 + int(9 * sh))]
    dr.polygon(mouth, fill=accent if is_black else BLACK)
    fang = BLACK if is_black else WHITE
    dr.polygon([(193, nose_y + 5), (199, nose_y + 5), (196, nose_y + 14 + int(5 * sh))], fill=fang)
    dr.polygon([(204, nose_y + 5), (210, nose_y + 5), (207, nose_y + 14 + int(5 * sh))], fill=fang)

    # Ria mép hướng về đối thủ (đen để thấy trên nền trắng)
    for (y1, y2) in ((110, 102), (116, 116), (122, 130)):
        dr.line([(210, y1), (236, y2)], fill=BLACK, width=3)

    # Vằn lưng + trán (chỉ mèo vàng)
    if not is_black:
        dr.line([(92, 104), (108, 90)], fill=BLACK, width=4)
        dr.line([(118, 98), (132, 86)], fill=BLACK, width=4)
        dr.line([(144, 96), (158, 88)], fill=BLACK, width=4)
        dr.line([(168, 72), (174, 82)], fill=BLACK, width=3)
        dr.line([(182, 70), (186, 80)], fill=BLACK, width=3)

    # 7) Chân trước TÁT — vẽ SAU cùng để không bị đầu che: rút sau lưng (165°) rồi tát tới trước (-10°)
    shoulder = (152, 136)
    arm_len = 62
    atk = max(0.0, min(1.0, attack_phase))
    ang_deg = 165.0 - 175.0 * atk
    ang = math.radians(ang_deg)
    paw = (int(shoulder[0] + math.cos(ang) * arm_len), int(shoulder[1] - math.sin(ang) * arm_len))
    segment(shoulder, paw, 11)
    dr.ellipse([paw[0] - 10, paw[1] - 10, paw[0] + 10, paw[1] + 10], fill=body, outline=edge, width=4)
    # Móng vuốt bung theo claw_power (đạo diễn trận đấu điều khiển)
    cp = max(0.0, min(1.0, claw_power))
    if cp > 0.03:
        claw_len = int(4 + 16 * cp)
        for k in (-1, 0, 1):
            c_ang = math.radians(ang_deg - 14 + k * 14)
            x1 = paw[0] + int(math.cos(c_ang) * 9)
            y1 = paw[1] - int(math.sin(c_ang) * 9)
            x2 = paw[0] + int(math.cos(c_ang) * (9 + claw_len))
            y2 = paw[1] - int(math.sin(c_ang) * (9 + claw_len))
            dr.line([(x1, y1), (x2, y2)], fill=BLACK, width=3)

    if facing < 0:
        spr = ImageOps.mirror(spr)
    if abs(lean) > 0.01:
        spr = spr.rotate(lean, resample=Image.Resampling.BICUBIC, center=(W // 2, H // 2))
    return spr


# =========================================================================
# RIG MÈO V3 — POSE-DRIVEN, KHỚP NỐI 2-XƯƠNG IK, ĐẦU TÁCH KHỐI, NÉT MƯỢT (SUPERSAMPLE)
# =========================================================================


class _SDraw:
    """Bọc ImageDraw để vẽ theo toạ độ thiết kế trên canvas supersample (nét mượt, chống răng cưa)."""

    def __init__(self, draw: ImageDraw.ImageDraw, scale: float):
        self.d = draw
        self.s = float(scale)

    def _pts(self, xy):
        return [(p[0] * self.s, p[1] * self.s) for p in xy]

    def _box(self, box):
        return [v * self.s for v in box]

    def _w(self, w):
        return max(1, int(round(w * self.s)))

    def line(self, xy, fill, width=3):
        self.d.line(self._pts(xy), fill=fill, width=self._w(width))

    def ellipse(self, box, fill=None, outline=None, width=1):
        self.d.ellipse(self._box(box), fill=fill, outline=outline,
                       width=self._w(width) if outline is not None else 1)

    def polygon(self, pts, fill=None, outline=None, width=1):
        self.d.polygon(self._pts(pts), fill=fill, outline=outline,
                       width=self._w(width) if outline is not None else 1)

    def arc(self, box, start, end, fill=None, width=2):
        self.d.arc(self._box(box), start, end, fill=fill, width=self._w(width))


def _cat_rot(px, py, cx, cy, deg):
    """Xoay 1 điểm quanh tâm (độ, dương = ngược kim đồng hồ)."""
    if abs(deg) < 0.01:
        return px, py
    a = math.radians(deg)
    dx, dy = px - cx, py - cy
    return (cx + dx * math.cos(a) + dy * math.sin(a),
            cy - dx * math.sin(a) + dy * math.cos(a))


def _reach_target(s, t, l1, l2):
    """Kẹp đích bàn chân trong tầm với của chi 2 xương."""
    dx, dy = t[0] - s[0], t[1] - s[1]
    d = math.hypot(dx, dy)
    maxd = l1 + l2 - 0.6
    if d > maxd:
        k = maxd / (d or 1.0)
        return (s[0] + dx * k, s[1] + dy * k)
    return t


def _ik_elbow(s, t, l1, l2, flip=-1.0):
    """Khớp khuỷu của chi 2 xương (2-bone IK): trả về vị trí khuỷu tự nhiên."""
    dx, dy = t[0] - s[0], t[1] - s[1]
    d = math.hypot(dx, dy) or 1.0
    d = max(1.0, min(d, l1 + l2 - 0.6))
    a = (l1 * l1 - l2 * l2 + d * d) / (2.0 * d)
    h = math.sqrt(max(l1 * l1 - a * a, 0.0))
    ux, uy = dx / d, dy / d
    px, py = -uy * flip, ux * flip
    return (s[0] + ux * a + px * h, s[1] + uy * a + py * h)


def _draw_cat_pose(
    variant: str = "tabby",
    head_dx: float = 0.0,
    head_dy: float = 0.0,
    head_tilt: float = 0.0,
    blink: float = 0.0,
    pupil_dx: float = 0.6,
    pupil_dy: float = 0.0,
    ear: float = 0.35,
    shout: float = 0.3,
    bristle: float = 0.0,
    paw_a=None,
    paw_b=None,
    claw_a: float = 0.0,
    claw_b: float = 0.0,
    foot_a=(0.0, 0.0),
    foot_b=(0.0, 0.0),
    tail_lift: float = 0.45,
    tail_flick: float = 0.0,
    tail_puff: float = 0.0
) -> Image.Image:
    """
    Rig mèo v3 (pose-driven) — vẽ ở 3x rồi thu nhỏ nên nét mượt, chống răng cưa:
    - ĐẦU TÁCH KHỎI THÂN: head_dx/dy/tilt (né, cúi, húc, quay nhìn, gật)
    - 2 CHI TRƯỚC khớp 2-xương IK: paw_a/paw_b = đích bàn chân (None = thủ thế mặc định)
    - 2 BÀN CHÂN SAU có chu kỳ bước (foot_a/foot_b) + ĐUÔI chuỗi 6 đốt (lift/flick/puff)
    - MẶT: mắt nháy (blink), đồng tử nhìn theo đối thủ, tai dẹp (ear), miệng gầm (shout)
    - LÔNG XÙ GÁY khi cảnh dọa nhau (bristle 0..1)
    Trả về sprite RGBA 240x224, bàn chân chạm mốc y=208 (để paste theo mặt đất).
    """
    W, H = 240, 224
    SS = 3
    canvas = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    sd = _SDraw(ImageDraw.Draw(canvas), SS)

    is_black = variant == "black"
    body = BLACK if is_black else WHITE
    edge = WHITE if is_black else BLACK   # viền tách khối (mun viền trắng, vàng viền đen)
    accent = edge

    def segment(p1, p2, w):
        """Chi/đuôi dày có viền tách khối."""
        sd.line([p1, p2], fill=edge, width=w + 4)
        sd.line([p1, p2], fill=body, width=max(1, int(w) - 1))
        rr = (w + 4) / 2.0
        for pt in (p1, p2):
            sd.ellipse([pt[0] - rr, pt[1] - rr, pt[0] + rr, pt[1] + rr], fill=edge)
            r2 = max(1.0, w / 2.0 - 1.0)
            sd.ellipse([pt[0] - r2, pt[1] - r2, pt[0] + r2, pt[1] + r2], fill=body)

    # ===== 1) ĐUÔI: chuỗi 6 đốt có vật lý (độ nâng / vẫy / xù) =====
    lift = max(0.0, min(1.0, tail_lift))
    puff = max(0.0, min(1.0, tail_puff))
    ang0 = 152.0 + 78.0 * lift
    tip_curl = -9.0 + 15.0 * lift
    tw = 9.0 + 4.0 * puff
    tpts = [(60.0, 172.0)]
    for i in range(6):
        ang = math.radians(ang0 + tip_curl * i + 20.0 * tail_flick * math.sin(i * 0.9))
        tpts.append((tpts[-1][0] + math.cos(ang) * 11.0,
                     tpts[-1][1] + math.sin(ang) * 11.0))
    for i in range(6):
        segment(tpts[i], tpts[i + 1], tw * (1.0 - 0.055 * i))
    if not is_black:
        for i in (2, 4):
            x0, y0 = tpts[i]
            sd.line([(x0 - 5, y0 - 5), (x0 + 5, y0 + 5)], fill=BLACK, width=3)

    # ===== 2) LÔNG XÙ GÁY (bristle) — vẽ trước thân để gốc lông ẩn dưới thân =====
    br = max(0.0, min(1.0, bristle))
    if br > 0.04:
        back = [(72, 132), (95, 119), (118, 113), (140, 111)]
        for k in range(3):
            x0, y0 = back[k]
            x1, y1 = back[k + 1]
            nx, ny = (y1 - y0), -(x1 - x0)
            nl = math.hypot(nx, ny) or 1.0
            nx, ny = nx / nl, ny / nl
            hgt = 5.0 + 11.0 * br
            bx, by = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            sd.polygon([(x0, y0), (x1, y1), (bx + nx * hgt, by + ny * hgt)],
                       fill=body, outline=edge, width=2)

    # ===== 3) CHÂN SAU (2 bàn, chu kỳ bước) + HÔNG + THÂN =====
    fx1, fy1 = 104.0 + foot_a[0], 204.0 + foot_a[1]
    fx2, fy2 = 72.0 + foot_b[0], 204.0 + foot_b[1]
    segment((80, 178), (fx2, fy2 - 5), 12)                      # chân xa
    sd.ellipse([fx2 - 14, fy2 - 8, fx2 + 14, fy2 + 6], fill=body, outline=edge, width=3)
    sd.ellipse([36, 126, 118, 210], fill=body, outline=edge, width=4)    # hông
    sd.ellipse([84, 94, 190, 188], fill=body, outline=edge, width=4)     # ngực
    segment((92, 180), (fx1, fy1 - 5), 13)                      # chân gần
    sd.ellipse([fx1 - 15, fy1 - 9, fx1 + 15, fy1 + 7], fill=body, outline=edge, width=4)

    # vằn lưng (chỉ mèo vàng)
    if not is_black:
        sd.line([(96, 112), (108, 100)], fill=BLACK, width=4)
        sd.line([(122, 104), (134, 92)], fill=BLACK, width=4)
        sd.line([(148, 100), (160, 92)], fill=BLACK, width=4)

    # ===== 4) HAI CHI TRƯỚC — khớp 2-xương IK, móng bung theo claw =====
    def draw_arm(sh, paw, claw, w1, w2):
        l1, l2 = 27.0, 27.0
        t = _reach_target(sh, paw, l1, l2)
        el = _ik_elbow(sh, t, l1, l2, flip=-1.0)
        segment(sh, el, w1)
        r = w1 / 2.0
        sd.ellipse([el[0] - r, el[1] - r, el[0] + r, el[1] + r], fill=body, outline=edge, width=2)
        segment(el, t, w2)
        sd.ellipse([t[0] - 11, t[1] - 9, t[0] + 11, t[1] + 7], fill=body, outline=edge, width=4)
        cp = max(0.0, min(1.0, claw))
        if cp > 0.05:
            ang = math.atan2(t[1] - el[1], t[0] - el[0])
            for dk in (-0.30, 0.0, 0.30):
                a2 = ang + dk
                x1 = t[0] + math.cos(a2) * 7
                y1 = t[1] + math.sin(a2) * 7
                x2 = x1 + math.cos(a2) * (5 + 11 * cp)
                y2 = y1 + math.sin(a2) * (5 + 11 * cp)
                sd.line([(x1, y1), (x2, y2)], fill=BLACK, width=3)

    pa = paw_a if paw_a is not None else (198.0, 150.0)
    pb = paw_b if paw_b is not None else (168.0, 202.0)
    draw_arm((146.0, 150.0), pb, claw_b, 11, 9)      # chi xa (trụ dưới / bước)
    draw_arm((160.0, 130.0), pa, claw_a, 12, 10)     # chi gần (tát / giơ / giữ)

    # ===== 5) ĐẦU (tách khối — xoay/nghiêng/di chuyển độc lập với thân) =====
    hcx, hcy = 184.0 + head_dx, 96.0 + head_dy
    tilt = head_tilt

    def HR(px, py):
        return _cat_rot(px, py, hcx, hcy, tilt)

    ef = max(0.0, min(1.0, ear))
    # tai (dẹp dần theo ear) — vẽ trước hộp sọ để gốc tai ẩn
    sd.polygon([HR(152, 76), HR(138 + 10 * ef, 36 + 14 * ef), HR(176, 64)],
               fill=body, outline=edge, width=3)
    sd.polygon([HR(194, 66), HR(208 - 6 * ef, 32 + 14 * ef), HR(222, 64)],
               fill=body, outline=edge, width=3)
    sd.line([HR(152, 70), HR(148 + 6 * ef, 52 + 8 * ef)], fill=accent, width=3)
    sd.line([HR(196, 62), HR(202 - 3 * ef, 46 + 8 * ef)], fill=accent, width=3)
    # hộp sọ
    sd.ellipse([hcx - 38, hcy - 38, hcx + 38, hcy + 38], fill=body, outline=edge, width=4)

    # mắt: nháy + đồng tử nhìn theo đối thủ
    e_open = 1.0 - max(0.0, min(1.0, blink)) * 0.85
    for exo, eyo in ((-16, 3), (14, -3)):
        ex, ey = HR(hcx + exo, hcy + eyo)
        if e_open <= 0.34:
            sd.arc([ex - 9, ey - 8, ex + 9, ey + 8], 200, 340, fill=accent, width=3)
        else:
            top = ey - 9.5 * e_open
            sd.ellipse([ex - 9.5, top, ex + 9.5, ey + 9.5], fill=WHITE, outline=accent, width=3)
            pw = 7.5
            ph = 8.5 * e_open
            ppx = ex + 3.2 * max(-1.0, min(1.0, pupil_dx))
            ppy = ey + 2.0 + 3.0 * max(-1.0, min(1.0, pupil_dy))
            sd.ellipse([ppx - pw, ppy - ph, ppx + pw, ppy + ph], fill=BLACK)

    # lông mày cau (căng thẳng)
    sd.line([HR(158, 80), HR(178, 90)], fill=accent, width=4)
    sd.line([HR(192, 86), HR(210, 78)], fill=accent, width=4)

    # mũi + miệng gầm (mở theo shout) + răng nanh
    sh_ = max(0.0, min(1.0, shout))
    sd.polygon([HR(hcx + 18, hcy + 8), HR(hcx + 30, hcy + 8), HR(hcx + 24, hcy + 15)],
               fill=accent)
    mouth_fill = WHITE if is_black else BLACK
    fang_fill = BLACK if is_black else WHITE
    sd.polygon([HR(hcx + 13, hcy + 18), HR(hcx + 33, hcy + 17),
                HR(hcx + 31, hcy + 33 + 9 * sh_), HR(hcx + 15, hcy + 32 + 9 * sh_)],
               fill=mouth_fill)
    sd.polygon([HR(hcx + 15, hcy + 19), HR(hcx + 21, hcy + 19),
                HR(hcx + 18, hcy + 28 + 4 * sh_)], fill=fang_fill)
    sd.polygon([HR(hcx + 26, hcy + 19), HR(hcx + 32, hcy + 19),
                HR(hcx + 29, hcy + 28 + 4 * sh_)], fill=fang_fill)

    # vằn trán (chỉ mèo vàng)
    if not is_black:
        sd.line([HR(168, 62), HR(174, 74)], fill=BLACK, width=3)
        sd.line([HR(182, 58), HR(186, 70)], fill=BLACK, width=3)

    # ria mép (luôn đen để nổi trên nền trắng)
    for dy1, dy2 in ((2, -6), (8, 8), (14, 24)):
        sd.line([HR(hcx + 32, hcy + 12 + dy1), HR(hcx + 58, hcy + 12 + dy2)],
                fill=BLACK, width=3)

    return canvas.resize((W, H), Image.Resampling.LANCZOS)


def _render_cat_sprite(variant: str = "tabby", facing: int = 1, lean: float = 0.0,
                       squash: float = 1.0, **pose) -> Image.Image:
    """
    Sprite mèo hoàn thiện: xoay quanh MẶT ĐẤT (chân không trượt) + squash/stretch giữ chân chạm đất.
    facing=-1: lật sang trái (nhân vật thứ 2 đối diện).
    """
    spr = _draw_cat_pose(variant, **pose)
    if facing < 0:
        spr = ImageOps.mirror(spr)
    if abs(squash - 1.0) > 0.004:
        new_h = max(4, int(round(spr.height * squash)))
        spr = spr.resize((spr.width, new_h), Image.Resampling.BILINEAR)
    if abs(lean) > 0.02:
        spr = spr.rotate(lean, resample=Image.Resampling.BICUBIC,
                         center=(spr.width // 2, 208))
    return spr


def _draw_cat_lying(variant: str = "black", breathe: float = 0.0,
                    tail_flick: float = 0.0, asleep: bool = True) -> Image.Image:
    """
    Mèo nằm dài sưởi nắng (sprite 300x160, mặt đất y=130):
    - thân duỗi dài, thở nhẹ (breathe 0..1), đuôi quét đất, mắt nhắm ngủ ngon.
    """
    W, H, SS = 300, 160, 3
    canvas = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    sd = _SDraw(ImageDraw.Draw(canvas), SS)
    is_black = variant == "black"
    body = BLACK if is_black else WHITE
    edge = WHITE if is_black else BLACK
    accent = edge
    gy = 130.0

    # đuôi vòng ra trước
    tpts = [(84.0, gy - 14.0)]
    for i in range(6):
        ang = math.radians(160 + 13 * i + 15 * tail_flick * math.sin(i * 0.9))
        tpts.append((tpts[-1][0] + math.cos(ang) * 13.0,
                     tpts[-1][1] + math.sin(ang) * 6.0))
    for i in range(6):
        w = 8 - i * 0.5
        sd.line([tpts[i], tpts[i + 1]], fill=edge, width=w + 4)
        sd.line([tpts[i], tpts[i + 1]], fill=body, width=w)

    br = 1.0 + 0.03 * math.sin(breathe * math.pi * 2.0)
    rx, ry = 82.0, 28.0 * br
    bx, by = 148.0, gy - ry + 6
    sd.ellipse([bx - rx, by - ry, bx + rx, by + ry], fill=body, outline=edge, width=4)
    # bàn chân trước duỗi
    for px_, py_ in ((bx + 62, gy - 7), (bx + 86, gy - 6)):
        sd.ellipse([px_ - 15, py_ - 9, px_ + 15, py_ + 7], fill=body, outline=edge, width=4)
    # đầu gối trên thân (bên phải)
    hx, hy = bx + rx - 4, by - 14
    sd.polygon([(hx - 26, hy - 18), (hx - 34, hy - 46), (hx - 8, hy - 30)],
               fill=body, outline=edge, width=3)
    sd.polygon([(hx + 14, hy - 24), (hx + 24, hy - 50), (hx + 36, hy - 20)],
               fill=body, outline=edge, width=3)
    sd.ellipse([hx - 30, hy - 30, hx + 30, hy + 30], fill=body, outline=edge, width=4)
    if asleep:
        for exo in (-12, 10):
            ex, ey = hx + exo, hy - 2
            sd.arc([ex - 8, ey - 7, ex + 8, ey + 8], 200, 340, fill=accent, width=3)
    else:
        for exo in (-12, 10):
            sd.ellipse([hx + exo - 7, hy - 9, hx + exo + 7, hy + 4], fill=WHITE, outline=accent, width=3)
            sd.ellipse([hx + exo - 3, hy - 5, hx + exo + 3, hy + 1], fill=BLACK)
    sd.polygon([(hx + 12, hy + 8), (hx + 24, hy + 8), (hx + 18, hy + 15)], fill=accent)
    sd.arc([hx + 6, hy + 10, hx + 30, hy + 26], 20, 150, fill=accent, width=3)
    for dy1, dy2 in ((8, 0), (14, 14)):
        sd.line([(hx + 26, hy + 12 + dy1), (hx + 50, hy + 12 + dy2)], fill=BLACK, width=3)
    if not is_black:
        for ox in (-30, -6, 18):
            sd.line([(bx + ox, by - ry + 4), (bx + ox + 10, by - ry - 8)], fill=BLACK, width=4)
    return canvas.resize((W, H), Image.Resampling.LANCZOS)


def _draw_impact_star(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, rot: float = 0.0):
    """Sao va chạm kiểu comic: lõi sao 16 cánh + tia nhỏ tỏa ra (đen-trắng)."""
    if r < 4:
        return
    pts = []
    for i in range(16):
        ang = i * (math.pi / 8.0) + rot
        rr = r if i % 2 == 0 else r // 2
        pts.append((cx + int(math.cos(ang) * rr), cy + int(math.sin(ang) * rr)))
    draw.polygon(pts, fill=WHITE, outline=BLACK, width=3)
    for i in range(6):
        ang = rot + i * math.pi / 3.0
        x1 = cx + int(math.cos(ang) * (r + 5))
        y1 = cy + int(math.sin(ang) * (r + 5))
        x2 = cx + int(math.cos(ang) * (r + 13))
        y2 = cy + int(math.sin(ang) * (r + 13))
        draw.line([(x1, y1), (x2, y2)], fill=BLACK, width=3)


def _draw_whoosh_arcs(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, prog: float):
    """Vệt gió khi tát trượt: 3 cung đồng tâm (prog 0..1 = độ hiện)."""
    if prog <= 0.0 or r < 8:
        return
    for i in range(3):
        rr = r - i * 9
        if rr < 7:
            continue
        pts = []
        for k in range(9):
            ang = math.radians(-55 + k * 22)
            pts.append((cx + int(math.cos(ang) * rr), cy + int(math.sin(ang) * rr)))
        draw.line(pts, fill=BLACK, width=max(2, 5 - i))


def _draw_dust_puff(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int):
    """Bụi bốc lên ở chân khi tiếp đất (3 cụm tròn outline)."""
    if r < 3:
        return
    for k, scale in ((0, 1.0), (1, 0.75), (2, 0.5)):
        rr = max(2, int(r * scale))
        ox = (k - 1) * int(r * 0.9)
        draw.ellipse([cx + ox - rr, cy - rr, cx + ox + rr, cy + rr], fill=WHITE, outline=BLACK, width=2)


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
    if spec.get("pie_cats"):
        # thu nhỏ + nâng biểu đồ lên nhường chỗ cho 2 mèo giả vờ tát phía dưới
        cx = width // 2
        cy = height // 2 - 38
        radius = 96

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
        _labels = spec.get("pie_labels") or labels_default
        sub_txt = _labels[idx] if idx < len(_labels) else f"NHÓM {idx+1}"
        box_w, box_h = 128, 54
        bx = lx3 + 6 if horiz_dir > 0 else lx3 - box_w - 6
        _bottom_guard = 175 if spec.get("pie_cats") else 16
        by = max(72, min(height - box_h - _bottom_guard, ly2 - box_h // 2))

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
    if spec.get("brain_variant") == "duel":
        _compose_brain_duel_scene(img, draw, scene, spec, width, height, p)
        return
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


def _compose_cat_duel(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    p: float,
    text_blob: str
):
    """
    Võ đài 2 chiến binh mèo (Band B 720x420) — vũ đạo trận đấu đầy đủ:
    - 0.00–0.12  Thủ thế gườm nhau (thở nhẹ, đuôi ngoáy, tai dẹp dần)
    - 0.12–0.72  3 hiệp tát xen kẽ: lấy đà → nhảy bổ (squash/stretch) → vung chân
                 → đối thủ trúng đòn (bật lùi + sao va chạm) hoặc né (vệt gió)
    - 0.72–0.90  Liên hoàn tốc độ cao: cả hai lao vào tát liên tục, tóe lửa
    - 0.90–1.00  Áp sát "mũi đối mũi" căng thẳng (thở dồn, đuôi quất)
    Kịch bản có chữ "trượt/hụt" → phần lớn các đòn sẽ trượt (đúng lời thoại).
    """
    ground_y = height - 45
    miss_mode = any(w in text_blob for w in ("trượt", "hụt"))
    hit_flags = (False, False, True) if miss_mode else (True, False, True)

    def ease_io(t):
        t = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * t)

    def ease_in(t):
        t = max(0.0, min(1.0, t))
        return t ** 3

    def bell(t, a, b):
        if t <= a or t >= b:
            return 0.0
        return math.sin(math.pi * (t - a) / (b - a))

    # ---- Trạng thái từng mèo: dx (lao vào tâm), hop (bay lên), lean (độ), atk (0..1 cánh tay),
    #      claw, ear, shout, tail, sq (hệ số squash/stretch)
    def new_f():
        return {"dx": 0.0, "hop": 0.0, "lean": 0.0, "atk": 0.06, "claw": 0.0,
                "ear": 0.35, "shout": 0.3, "tail": 0.0, "sq": 1.0}
    F = [new_f(), new_f()]
    events = []   # (kind, side_thủ, t_center): "hit" = trúng đòn, "whoosh" = trượt

    if p < 0.12:
        s = p / 0.12
        for i in range(2):
            ph = i * math.pi
            F[i]["dx"] = 3.0 * ease_io(s)
            F[i]["lean"] = 2.0 + 1.0 * math.sin(s * math.pi * 2 + ph)
            F[i]["ear"] = 0.45
            F[i]["shout"] = 0.35 + 0.2 * math.sin(s * math.pi * 2 + ph)
            F[i]["tail"] = math.sin(s * math.pi * 4 + ph)
    elif p < 0.72:
        k = int((p - 0.12) / 0.2)
        t = (p - (0.12 + 0.2 * k)) / 0.2
        ai = k % 2
        di = 1 - ai
        A, D = F[ai], F[di]
        # Kẻ tấn công: lấy đà → nhảy bổ → tát → hạ cánh rút về
        if t <= 0.35:
            A["atk"] = 0.10 * math.sin((t / 0.35) * math.pi)      # nhún lấy đà
            A["dx"] = 8 * ease_io(t / 0.35)
        elif t <= 0.55:
            u = (t - 0.35) / 0.20
            A["atk"] = ease_in(u)                                 # vung tới nhanh dần
            A["dx"] = 8 + 26 * ease_io(u)                         # lao tới
            A["hop"] = 16 * math.sin(math.pi * u)                 # bay lên
        elif t <= 0.66:
            A["atk"] = 1.0                                        # tát hết tầm
            A["dx"] = 34
            A["hop"] = 4
        else:
            v = (t - 0.66) / 0.34
            A["atk"] = max(0.14, 1.0 - 0.86 * ease_io(v))         # thu chân về
            A["dx"] = 34 * (1 - ease_io(v))
            A["hop"] = 6 * bell(t, 0.66, 0.86)
            A["sq"] = 1.0 - 0.07 * bell(t, 0.64, 0.9)             # nhún tiếp đất
        A["claw"] = bell(t, 0.5, 0.86)
        A["lean"] = 4 + 8 * bell(t, 0.3, 0.92)
        A["ear"] = 1.0
        A["shout"] = 0.5 + 0.5 * bell(t, 0.4, 0.9)
        A["tail"] = math.sin(p * math.pi * 18) * 0.7
        # Đối thủ: trúng đòn (bật lùi) hoặc né (ngửa người)
        if hit_flags[k]:
            D["dx"] = -16 * bell(t, 0.45, 1.05)
            D["hop"] = 6 * bell(t, 0.5, 0.78)
            D["lean"] = -13 * bell(t, 0.45, 0.95) + 2.5 * math.sin(t * math.pi * 10) * bell(t, 0.5, 1.0)
            D["atk"] = 0.35 + 0.4 * bell(t, 0.55, 0.9)
            D["ear"] = 1.0
            D["shout"] = 0.75 + 0.25 * bell(t, 0.5, 0.85)
            D["tail"] = math.sin(p * math.pi * 16 + 1.0)
            events.append(("hit", di, 0.12 + 0.2 * k + 0.2 * 0.56))
        else:
            D["dx"] = -11 * bell(t, 0.4, 1.0)
            D["lean"] = 6 * bell(t, 0.35, 0.62) - 13 * bell(t, 0.45, 0.95)
            D["atk"] = 0.16
            D["ear"] = 0.9
            D["shout"] = 0.4
            D["tail"] = math.sin(p * math.pi * 14 + 1.0)
            events.append(("whoosh", di, 0.12 + 0.2 * k + 0.2 * 0.56))
    elif p < 0.90:
        u = (p - 0.72) / 0.18
        for i in range(2):
            ph = u * math.pi * 6.0 + i * math.pi
            F[i]["dx"] = 96 + 5 * math.sin(ph)
            F[i]["atk"] = 0.5 - 0.5 * math.cos(ph)
            F[i]["claw"] = 1.0
            F[i]["hop"] = 4 * abs(math.sin(ph * 0.5))
            F[i]["lean"] = 5 + 3 * math.sin(ph * 0.5)
            F[i]["ear"] = 1.0
            F[i]["shout"] = 1.0
            F[i]["tail"] = math.sin(ph * 0.7)
        for j in range(6):
            events.append(("hit", j % 2, 0.72 + 0.18 * (j + 0.5) / 6.0))
    else:
        v = (p - 0.90) / 0.10
        for i in range(2):
            ph = i * math.pi
            F[i]["dx"] = 102 - 8 * ease_io(v)
            F[i]["atk"] = 0.12 + 0.06 * math.sin(v * math.pi * 2 + ph)
            F[i]["lean"] = 6.0 + 1.5 * math.sin(v * math.pi * 3 + ph)
            F[i]["ear"] = 1.0
            F[i]["shout"] = 0.85 + 0.15 * math.sin(v * math.pi * 4 + ph)
            F[i]["tail"] = math.sin(v * math.pi * 6 + ph)
            F[i]["sq"] = 1.0 + 0.02 * math.sin(v * math.pi * 6 + ph)

    # ---- Vẽ 2 chiến binh (scale dọc tại chỗ để giữ chân chạm đất)
    sprites = []
    for i, (facing, variant) in enumerate(((1, "tabby"), (-1, "black"))):
        f = F[i]
        spr = _draw_cat_fighter_rig(
            facing=facing, variant=variant,
            attack_phase=f["atk"], lean=(-f["lean"] if i == 0 else f["lean"]),
            tail_flick=f["tail"], claw_power=f["claw"],
            ear_flick=f["ear"], shout=f["shout"],
        )
        if abs(f["sq"] - 1.0) > 0.005:
            new_h = max(2, int(spr.height * f["sq"]))
            resample = getattr(Image, "Resampling", Image).BILINEAR
            spr = spr.resize((spr.width, new_h), resample)
        sprites.append(spr)

    y_base = ground_y - 208
    xs = []
    for i, spr in enumerate(sprites):
        f = F[i]
        scale = spr.height / 224.0
        y_top = int(y_base - f["hop"] - 208 * (scale - 1.0))
        x = int(40 + f["dx"]) if i == 0 else int(width - 40 - 240 - f["dx"])
        xs.append(x)
        img.paste(spr, (x, y_top), spr)

    # ---- Hiệu ứng đòn đánh: sao trúng đòn / vệt gió trượt
    for kind, side, t_ev in events:
        near = 1.0 - min(1.0, abs(p - t_ev) / 0.05)
        if near <= 0.0:
            continue
        hx = xs[side] + (178 if side == 0 else 62)
        if kind == "hit":
            # Sao trúng đòn ở ĐỈNH ĐẦU, lệch về phía đối thủ để đọc rõ trên cả nền trắng lẫn mèo đen
            star_scale = 0.55 if t_ev >= 0.72 else 1.0
            r = max(6, int((12 + 26 * near) * star_scale))
            _draw_impact_star(draw, hx + (24 if side == 0 else -24), y_base + 62, r, rot=p * 3.0)
        else:
            _draw_whoosh_arcs(draw, hx + (34 if side == 0 else -34), y_base + 96, int(34 + 10 * near), near)

    # ---- Bụi tiếp đất + vệt tốc độ khi lao tới
    if 0.12 <= p < 0.72:
        k = int((p - 0.12) / 0.2)
        t = (p - (0.12 + 0.2 * k)) / 0.2
        ai = k % 2
        for i in range(2):
            if i == ai:
                dust = bell(t, 0.62, 0.85)
                if dust > 0.05:
                    _draw_dust_puff(draw, xs[i] + 120, ground_y - 8, int(6 + 14 * dust))
                speed = bell(t, 0.3, 0.62)
                if speed > 0.15:
                    for s_i in range(3):
                        ly = y_base + 54 + s_i * 40
                        ln = 26 + int(18 * speed) + s_i * 5
                        if i == 0:
                            x2 = xs[i] - 8
                            draw.line([(x2 - ln, ly), (x2, ly)], fill=BLACK, width=3)
                        else:
                            x2 = xs[i] + 240 + 8
                            draw.line([(x2, ly), (x2 + ln, ly)], fill=BLACK, width=3)
            else:
                slide = bell(t, 0.5, 0.95)
                if slide > 0.1:
                    _draw_dust_puff(draw, xs[i] + 120, ground_y - 6, int(4 + 10 * slide))

    # ---- Tâm chấn giữa sàn (mờ khi thủ thế, bùng khi giao đấu)
    activity = 0.0
    if p >= 0.12:
        if p < 0.72:
            k = int((p - 0.12) / 0.2)
            hit_t = 0.12 + 0.2 * k + 0.112   # tâm chấn chỉ bùng đúng lúc chạm đòn
            activity = max(0.0, 1.0 - abs(p - hit_t) / 0.055)
        elif p < 0.90:
            activity = 0.75 + 0.25 * abs(math.sin((p - 0.72) / 0.18 * math.pi * 6))
        else:
            activity = max(0.0, 1.0 - (p - 0.90) / 0.10) * 0.5
    if activity > 0.12:
        cxc, cyc = width // 2, 205
        burst_r = int(18 + 22 * activity)
        pts = []
        for i in range(16):
            ang = i * (math.pi / 8.0) + p * 3.0
            r = burst_r if i % 2 == 0 else burst_r // 2
            pts.append((cxc + int(math.cos(ang) * r), cyc + int(math.sin(ang) * r)))
        draw.polygon(pts, fill=WHITE, outline=BLACK, width=3)
        if activity > 0.55:
            draw.polygon(
                [(cxc + 2, cyc - 16), (cxc - 9, cyc + 2), (cxc + 2, cyc + 2),
                 (cxc - 5, cyc + 18), (cxc + 11, cyc - 2), (cxc + 1, cyc - 2)],
                fill=BLACK
            )

    # ---- Mặt đất + gạch hatch
    draw.line([(50, ground_y), (width - 50, ground_y)], fill=BLACK, width=4)
    for hx in range(70, width - 60, 40):
        draw.line([(hx, ground_y), (hx - 14, ground_y + 14)], fill=BLACK, width=2)


def _draw_cat_ground(draw: ImageDraw.ImageDraw, width: int, ground_y: int):
    """Mặt đất võ đài: đường nền + gạch hatch nghiêng."""
    draw.line([(50, ground_y), (width - 50, ground_y)], fill=BLACK, width=4)
    for hx in range(70, width - 60, 40):
        draw.line([(hx, ground_y), (hx - 14, ground_y + 14)], fill=BLACK, width=2)


def _draw_cat_ground_shadow(draw: ImageDraw.ImageDraw, cx: float, ground_y: int,
                            half_w: int = 70, layers: int = 3):
    """Bóng tiếp đất (vài lát gạch ngắn dưới chân) — giúp mèo 'đứng thật' trên sàn."""
    for k in range(layers):
        hw = half_w - k * 12
        y = ground_y - 4 - k * 5
        draw.line([(cx - hw, y), (cx + hw, y)], fill=BLACK, width=2)


def _draw_sweat_drop(draw: ImageDraw.ImageDraw, x: float, y: float, s: float = 1.0):
    """Giọt mồ hôi hài hước (toát ra khi căng thẳng / đuối sức)."""
    draw.polygon([(x - 4 * s, y - 2 * s), (x + 4 * s, y - 2 * s), (x, y - 13 * s)],
                 fill=WHITE, outline=BLACK, width=2)
    draw.ellipse([x - 6 * s, y - 4 * s, x + 6 * s, y + 8 * s], fill=WHITE, outline=BLACK, width=3)


def _draw_heart(draw: ImageDraw.ImageDraw, cx: float, cy: float, s: float = 1.0, fill=BLACK):
    pts = [(cx - 9 * s, cy - 3 * s), (cx - 9 * s, cy - 8 * s), (cx - 4.5 * s, cy - 12 * s),
           (cx, cy - 8.5 * s), (cx + 4.5 * s, cy - 12 * s), (cx + 9 * s, cy - 8 * s),
           (cx + 9 * s, cy - 3 * s), (cx, cy + 7 * s)]
    draw.polygon(pts, fill=fill)


def _draw_bolt(draw: ImageDraw.ImageDraw, x1: float, y1: float, x2: float, y2: float,
               p: float, segs: int = 4, w: int = 7):
    """Tia chớp zic-zac giật liên tục giữa 2 điểm."""
    pts = [(x1, y1)]
    for k in range(1, segs):
        t = k / float(segs)
        xm = x1 + (x2 - x1) * t + 9 * math.sin(p * 30.0 + k * 2.1)
        ym = y1 + (y2 - y1) * t + (15 if k % 2 else -15) * (1.0 - abs(2 * t - 1.0))
        pts.append((xm, ym))
    pts.append((x2, y2))
    draw.line(pts, fill=BLACK, width=w, joint="curve")
    for k in range(1, segs):
        xm, ym = pts[k]
        r = 4 + 3 * abs(math.sin(p * 20.0 + k))
        draw.line([(xm - r * 2, ym - r), (xm - r * 3.5, ym - r * 2)], fill=BLACK, width=2)


def _draw_sound_waves(draw: ImageDraw.ImageDraw, cx: float, cy: float, p: float,
                      n: int = 3, base: int = 26, span: int = 86):
    """Sóng âm lan ra từ miệng mèo gầm ghè."""
    for k in range(n):
        ph = (p * 1.6 + k * 0.33) % 1.0
        r = base + span * ph
        spread = 34 - 12 * ph
        draw.arc([cx - r, cy - r, cx + r, cy + r], -spread, spread, fill=BLACK, width=3)


def _draw_clock_prop(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: int, p: float,
                     label: str = ""):
    """Đồng hồ đạo cụ với kim phút quay nhanh (đếm ngược 30 phút)."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE, outline=BLACK, width=4)
    for k in range(12):
        a = k * math.pi / 6.0
        draw.line([(cx + math.cos(a) * (r - 9), cy + math.sin(a) * (r - 9)),
                   (cx + math.cos(a) * (r - 3), cy + math.sin(a) * (r - 3))],
                  fill=BLACK, width=2)
    amin = -math.pi / 2 + p * math.pi * 4.0
    ahr = -math.pi / 2 + p * math.pi * 2.0 / 6.0
    draw.line([(cx, cy), (cx + math.cos(amin) * (r - 15), cy + math.sin(amin) * (r - 15))],
              fill=BLACK, width=3)
    draw.line([(cx, cy), (cx + math.cos(ahr) * (r - 27), cy + math.sin(ahr) * (r - 27))],
              fill=BLACK, width=5)
    draw.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=BLACK)
    if label:
        f = _get_font(13, bold=True)
        try:
            tw = draw.textlength(label, font=f)
        except Exception:
            tw = 60
        draw.text((cx - tw / 2, cy + r + 6), label, font=f, fill=BLACK)


def _draw_sunbeam(draw: ImageDraw.ImageDraw, width: int, height: int, ground_y: int, p: float):
    """Góc nắng: ông mặt trời nhỏ + vệt nắng chéo rơi xuống sàn (chỗ mèo mun nằm sưởi)."""
    sx, sy, sr = width - 78, 96, 24
    draw.ellipse([sx - sr, sy - sr, sx + sr, sy + sr], outline=BLACK, width=4)
    for k in range(8):
        a = k * math.pi / 4.0 + 0.35
        r1 = sr + 8
        r2 = sr + 22 + 3 * math.sin(p * math.pi * 4.0 + k)
        draw.line([(sx + math.cos(a) * r1, sy + math.sin(a) * r1),
                   (sx + math.cos(a) * r2, sy + math.sin(a) * r2)], fill=BLACK, width=3)
    for k in range(5):
        x0 = width - 46 - k * 30
        draw.line([(x0, 150), (x0 - 170, ground_y - 2)], fill=BLACK, width=2)
    draw.line([(width - 262, ground_y - 3), (width - 64, ground_y - 3)], fill=BLACK, width=3)


def _draw_dish_prop(draw: ImageDraw.ImageDraw, cx: float, cy: float, food: bool = True):
    """Đĩa thức ăn đầy (vành đĩa + núm đồ ăn + vụn lấm tấm)."""
    w_, h_ = 54, 18
    draw.ellipse([cx - w_, cy - h_, cx + w_, cy + h_], fill=WHITE, outline=BLACK, width=4)
    draw.ellipse([cx - w_ + 13, cy - h_ + 6, cx + w_ - 13, cy + h_ - 4],
                 fill=WHITE, outline=BLACK, width=2)
    if food:
        draw.polygon([(cx - 44, cy - 2), (cx - 34, cy - 15), (cx - 18, cy - 23),
                      (cx - 1, cy - 25), (cx + 16, cy - 21), (cx + 30, cy - 13),
                      (cx + 41, cy - 2)], fill=WHITE, outline=BLACK, width=3)
        for ox, oy in ((-20, -12), (-2, -17), (16, -12)):
            draw.ellipse([cx + ox - 2, cy + oy - 2, cx + ox + 2, cy + oy + 2], fill=BLACK)


def _cat_duel_choreo(p: float, mode: str, width: int):
    """
    Bộ biên đạo trận mèo (battle / threat / sloppy) — trả về (F, FX, EVENTS, zs, base_x):
    - F: pose từng mèo (đầu/tay/chân/đuôi/mặt), FX: vị trí & nghiêng & squash, EVENTS: mốc đòn đánh
    - zs: hệ số zoom sân khấu (cận cảnh cho sloppy), base_x: mép trái sprite mỗi mèo
    """
    def bell(t, a, b):
        if t <= a or t >= b:
            return 0.0
        return math.sin(math.pi * (t - a) / (b - a))

    def ease_io(t):
        t = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * t)

    def blank():
        return {"head_dx": 0.0, "head_dy": 0.0, "head_tilt": 0.0, "blink": 0.0,
                "pupil_dx": 0.8, "pupil_dy": 0.0, "ear": 0.35, "shout": 0.3,
                "bristle": 0.0, "paw_a": None, "paw_b": None, "claw_a": 0.0, "claw_b": 0.0,
                "foot_a": (0.0, 0.0), "foot_b": (0.0, 0.0),
                "tail_lift": 0.45, "tail_flick": 0.0, "tail_puff": 0.0}

    def walk_feet(f, ph, step=9.0, lift=8.0):
        f["foot_a"] = (step * math.sin(ph), -lift * max(0.0, math.sin(ph)))
        f["foot_b"] = (step * math.sin(ph + math.pi), -lift * max(0.0, math.sin(ph + math.pi)))
        f["paw_b"] = (168.0 + step * 1.1 * math.sin(ph + math.pi),
                      202.0 - lift * max(0.0, math.sin(ph + math.pi)))

    F = [blank(), blank()]
    FX = [{"dx": 0.0, "hop": 0.0, "lean": 0.0, "squash": 1.0},
          {"dx": 0.0, "hop": 0.0, "lean": 0.0, "squash": 1.0}]
    EVENTS = []
    zs = 1.0
    base_x = [40.0, float(width - 280.0)]
    miss_all = mode == "threat"
    sloppy = mode == "sloppy"
    if mode == "threat":
        zs = 1.12
    elif sloppy:
        zs = 1.16
        base_x = [120.0, float(width - 360.0)]

    if sloppy:
        # ============ SLOPPY: 3 cú tát trượt liên hoàn + né vụng về ============
        if p < 0.08:
            u = p / 0.08
            for i in range(2):
                F[i]["bristle"] = 0.5 * u
                F[i]["ear"] = 0.6 * u
                FX[i]["lean"] = 3.0 * u
                F[i]["tail_lift"] = 0.6
        elif p < 0.70:
            k = min(2, int((p - 0.08) / 0.2067))
            t = ((p - 0.08) - 0.2067 * k) / 0.2067
            arm = "paw_b" if k == 1 else "paw_a"
            A = F[0]
            if t <= 0.30:
                u = t / 0.30
                A[arm] = (150.0 - 12.0 * u, 114.0 + 12.0 * u)
                A["claw_a"] = 0.5
                FX[0]["lean"] = -5.0 * u
                A["tail_lift"] = 1.0
            elif t <= 0.55:
                u = (t - 0.30) / 0.25
                if arm == "paw_a":
                    A["paw_a"] = (176.0 + 58.0 * u, 120.0 - 8.0 * u)
                    A["claw_a"] = 1.0
                else:
                    A["paw_b"] = (150.0 + 60.0 * u, 168.0 + 6.0 * u)
                    A["claw_b"] = 1.0
                FX[0]["lean"] = 5.0 + 9.0 * u
                A["shout"] = 0.75
            else:
                u = (t - 0.55) / 0.45
                if arm == "paw_a":
                    A["paw_a"] = (234.0 - 36.0 * ease_io(u), 112.0 + 26.0 * ease_io(u))
                else:
                    A["paw_b"] = (210.0 - 42.0 * ease_io(u), 174.0 - 6.0 * ease_io(u))
                FX[0]["lean"] = 14.0 - 10.0 * ease_io(u)
                A["claw_a"] = 0.4 * (1.0 - u)
                A["claw_b"] = 0.4 * (1.0 - u) if k == 1 else A["claw_b"]
            A["ear"] = 1.0
            A["bristle"] = 0.8
            A["tail_puff"] = 0.6
            A["tail_flick"] = math.sin(p * 70)
            D = F[1]
            if k == 0:
                dk = bell(t, 0.34, 0.95)
                D["head_dy"] = 16.0 * dk
                D["head_tilt"] = 10.0 * dk
                D["blink"] = 0.6 * dk
                FX[1]["squash"] = 1.0 - 0.14 * dk
                D["paw_a"] = (198.0, 150.0 + 22.0 * dk)
            elif k == 1:
                lb = bell(t, 0.34, 1.1)
                wob = 5.0 * math.sin(t * math.pi * 9.0) * bell(t, 0.55, 1.0)
                D["head_dy"] = -6.0 * lb
                FX[1]["lean"] = 8.0 + 18.0 * lb + wob
                FX[1]["squash"] = 1.0 + 0.05 * lb
                D["paw_a"] = (204.0 - 12.0 * lb, 122.0 - 24.0 * lb)
                D["paw_b"] = (150.0 + 12.0 * lb, 154.0 - 18.0 * lb)
                D["claw_a"] = 0.6
                D["shout"] = 0.6 * lb
                D["ear"] = 0.45
            else:
                hp = bell(t, 0.30, 0.92)
                D["foot_a"] = (11.0 * hp, -7.0 * hp)
                D["foot_b"] = (-11.0 * hp, 0.0)
                FX[1]["dx"] = -30.0 * hp
                FX[1]["hop"] = 15.0 * math.sin(math.pi * max(0.0, min(1.0, (t - 0.30) / 0.5)))
                FX[1]["lean"] = -9.0 * hp
                FX[1]["squash"] = 1.0 - 0.10 * bell(t, 0.62, 0.98)
                D["paw_a"] = (206.0 + 10.0 * hp, 128.0 - 12.0 * hp)
                D["ear"] = 0.9
                D["shout"] = 0.5 * hp
            D["bristle"] = 0.7
            D["tail_lift"] = 0.8
            D["tail_flick"] = math.sin(p * 60 + 1.0)
            EVENTS.append(("whoosh", 1, 0.08 + 0.2067 * k + 0.2067 * 0.52))
        elif p < 0.88:
            u = (p - 0.70) / 0.18
            F[0]["paw_a"] = (196.0, 168.0)
            F[0]["paw_b"] = (168.0, 202.0)
            F[0]["head_dy"] = 6.0 * math.sin(u * math.pi)
            F[0]["shout"] = 0.65 + 0.35 * abs(math.sin(u * math.pi * 7.0))
            F[0]["ear"] = 0.9
            F[0]["tail_lift"] = 0.2
            FX[0]["lean"] = 5.0 + 3.0 * math.sin(u * math.pi * 6.0)
            FX[0]["squash"] = 1.0 - 0.05 * math.sin(u * math.pi)
            F[1]["ear"] = 0.15
            F[1]["head_tilt"] = 9.0 * ease_io(u)
            F[1]["blink"] = 0.3 * u
            F[1]["tail_lift"] = 0.9
            F[1]["tail_flick"] = 0.5 * math.sin(u * math.pi * 4)
            FX[1]["lean"] = 2.0 * math.sin(u * math.pi * 2)
        else:
            v = (p - 0.88) / 0.12
            F[0]["head_dy"] = 5.0
            F[0]["shout"] = 0.4 + 0.25 * abs(math.sin(v * math.pi * 4))
            F[0]["ear"] = 0.85
            F[0]["tail_lift"] = 0.2
            F[1]["head_tilt"] = 7.0
            F[1]["blink"] = 0.25 + 0.2 * math.sin(v * math.pi * 3)
            F[1]["tail_lift"] = 0.85
            F[1]["tail_flick"] = 0.4 * math.sin(v * math.pi * 5)
        return F, FX, EVENTS, zs, base_x

    # ============ BATTLE / THREAT ============
    if p < 0.10:
        t = p / 0.10
        for i in range(2):
            ph = t * math.pi * 2.0 * 3.0 + i * math.pi
            FX[i]["dx"] = -140.0 * (1.0 - ease_io(t))
            FX[i]["hop"] = 3.0 * abs(math.sin(ph))
            walk_feet(F[i], ph, step=10.0, lift=8.0)
            F[i]["bristle"] = 0.25 + 0.35 * t
            F[i]["ear"] = 0.3 + 0.5 * t
            F[i]["shout"] = 0.25 + 0.2 * t
            F[i]["tail_lift"] = 0.3 + 0.45 * t
            F[i]["tail_flick"] = 0.5 * math.sin(p * 60 + i)
            FX[i]["lean"] = 3.0
    elif p < 0.20:
        u = (p - 0.10) / 0.10
        for i in range(2):
            ph = i * math.pi
            F[i]["bristle"] = 0.6 + 0.4 * abs(math.sin(u * math.pi * 3 + ph))
            F[i]["ear"] = 1.0
            F[i]["shout"] = 0.45 + 0.4 * abs(math.sin(u * math.pi * 4 + ph))
            F[i]["tail_puff"] = 0.7
            F[i]["tail_lift"] = 0.78
            F[i]["tail_flick"] = 0.9 * math.sin(p * 90 + ph)
            F[i]["head_dx"] = 3.0 * math.sin(p * 80 + ph)
            F[i]["paw_a"] = (196.0, 138.0)
            FX[i]["lean"] = 3.5 + 1.2 * math.sin(u * math.pi * 4 + ph)
    elif p < 0.62:
        k = min(1, int((p - 0.20) / 0.21))
        t = ((p - 0.20) - 0.21 * k) / 0.21
        ai = k % 2
        di = 1 - ai
        A = F[ai]
        if t <= 0.28:
            u = t / 0.28
            A["paw_a"] = (156.0 - 20.0 * u, 106.0 + 20.0 * u)
            A["head_dy"] = 4.0 * u
            FX[ai]["lean"] = -5.0 * u
            A["tail_lift"] = 1.0
            A["tail_flick"] = 1.2 * math.sin(p * 90)
            A["claw_a"] = 0.4
        elif t <= 0.46:
            u = (t - 0.28) / 0.18
            FX[ai]["dx"] = 30.0 * ease_io(u)
            FX[ai]["hop"] = 24.0 * math.sin(math.pi * u)
            FX[ai]["lean"] = 11.0 * u
            A["paw_a"] = (198.0 + 26.0 * u, 128.0 - 6.0 * u)
            A["paw_b"] = (180.0 + 22.0 * u, 160.0 - 32.0 * u)
            A["claw_a"] = 1.0
            A["claw_b"] = 0.7
            A["bristle"] = 1.0
        elif t <= 0.60:
            A["paw_a"] = (230.0, 126.0)
            A["paw_b"] = (208.0, 138.0)
            A["claw_a"] = 1.0
            A["shout"] = 1.0
            FX[ai]["dx"] = 34.0
            FX[ai]["hop"] = 3.0
        elif t <= 0.74:
            u = (t - 0.60) / 0.14
            A["paw_a"] = (200.0, 138.0)
            A["paw_b"] = (152.0 + 30.0 * u, 162.0)
            A["claw_a"] = 0.3
            A["claw_b"] = 0.5
            FX[ai]["hop"] = 3.0 * math.sin(math.pi * u)
        elif t <= 0.88:
            u = (t - 0.74) / 0.14
            A["paw_b"] = (182.0 + 52.0 * u, 170.0 - 10.0 * u)
            A["claw_b"] = 1.0
            A["shout"] = 1.0
            FX[ai]["lean"] = 11.0 + 4.0 * u
        else:
            u = (t - 0.88) / 0.12
            A["paw_b"] = (234.0 - 40.0 * u, 160.0 + 20.0 * u)
            A["claw_b"] = 0.5 * (1.0 - u)
            FX[ai]["squash"] = 1.0 - 0.08 * u
            FX[ai]["dx"] = 34.0 * (1.0 - 0.5 * u)
        A["bristle"] = max(A["bristle"], 0.85)
        A["ear"] = 1.0
        D = F[di]
        if miss_all or k == 1:
            dk1 = bell(t, 0.52, 0.74)
            D["head_dy"] = 13.0 * dk1
            D["head_tilt"] = 9.0 * dk1
            D["blink"] = 0.5 * dk1
            FX[di]["squash"] = min(FX[di]["squash"], 1.0 - 0.12 * dk1)
            FX[di]["lean"] = FX[di]["lean"] - 6.0 * dk1
        else:
            rk = bell(t, 0.55, 1.0)
            D["head_dy"] = -3.0 * rk
            FX[di]["dx"] = -18.0 * rk
            FX[di]["hop"] = 6.0 * rk
            FX[di]["lean"] = -13.0 * rk + 3.0 * math.sin(p * 120) * rk
            D["shout"] = 0.75 + 0.25 * rk
        if miss_all or k == 0:
            dk2 = bell(t, 0.80, 1.05)
            D["head_dy"] = max(D["head_dy"], 15.0 * dk2)
            D["blink"] = max(D["blink"], 0.5 * dk2)
            FX[di]["squash"] = min(FX[di]["squash"], 1.0 - 0.13 * dk2)
            FX[di]["lean"] = FX[di]["lean"] + 5.0 * dk2
        else:
            rk2 = bell(t, 0.80, 1.06)
            FX[di]["dx"] = FX[di]["dx"] - 20.0 * rk2
            FX[di]["lean"] = FX[di]["lean"] - 14.0 * rk2 + 3.0 * math.sin(p * 140) * rk2
            FX[di]["hop"] = FX[di]["hop"] + 7.0 * rk2
            D["shout"] = 0.8
            D["claw_a"] = 0.6
        D["bristle"] = 1.0
        D["tail_lift"] = 0.9
        D["tail_puff"] = 0.6
        D["tail_flick"] = math.sin(p * 70 + di)
        t1 = 0.20 + 0.21 * k + 0.21 * 0.60
        t2 = 0.20 + 0.21 * k + 0.21 * 0.86
        if miss_all:
            EVENTS.append(("whoosh", di, t1))
            EVENTS.append(("whoosh", di, t2))
        elif k == 0:
            EVENTS.append(("hit", di, t1))
            EVENTS.append(("whoosh", di, t2))
        else:
            EVENTS.append(("whoosh", di, t1))
            EVENTS.append(("hit", di, t2))
    elif p < 0.82:
        if miss_all:
            u = (p - 0.62) / 0.20
            for i in range(2):
                ph = u * math.pi * 4.0 + i * math.pi
                jab = max(0.0, math.sin(ph))
                FX[i]["dx"] = 70.0 + 26.0 * ease_io(u)
                F[i]["paw_a"] = (198.0 + 36.0 * jab, 138.0 - 12.0 * jab)
                F[i]["claw_a"] = 0.85 * jab
                F[i]["shout"] = 0.6 + 0.4 * jab
                F[i]["bristle"] = 1.0
                F[i]["ear"] = 1.0
                F[i]["tail_puff"] = 1.0
                F[i]["tail_flick"] = 0.8 * math.sin(ph * 0.6)
                F[i]["head_dx"] = 4.0
            for j in range(4):
                EVENTS.append(("whoosh", j % 2, 0.62 + 0.20 * (j + 0.5) / 4.0))
        else:
            u = (p - 0.62) / 0.20
            for i in range(2):
                ph = u * math.pi * 8.0 + i * math.pi
                FX[i]["dx"] = 96.0 + 5.0 * math.sin(ph)
                FX[i]["hop"] = 4.0 * abs(math.sin(ph * 0.5))
                FX[i]["lean"] = 6.0 + 3.0 * math.sin(ph * 0.5)
                F[i]["paw_a"] = (206.0 + 24.0 * math.sin(ph), 130.0 + 9.0 * math.cos(ph))
                F[i]["paw_b"] = (190.0 - 24.0 * math.sin(ph), 164.0 - 9.0 * math.cos(ph))
                F[i]["claw_a"] = 1.0
                F[i]["claw_b"] = 1.0
                F[i]["ear"] = 1.0
                F[i]["shout"] = 1.0
                F[i]["bristle"] = 1.0
                F[i]["tail_puff"] = 0.8
                F[i]["tail_flick"] = math.sin(ph * 0.7)
            for j in range(6):
                EVENTS.append(("hit", j % 2, 0.62 + 0.18 * (j + 0.5) / 6.0))
    else:
        if miss_all:
            v = (p - 0.82) / 0.18
            for i in range(2):
                ph = i * math.pi
                FX[i]["dx"] = 96.0
                F[i]["head_dx"] = 6.0
                F[i]["shout"] = 0.85 + 0.15 * abs(math.sin(v * math.pi * 4 + ph))
                F[i]["bristle"] = 1.0
                F[i]["ear"] = 1.0
                F[i]["tail_puff"] = 1.0
                F[i]["head_dy"] = 1.0 * math.sin(v * math.pi * 6 + ph)
                F[i]["tail_flick"] = math.sin(v * math.pi * 6 + ph)
        else:
            v = (p - 0.82) / 0.18
            if v < 0.45:
                u = v / 0.45
                for i in range(2):
                    FX[i]["dx"] = 96.0 - 56.0 * ease_io(u)
                    FX[i]["hop"] = 16.0 * math.sin(math.pi * u)
                    FX[i]["lean"] = -8.0 * u
                    F[i]["bristle"] = 0.8
            elif v < 0.62:
                for i in range(2):
                    FX[i]["dx"] = 40.0
                    FX[i]["squash"] = 1.0 - 0.08 * bell(v, 0.45, 0.75)
                    F[i]["bristle"] = 0.9
            else:
                u = (v - 0.62) / 0.38
                for i in range(2):
                    FX[i]["dx"] = 40.0 + 56.0 * ease_io(u)
                    F[i]["head_dx"] = 9.0 * ease_io(u)
                    F[i]["head_dy"] = -2.0 * ease_io(u)
                    F[i]["shout"] = 0.85 + 0.15 * abs(math.sin(p * 120))
                    F[i]["ear"] = 1.0
                    F[i]["bristle"] = 1.0
                    F[i]["tail_puff"] = 1.0
                    F[i]["tail_flick"] = math.sin(p * 100)
    return F, FX, EVENTS, zs, base_x


def _compose_cat_duel_v3(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    Võ đài mèo v3 — mỗi cảnh một ĐẠO DIỄN riêng (spec["cat_mode"]) để không cảnh nào lặp cảnh nào:
    - battle:  dạo bước vào sàn → gườm nhau → 2 hiệp tát (nhảy bổ, 2 chân, sao va chạm) → liên hoàn → mũi đối mũi
    - sloppy:  CẬN CẢNH hài — tát trượt liên tục, đối thủ né vụng về, mất thăng bằng, toát mồ hôi
    - threat:  xù lông gườm nhau, đòn giả vờ tuyệt đối không chạm (đúng lời thoại "chỉ là dọa")
    - retreat: mèo vàng quay đầu đi khuất, mèo mun nằm dài tắm nắng trong vệt nắng (đồng hồ 30 phút)
    """
    mode = spec.get("cat_mode") or "battle"
    ground_y = height - 45
    y_base = ground_y - 208
    VARIANT = ("tabby", "black")

    def bell(t, a, b):
        if t <= a or t >= b:
            return 0.0
        return math.sin(math.pi * (t - a) / (b - a))

    def ease_io(t):
        t = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * t)

    # ================== RETREAT: rút lui trong êm đềm ==================
    if mode == "retreat":
        base0, base1 = 170.0, float(width - 300)
        _draw_clock_prop(draw, 100, 108, 38, p, "30 PHÚT")
        _draw_sunbeam(draw, width, height, ground_y, p)
        sun_cx = width - 160.0
        F0 = {"head_dx": 0.0, "head_dy": 0.0, "head_tilt": 0.0, "blink": 0.0,
              "pupil_dx": 0.8, "ear": 0.5, "shout": 0.3, "bristle": 0.0,
              "paw_a": None, "paw_b": None, "claw_a": 0.0, "claw_b": 0.0,
              "foot_a": (0.0, 0.0), "foot_b": (0.0, 0.0),
              "tail_lift": 0.45, "tail_flick": 0.0, "tail_puff": 0.0}
        F1 = dict(F0)
        fx0 = {"dx": 0.0, "hop": 0.0, "lean": 3.0}
        fx1 = {"dx": 0.0, "hop": 0.0, "lean": 3.0}
        f0 = 1 if p < 0.10 else -1
        if p < 0.10:
            u = p / 0.10
            F0["bristle"] = 0.7
            F0["ear"] = 1.0
            F0["shout"] = 0.5 + 0.4 * abs(math.sin(u * math.pi * 4))
            F0["tail_puff"] = 0.7
        else:
            if p < 0.60:
                walk_x = 128.0 * ease_io((p - 0.10) / 0.50)
                walking = True
                look_back = 0.0
            elif p < 0.72:
                walk_x = 128.0
                walking = False
                look_back = ease_io((p - 0.60) / 0.12)
            else:
                u = (p - 0.72) / 0.28
                walk_x = 128.0 + 470.0 * u
                walking = u < 0.96
                look_back = max(0.0, 1.0 - u * 4.0)
            fx0["dx"] = -walk_x
            F0["head_dx"] = -14.0 * look_back
            F0["ear"] = 0.3
            F0["tail_lift"] = 0.42
            F0["tail_flick"] = 0.5 * math.sin(p * 50.0)
            F0["blink"] = 0.6 if (0.64 <= p < 0.665 or 0.68 <= p < 0.705) else 0.0
            if walking:
                ph = (p - 0.10) * math.pi * 2.0 * 5.5
                F0["foot_a"] = (9.0 * math.sin(ph), -8.0 * max(0.0, math.sin(ph)))
                F0["foot_b"] = (9.0 * math.sin(ph + math.pi), -8.0 * max(0.0, math.sin(ph + math.pi)))
                F0["paw_b"] = (168.0 + 10.0 * math.sin(ph + math.pi), 202.0 - 9.0 * max(0.0, math.sin(ph + math.pi)))
                fx0["hop"] = 2.5 * abs(math.sin(ph))
                fx0["lean"] = 4.0
        spr0 = _render_cat_sprite("tabby", facing=f0,
                                  lean=(-fx0["lean"] if f0 == 1 else fx0["lean"]), **F0)
        x0 = int(base0 + fx0["dx"])
        _draw_cat_ground_shadow(draw, x0 + 120, ground_y, half_w=66)
        img.paste(spr0, (x0, int(ground_y - 208 - fx0["hop"])), spr0)
        if 0.09 <= p < 0.17:
            _draw_dust_puff(draw, x0 + 110, ground_y - 6, 10)
        # --- mèo mun ---
        lying = p >= 0.45
        if not lying:
            if p < 0.14:
                u = p / 0.14
                F1["bristle"] = 0.7
                F1["ear"] = 1.0
                F1["shout"] = 0.5 + 0.4 * abs(math.sin(u * math.pi * 4 + 1.0))
                F1["tail_puff"] = 0.7
            elif p < 0.45:
                u = (p - 0.14) / 0.31
                fx1["dx"] = 104.0 * ease_io(u)
                ph2 = (p - 0.14) * math.pi * 2.0 * 6.5
                F1["foot_a"] = (9.0 * math.sin(ph2), -8.0 * max(0.0, math.sin(ph2)))
                F1["foot_b"] = (9.0 * math.sin(ph2 + math.pi), -8.0 * max(0.0, math.sin(ph2 + math.pi)))
                F1["paw_b"] = (168.0 + 10.0 * math.sin(ph2 + math.pi), 202.0 - 9.0 * max(0.0, math.sin(ph2 + math.pi)))
                fx1["hop"] = 2.5 * abs(math.sin(ph2))
                F1["ear"] = 0.4
                F1["bristle"] = 0.3
                F1["tail_lift"] = 0.5
                F1["tail_flick"] = 0.4 * math.sin(p * 60)
            spr1 = _render_cat_sprite("black", facing=-1, lean=fx1["lean"], **F1)
            x1 = int(base1 + fx1["dx"])
            _draw_cat_ground_shadow(draw, x1 + 120, ground_y, half_w=66)
            img.paste(spr1, (x1, int(ground_y - 208 - fx1["hop"])), spr1)
        else:
            ly = _draw_cat_lying("black", breathe=(p * 1.7) % 1.0,
                                 tail_flick=0.7 * math.sin(p * 9.0))
            swayx = int(sun_cx - 150 + 5.0 * math.sin(p * 3.2))
            _draw_cat_ground_shadow(draw, sun_cx, ground_y, half_w=104, layers=2)
            img.paste(ly, (swayx, int(ground_y - 130)), ly)
            if 0.42 <= p < 0.52:
                _draw_dust_puff(draw, sun_cx, ground_y - 6, 13)
            if p > 0.64:
                fz = _get_font(20, bold=True)
                for k in range(3):
                    zk = ((p - 0.64) * 2.0 + k * 0.33) % 1.0
                    draw.text((sun_cx + 26 + 30 * zk + 8 * math.sin(zk * 6 + k),
                               262 - 56 * zk), "z" if k % 2 == 0 else "Z", font=fz, fill=BLACK)
        _draw_cat_ground(draw, width, ground_y)
        return

    # ================== BATTLE / THREAT / SLOPPY ==================
    F, FX, EVENTS, zs, base_x = _cat_duel_choreo(p, mode, width)

    placed = []
    for i in range(2):
        facing = 1 if i == 0 else -1
        spr = _render_cat_sprite(VARIANT[i], facing=facing,
                                 lean=(-FX[i]["lean"] if i == 0 else FX[i]["lean"]),
                                 squash=FX[i]["squash"], **F[i])
        X = base_x[i] + FX[i]["dx"]
        if abs(zs - 1.0) > 0.005:
            spr = spr.resize((max(2, int(round(spr.width * zs))),
                              max(2, int(round(spr.height * zs)))), Image.Resampling.LANCZOS)
            X = width / 2.0 + (X + 120.0 - width / 2.0) * zs - 120.0 * zs
        scale_y = spr.height / 224.0
        y_top = int(ground_y - 208.0 * scale_y - FX[i]["hop"])
        placed.append((int(X), y_top, spr, int(X) + 120.0 * zs))
    for (_xl, _yt, _spr, cx_) in placed:
        _draw_cat_ground_shadow(draw, cx_, ground_y, half_w=int(66 * zs))
    centers = []
    for (xl_, yt_, spr_, cx_) in placed:
        img.paste(spr_, (xl_, yt_), spr_)
        centers.append(cx_)

    # ---- Hiệu ứng đòn đánh: sao trúng đòn / vệt gió trượt ----
    for kind, side, t_ev in EVENTS:
        near = 1.0 - min(1.0, abs(p - t_ev) / 0.055)
        if near <= 0.0:
            continue
        cxs = centers[side]
        if kind == "hit":
            star_scale = 0.55 if (mode == "battle" and p >= 0.62) else 1.0
            r = max(6, int((12 + 26 * near) * star_scale))
            _draw_impact_star(draw, int(cxs) + int((30 if side == 0 else -30) * zs),
                              int(ground_y - 158 * zs), r, rot=p * 3.0)
        else:
            _draw_whoosh_arcs(draw, int(cxs) + int((74 if side == 0 else -74) * zs),
                              int(ground_y - 116 * zs), int((30 + 10 * near) * (1.0 + 0.3 * (zs - 1.0))), near)

    # ---- Bụi tiếp đất + vệt tốc độ khi nhảy bổ (battle) ----
    if mode == "battle":
        if 0.20 <= p < 0.62:
            k = min(1, int((p - 0.20) / 0.21))
            t = ((p - 0.20) - 0.21 * k) / 0.21
            ai = k % 2
            dust = bell(t, 0.80, 1.0)
            if dust > 0.05:
                _draw_dust_puff(draw, int(centers[ai]), ground_y - 8, int(6 + 16 * dust))
            spd = bell(t, 0.30, 0.58)
            if spd > 0.15:
                for s_i in range(3):
                    ly = y_base + 54 + s_i * 40
                    ln = int((26 + 18 * spd + s_i * 5) * zs)
                    if ai == 0:
                        xx = int(centers[0] - 120 * zs)
                        draw.line([(xx - ln, ly), (xx, ly)], fill=BLACK, width=3)
                    else:
                        xx = int(centers[1] + 120 * zs)
                        draw.line([(xx, ly), (xx + ln, ly)], fill=BLACK, width=3)
        elif 0.62 <= p < 0.82:
            ph = ((p - 0.62) / 0.20) * math.pi * 8.0
            for i in range(2):
                d = 0.5 + 0.5 * math.sin(ph + i * math.pi)
                if d > 0.4:
                    _draw_dust_puff(draw, int(centers[i]), ground_y - 6, int(4 + 10 * d))
        elif p >= 0.82 and p < 0.95:
            v = (p - 0.82) / 0.18
            d = bell(v, 0.42, 0.68)
            if d > 0.05:
                for i in range(2):
                    _draw_dust_puff(draw, int(centers[i]), ground_y - 6, int(5 + 14 * d))

    # ---- SLOPPY: giọt mồ hôi + lông tơ bay ----
    if mode == "sloppy":
        if p >= 0.52:
            hx0 = int(centers[0] + 64 * zs)
            dy_ = int(ground_y - 150 * zs - 6 * math.sin(p * 30.0))
            _draw_sweat_drop(draw, hx0 + 30, dy_, 1.0)
            if p >= 0.74:
                _draw_sweat_drop(draw, hx0 + 52, dy_ + 14, 0.8)
        for k in range(4):
            fk = (p * 1.1 + k * 0.27) % 1.0
            fx_ = int(140 + k * 130 + 20 * math.sin(fk * 9 + k))
            fy_ = int(y_base + 60 - 44 * fk)
            draw.line([(fx_ - 6, fy_), (fx_ + 6, fy_ - 4)], fill=BLACK, width=2)
            draw.line([(fx_ + 2, fy_ + 3), (fx_ + 12, fy_)], fill=BLACK, width=2)

    _draw_cat_ground(draw, width, ground_y)


def _compose_cat_food_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    Mèo và ĐĨA THỨC ĂN (cảnh "bí mật bất ngờ"):
    - Mèo vàng đứng cạnh đĩa đầy → GẠT đĩa sang một bên (0.12–0.60)
    - Rồi CHỈ CHÂN về góc nắng (0.60–1.0) — tiết lộ điều mèo thật sự muốn
    """
    def ease_io(t):
        t = max(0.0, min(1.0, t))
        return 0.5 - 0.5 * math.cos(math.pi * t)

    def bell(t, a, b):
        if t <= a or t >= b:
            return 0.0
        return math.sin(math.pi * (t - a) / (b - a))

    ground_y = height - 45
    y_base = ground_y - 208
    cat_x = 205.0
    _draw_sunbeam(draw, width, height, ground_y, p)

    # đĩa bị gạt sang phải dần (vẫn ĐẦY thức ăn — mèo gạt đi dù đĩa đầy)
    slide = 130.0 * ease_io((p - 0.16) / 0.50) if p > 0.16 else 0.0
    dish_cx = 445.0 + slide
    _draw_dish_prop(draw, dish_cx, ground_y - 16, food=True)

    # vụn đồ ăn vương vãi sau cú gạt
    if p > 0.45:
        for k, (ox, oy) in enumerate(((-96, -4), (-82, -10), (-108, -9))):
            bx = dish_cx + ox
            draw.ellipse([bx - 4, ground_y + oy - 4, bx + 4, ground_y + oy + 4], fill=BLACK)

    # pose mèo: tì đẩy → chỉ chân
    pose = dict(ear=0.4, shout=0.15, blink=0.0, pupil_dx=0.9, pupil_dy=0.0,
                tail_lift=0.55, tail_flick=0.5 * math.sin(p * 30.0))
    lean = 2.0
    if p < 0.12:
        pose["head_dx"] = 4.0
        pose["pupil_dy"] = 0.3
        pose["paw_a"] = (176.0, 160.0)
    elif p < 0.60:
        u = (p - 0.12) / 0.48
        tì = math.sin(math.pi * min(1.0, u / 0.7))
        lean = 3.0 + 5.0 * tì
        lip_local = min(190.0, (dish_cx - 54.0) - cat_x)
        pose["paw_a"] = (lip_local, 186.0 - 6.0 * tì)
        pose["claw_a"] = 0.0
        pose["head_dy"] = 3.0 * tì
        pose["shout"] = 0.1 + 0.15 * tì
    else:
        u = (p - 0.60) / 0.40
        sweep = ease_io(min(1.0, u * 1.6))
        pose["paw_a"] = (150.0 + 78.0 * sweep, 146.0 + 6.0 * sweep)
        pose["claw_a"] = 0.55 * sweep
        pose["head_dx"] = 10.0 * sweep
        pose["head_dy"] = -12.0 * sweep
        pose["head_tilt"] = 8.0 * sweep
        pose["pupil_dx"] = 1.0
        pose["pupil_dy"] = -1.0
        pose["ear"] = 0.15
        pose["tail_lift"] = 0.85
        lean = 2.0 - 3.0 * sweep
        if 0.68 <= p < 0.80:
            _draw_impact_star(draw, int(cat_x + 120), int(y_base + 34),
                              max(5, int(10 * bell(p, 0.68, 0.80))), rot=p * 4.0)
        if sweep > 0.45:
            # mũi tên nét đứt từ bàn chân chỉ thẳng về góc nắng
            pt_end = _reach_target((160.0, 130.0), pose["paw_a"], 27.0, 27.0)
            sx0, sy0 = cat_x + pt_end[0], y_base + pt_end[1]
            ex0, ey0 = float(width - 78), 96.0
            for f0 in (0.10, 0.30, 0.50):
                dx0 = sx0 + (ex0 - sx0) * f0
                dy0 = sy0 + (ey0 - sy0) * f0
                dx1 = sx0 + (ex0 - sx0) * (f0 + 0.085)
                dy1 = sy0 + (ey0 - sy0) * (f0 + 0.085)
                draw.line([(dx0, dy0), (dx1, dy1)], fill=BLACK, width=3)
            ar = math.atan2(ey0 - sy0, ex0 - sx0)
            ax0 = sx0 + (ex0 - sx0) * 0.78
            ay0 = sy0 + (ey0 - sy0) * 0.78
            draw.polygon([(ax0 + math.cos(ar) * 16, ay0 + math.sin(ar) * 16),
                          (ax0 + math.cos(ar + 2.5) * 12, ay0 + math.sin(ar + 2.5) * 12),
                          (ax0 + math.cos(ar - 2.5) * 12, ay0 + math.sin(ar - 2.5) * 12)],
                         fill=BLACK)
    spr = _render_cat_sprite("tabby", facing=1, lean=lean, **pose)
    _draw_cat_ground_shadow(draw, cat_x + 120, ground_y, half_w=66)
    img.paste(spr, (int(cat_x), int(ground_y - 208)), spr)
    _draw_cat_ground(draw, width, ground_y)


def _compose_microscope_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    KÍNH PHÓNG ĐẠI soi mèo gầm ghè (cảnh "80% tâm lý chiến"):
    - Mèo vàng há miệng gầm ghè bên trong thấu kính, lông xù, móng bung
    - Sóng âm lan ra từ miệng + ánh loé trên mặt kính
    """
    ground_y = height - 45
    cx, cy, r = width // 2 - 30, height // 2 + 16, 150

    pose = dict(shout=0.7 + 0.3 * abs(math.sin(p * 14.0)),
                bristle=0.75, ear=0.85,
                head_tilt=3.0 * math.sin(p * 26.0),
                head_dy=2.0 * math.sin(p * 18.0),
                pupil_dx=1.0,
                paw_a=(208.0, 124.0), claw_a=0.35,
                paw_b=(168.0, 202.0),
                tail_lift=0.85, tail_flick=0.9 * math.sin(p * 70.0), tail_puff=0.7,
                blink=0.25 * max(0.0, math.sin(p * 30.0)))
    spr = _render_cat_sprite("tabby", facing=1, lean=2.0, **pose)
    img.paste(spr, (int(cx - 120), int(ground_y - 208 - 6)), spr)

    # sóng âm từ miệng mèo
    _draw_sound_waves(draw, cx + 104, cy + 40, p)

    # thấu kính + tay cầm
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLACK, width=6)
    draw.ellipse([cx - r + 14, cy - r + 14, cx + r - 14, cy + r - 14], outline=BLACK, width=2)
    hx1, hy1 = cx + int(r * 0.71), cy + int(r * 0.71)
    hx2, hy2 = hx1 + 76, hy1 + 76
    draw.line([(hx1, hy1), (hx2, hy2)], fill=BLACK, width=16)
    draw.ellipse([hx2 - 10, hy2 - 10, hx2 + 10, hy2 + 10], fill=BLACK)
    gx = cx - 84 + int(6 * math.sin(p * 8.0))
    draw.line([(gx, cy - 92), (gx + 42, cy - 50)], fill=BLACK, width=6)
    draw.line([(gx + 52, cy - 42), (gx + 70, cy - 24)], fill=BLACK, width=4)

    _draw_cat_ground(draw, width, ground_y)


def _compose_cat_comment_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    Mèo dễ thương mời BÌNH LUẬN (cảnh CTA "chia sẻ câu chuyện"):
    - Mèo vàng đứng cạnh khung bình luận, một chân giữ khung, đung đưa nhẹ
    - Khung có trái tim + các dòng bình luận, tim bay lên quanh khung, mắt nháy tự nhiên
    """
    ground_y = height - 45
    y_base = ground_y - 208
    sway = 2.2 * math.sin(p * math.pi * 2.4)
    blink = max(0.0, 1.0 - abs(((p * 2.2) % 0.5) - 0.25) / 0.055)
    pose = dict(head_tilt=3.0 + sway * 0.6, blink=blink, pupil_dx=0.5, pupil_dy=-0.2,
                ear=0.25, shout=0.12,
                paw_a=(171.0, 78.0), claw_a=0.0, paw_b=(172.0, 200.0),
                tail_lift=0.75, tail_flick=0.5 * math.sin(p * 30.0))
    spr = _render_cat_sprite("tabby", facing=1, lean=-1.2 + 0.6 * sway, **pose)
    _draw_cat_ground_shadow(draw, 250 + 120, ground_y, half_w=66)
    img.paste(spr, (int(250 + sway), int(ground_y - 208)), spr)

    # khung bình luận (đung đưa nhẹ)
    bob = 4.0 * math.sin(p * math.pi * 2.0)
    bx0, by0 = 420.0, 118.0 + bob
    bx1, by1 = 692.0, 268.0 + bob
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=18, fill=WHITE, outline=BLACK, width=4)
    draw.polygon([(bx0 + 34, by1 - 2), (bx0 + 10, by1 + 36), (bx0 + 78, by1 - 2)],
                 fill=WHITE, outline=BLACK, width=4)
    _draw_heart(draw, bx0 + 58, by0 + 78, 2.6)
    for k, ln in enumerate((0.86, 0.62, 0.74)):
        yl = by0 + 44 + k * 38
        draw.line([(bx0 + 112, yl), (bx0 + 112 + (bx1 - bx0 - 140) * ln, yl)],
                  fill=BLACK, width=7)

    # tim bay lên quanh khung
    for k in range(3):
        hk = (p * 1.15 + k * 0.34) % 1.0
        hx_ = int(632 + 40 * math.sin(hk * 5.5 + k * 2.1))
        hy_ = int(320 - 230 * hk)
        _draw_heart(draw, hx_, hy_, max(0.7, 2.2 - 1.6 * hk))

    _draw_cat_ground(draw, width, ground_y)


def _compose_brain_duel_scene(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    scene: Dict[str, Any],
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    HAI BỘ NÃO MÈO đấu trí nối nhau bằng TIA CHỚP (cảnh "đấu trí tránh thương"):
    - 2 bộ não có TAI MÈO đối diện nhau, tia chớp giật liên tục ở giữa
    - Tia lửa điện bắn ra hai bên, khác hẳn cảnh não đơn + bánh răng
    """
    from ai.doodle_engine import fetch_vector_icon

    cx = width // 2
    lx, rx = cx - 216, cx + 66
    ly = ry = height // 2 - 68
    b1 = fetch_vector_icon("brain", size=150, color="#000000")
    if b1:
        img.paste(b1, (lx, ly), b1)
    b2 = fetch_vector_icon("brain", size=150, color="#000000")
    if b2:
        img.paste(ImageOps.mirror(b2), (rx, ry), b2)

    # tai mèo trên đỉnh mỗi bộ não
    for (bx, by, flip) in ((lx, ly, 1), (rx, ry, -1)):
        for exo in (30, 92):
            x0 = bx + (exo if flip == 1 else 150 - exo)
            draw.polygon([(x0 - 14, by + 16), (x0 - 2, by - 18), (x0 + 14, by + 12)],
                         fill=WHITE, outline=BLACK, width=3)
            draw.line([(x0 - 6, by + 10), (x0 - 1, by - 6)], fill=BLACK, width=2)

    # tia chớp giữa hai bộ não
    _draw_bolt(draw, lx + 148, ly + 86, rx - 2, ry + 112, p, segs=5, w=9)
    for k in range(3):
        sx = cx - 60 + 30 * k + int(8 * math.sin(p * 40.0 + k * 2.0))
        sy = ly + 40 + int(84 * ((p * 1.4 + k * 0.33) % 1.0))
        _draw_impact_star(draw, sx, sy, 8, rot=p * 6.0)

    _draw_cat_ground(draw, width, height - 45)


def _compose_pie_feint_cats(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    spec: Dict[str, Any],
    width: int,
    height: int,
    p: float
):
    """
    2 mèo GIẢ VỜ TÁT hai bên biểu đồ (concept cảnh "90% chỉ là dọa"):
    - mèo vàng (trái) và mèo mun (phải) đứng hai bên chân biểu đồ, thay phiên vung chân
      tát vào KHOẢNG KHÔNG giữa hai mặt — kèm vệt gió chứng minh "tát hụt".
    """
    if not spec.get("pie_cats"):
        return
    ground_y = height - 45
    zs = 0.62
    cyc = p * 2.0
    for idx in (0, 1):
        jab = max(0.0, math.sin((cyc + (0.5 if idx == 1 else 0.0)) * math.pi * 2.0))
        pose = dict(ear=0.4, shout=0.25 + 0.25 * jab, pupil_dx=0.9,
                    paw_a=(198.0 + 36.0 * jab, 150.0 - 24.0 * jab),
                    claw_a=0.5 * jab,
                    tail_lift=0.5, tail_flick=0.6 * math.sin(cyc * 6.28 + idx * 2.0))
        spr = _render_cat_sprite("tabby" if idx == 0 else "black",
                                 facing=(1 if idx == 0 else -1),
                                 lean=(-4.0 * jab if idx == 0 else 4.0 * jab), **pose)
        spr = spr.resize((max(2, int(spr.width * zs)), max(2, int(spr.height * zs))),
                         Image.Resampling.LANCZOS)
        X = 112.0 if idx == 0 else width - 112.0 - spr.width
        _draw_cat_ground_shadow(draw, X + spr.width / 2.0, ground_y,
                                half_w=int(48 * zs / 0.5), layers=2)
        img.paste(spr, (int(X), int(ground_y - 208.0 * zs)), spr)
        if jab > 0.55:
            _draw_whoosh_arcs(draw, width // 2 + (-110 if idx == 0 else 110),
                              ground_y - 51, int(16 + 8 * jab), jab)
    _draw_cat_ground(draw, width, ground_y)


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

    lunge = int(math.sin(p * math.pi * 3.0) * 32)
    bob = int(abs(math.sin(p * math.pi * 6.0)) * 10)

    text_blob = " ".join(
        str(scene.get(k) or "")
        for k in ("narration", "visual_concept", "doodle_label", "visual_type", "image_prompt")
    ).lower()
    has_cat = kw1.lower() == "cat" or kw2.lower() == "cat" or "mèo" in text_blob or "cat" in text_blob

    if spec.get("action_type") == "fight" and has_cat:
        # 2 CHIẾN BINH MÈO: đạo diễn v3 theo spec["cat_mode"] (battle / sloppy / threat)
        _compose_cat_duel_v3(img, draw, scene, spec, width, height, p)
        return

    # 2 icon tương tác đối đầu (trường hợp không phải mèo đánh nhau)
    im1 = fetch_vector_icon(kw1, size=175, color="#000000")
    im2 = fetch_vector_icon(kw2, size=175, color="#000000")
    if im1 and im2 and kw1 == kw2:
        im2 = ImageOps.mirror(im2)

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
        _compose_pie_feint_cats(img, draw, spec, width, height, phase_progress)
    elif action_type == "wallet_squeeze":
        _compose_wallet_squeeze_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "chart_bar":
        _compose_bar_chart_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "brain_think":
        _compose_brain_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "microscope":
        _compose_microscope_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "cat_food":
        _compose_cat_food_scene(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "cat_retreat":
        _compose_cat_duel_v3(img, draw, scene, spec, width, height, phase_progress)
    elif action_type == "cat_comment":
        _compose_cat_comment_scene(img, draw, scene, spec, width, height, phase_progress)
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
