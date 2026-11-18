"""Tạo bộ sưu tập 12 hình ảnh nền và khung đồ họa chất lượng cao (1080x1920) cho thumbnail và đóng khung video."""
import math
import random
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1080, 1920

out_dirs = [
    Path(r"D:\workspace\background_images"),
    Path(r"C:\tool v1\workspace\background_images"),
]
for d in out_dirs:
    d.mkdir(parents=True, exist_ok=True)


def add_sparkles_and_stars(img: Image.Image, num_stars=60, color=(255, 255, 200), max_size=8):
    """Vẽ thêm các hạt ánh sao và tia lấp lánh (bokeh/sparkles)."""
    draw = ImageDraw.Draw(img, "RGBA")
    random.seed(42)
    for _ in range(num_stars):
        x = random.randint(20, W - 20)
        y = random.randint(20, H - 20)
        r = random.uniform(1.5, max_size)
        alpha = random.randint(120, 240)
        c = (color[0], color[1], color[2], alpha)
        # Glow
        draw.ellipse([x - r * 2, y - r * 2, x + r * 2, y + r * 2], fill=(color[0], color[1], color[2], alpha // 4))
        # Center star
        draw.ellipse([x - r, y - r, x + r, y + r], fill=c)
        # 4-point cross for larger stars
        if r > 4:
            draw.line([x - r * 3, y, x + r * 3, y], fill=(color[0], color[1], color[2], alpha // 2), width=1)
            draw.line([x, y - r * 3, x, y + r * 3], fill=(color[0], color[1], color[2], alpha // 2), width=1)
    return img


def save_bg(img: Image.Image, filename: str):
    """Lưu ảnh vào tất cả các thư mục đích với chất lượng cao."""
    for d in out_dirs:
        p = d / filename
        img.convert("RGB").save(str(p), quality=94, optimize=True)
    print(f"✅ Đã tạo: {filename}")


# 1. TIA SÁNG VÀNG NGÔI SAO (Gold Starburst)
def gen_gold_starburst():
    y, x = np.ogrid[:H, :W]
    cx, cy = W / 2.0, H * 0.45
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    max_d = math.hypot(cx, cy)
    norm_d = np.clip(dist / max_d, 0, 1)

    angles = np.arctan2(dy, dx)
    rays = ((np.cos(angles * 28) + 1.0) / 2.0) ** 1.8

    # Colors: Vàng rực sáng tâm -> Cam đậm -> Nâu vàng tối ở góc
    c_center = np.array([255, 230, 110], dtype=np.float32)
    c_mid = np.array([220, 130, 15], dtype=np.float32)
    c_edge = np.array([70, 30, 5], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.5,
                        c_center[ch] * (1 - norm_d * 2) + c_mid[ch] * (norm_d * 2),
                        c_mid[ch] * (2 - norm_d * 2) + c_edge[ch] * (norm_d * 2 - 1))
        ray_add = (255 - c_mid[ch]) * rays * 0.45 * (1.0 - norm_d * 0.6)
        img_arr[:, :, ch] = np.clip(base + ray_add, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=80, color=(255, 245, 180), max_size=7)
    save_bg(img, "tia_sang_vang_ngoi_sao.jpg")


# 2. TIA SÁNG XANH NEON CYBERPUNK (Cyan Neon Burst)
def gen_cyan_neon():
    y, x = np.ogrid[:H, :W]
    cx, cy = W / 2.0, H * 0.45
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    angles = np.arctan2(dy, dx)
    rays = ((np.cos(angles * 24) + 1.0) / 2.0) ** 2.0

    c_center = np.array([160, 245, 255], dtype=np.float32)
    c_mid = np.array([6, 140, 210], dtype=np.float32)
    c_edge = np.array([4, 25, 60], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.5,
                        c_center[ch] * (1 - norm_d * 2) + c_mid[ch] * (norm_d * 2),
                        c_mid[ch] * (2 - norm_d * 2) + c_edge[ch] * (norm_d * 2 - 1))
        ray_add = 120 * rays * (1.0 - norm_d * 0.5)
        img_arr[:, :, ch] = np.clip(base + ray_add, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=70, color=(180, 250, 255), max_size=6)
    save_bg(img, "tia_sang_xanh_neon.jpg")


# 3. HOÀNG HÔN ĐỎ CAM RỰC LỬA (Sunset Fire Flame)
def gen_sunset_fire():
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.35
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    norm_d = np.clip(dist / math.hypot(W, H), 0, 1)

    angles = np.arctan2(dy, dx)
    rays = ((np.cos(angles * 20) + 1.0) / 2.0) ** 1.6

    c_center = np.array([255, 220, 80], dtype=np.float32)
    c_mid = np.array([230, 65, 10], dtype=np.float32)
    c_edge = np.array([55, 8, 12], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.4,
                        c_center[ch] * (1 - norm_d * 2.5) + c_mid[ch] * (norm_d * 2.5),
                        c_mid[ch] * (1.67 - norm_d * 1.67) + c_edge[ch] * (norm_d * 1.67 - 0.67))
        ray_add = 110 * rays * (1.0 - norm_d * 0.6)
        img_arr[:, :, ch] = np.clip(base + ray_add, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=60, color=(255, 230, 160), max_size=6)
    save_bg(img, "hoang_hon_do_cam.jpg")


# 4. TÍM KHÓI GALAXY HUYỀN ẢO (Purple Nebula Galaxy)
def gen_purple_galaxy():
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.5
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    c_center = np.array([235, 190, 255], dtype=np.float32)
    c_mid = np.array([130, 45, 200], dtype=np.float32)
    c_edge = np.array([20, 10, 45], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.5,
                        c_center[ch] * (1 - norm_d * 2) + c_mid[ch] * (norm_d * 2),
                        c_mid[ch] * (2 - norm_d * 2) + c_edge[ch] * (norm_d * 2 - 1))
        # Add cosmic noise/cloud wave
        cloud = np.sin(x / 90.0) * np.cos(y / 110.0) * 25.0
        img_arr[:, :, ch] = np.clip(base + cloud, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=90, color=(240, 220, 255), max_size=7)
    save_bg(img, "tim_khoi_galaxy.jpg")


# 5. XANH NGỌC EMERALD (Emerald Luxury)
def gen_emerald_luxury():
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.45
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    c_center = np.array([170, 255, 220], dtype=np.float32)
    c_mid = np.array([16, 145, 95], dtype=np.float32)
    c_edge = np.array([6, 35, 22], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.5,
                        c_center[ch] * (1 - norm_d * 2) + c_mid[ch] * (norm_d * 2),
                        c_mid[ch] * (2 - norm_d * 2) + c_edge[ch] * (norm_d * 2 - 1))
        img_arr[:, :, ch] = np.clip(base, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=55, color=(210, 255, 240), max_size=6)
    save_bg(img, "xanh_ngoc_emerald.jpg")


# 6. HỒNG & TÍM PASTEL (Sweet Pastel Dream)
def gen_pastel_dream():
    y, x = np.ogrid[:H, :W]
    # Diagonal gradient from top-left (Pink) to bottom-right (Lilac Purple)
    diag = (x / float(W) + y / float(H)) / 2.0
    c_top = np.array([255, 175, 210], dtype=np.float32)
    c_bot = np.array([170, 140, 240], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        img_arr[:, :, ch] = c_top[ch] * (1 - diag) + c_bot[ch] * diag

    img = Image.fromarray(img_arr.astype(np.uint8))
    # Soft bokeh circles
    draw = ImageDraw.Draw(img, "RGBA")
    random.seed(101)
    for _ in range(30):
        bx = random.randint(0, W)
        by = random.randint(0, H)
        br = random.randint(30, 90)
        draw.ellipse([bx - br, by - br, bx + br, by + br], fill=(255, 255, 255, random.randint(25, 60)))
    img = img.filter(ImageFilter.GaussianBlur(radius=6))
    save_bg(img, "hong_pastel_mong_mo.jpg")


# 7. XÁM CARBON TITAN (Titan Minimalist Dark)
def gen_carbon_titan():
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.5
    dist = np.sqrt((x - cx)**2 + (y - cy)**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    c_center = np.array([85, 95, 110], dtype=np.float32)
    c_edge = np.array([18, 22, 28], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = c_center[ch] * (1 - norm_d * 0.8) + c_edge[ch] * (norm_d * 0.8)
        # Subtle carbon texture
        pattern = ((x % 6 == 0) | (y % 6 == 0)).astype(np.float32) * 5.0
        img_arr[:, :, ch] = np.clip(base + pattern, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    save_bg(img, "xam_carbon_titan.jpg")


# 8. VÀNG ĐỒNG HOÀNG GIA (Royal Metallic Gold)
def gen_metallic_gold():
    y, x = np.ogrid[:H, :W]
    # Diagonal luxury sheen
    diag = (x * 1.5 + y) / (W * 1.5 + H)
    sheen = (np.sin(diag * 18.0) + 1.0) / 2.0

    c_base = np.array([195, 140, 40], dtype=np.float32)
    c_dark = np.array([90, 55, 12], dtype=np.float32)
    c_bright = np.array([255, 235, 140], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        mid = c_base[ch] * (1 - sheen * 0.6) + c_bright[ch] * (sheen * 0.6)
        vignette = (np.sqrt((x - W/2)**2 + (y - H/2)**2) / math.hypot(W/2, H/2)) * 0.4
        final_c = mid * (1 - vignette) + c_dark[ch] * vignette
        img_arr[:, :, ch] = np.clip(final_c, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=45, color=(255, 245, 200), max_size=5)
    save_bg(img, "vang_dong_metallic.jpg")


# 9. KHUNG ĐIỆN ẢNH CINEMATIC LETTERBOX (Movie Poster Frame)
def gen_cinematic_letterbox():
    # Nền đen obsidian với ánh sáng vàng điện ảnh ở giữa
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.5
    dist = np.sqrt((x - cx)**2 + (y - cy)**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    c_center = np.array([45, 38, 30], dtype=np.float32)
    c_edge = np.array([10, 10, 12], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        img_arr[:, :, ch] = c_center[ch] * (1 - norm_d) + c_edge[ch] * norm_d

    img = Image.fromarray(img_arr.astype(np.uint8))
    draw = ImageDraw.Draw(img, "RGBA")

    # Dải viền đen điện ảnh trên và dưới (Top & Bottom Cinema Letterbox)
    bar_h = 320
    draw.rectangle([0, 0, W, bar_h], fill=(12, 14, 18, 255))
    draw.rectangle([0, H - bar_h, W, H], fill=(12, 14, 18, 255))

    # Viền chỉ vàng sang trọng phân tách letterbox
    draw.line([0, bar_h, W, bar_h], fill=(245, 158, 11, 230), width=4)
    draw.line([0, H - bar_h, W, H - bar_h], fill=(245, 158, 11, 230), width=4)

    # Đổ bóng nhẹ phía dưới viền vàng
    draw.line([0, bar_h + 3, W, bar_h + 3], fill=(0, 0, 0, 140), width=2)
    draw.line([0, H - bar_h - 3, W, H - bar_h - 3], fill=(0, 0, 0, 140), width=2)

    save_bg(img, "khung_dien_anh_cinema.jpg")


# 10. KHUNG VIỀN GIẬT TÍT ĐỎ ĐEN (Aggressive Red & Dark Banner)
def gen_aggressive_red_black():
    img = Image.new("RGBA", (W, H), (15, 17, 23, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    # Dải banner đỏ rực rỡ ở trên (Top Breaking News Banner)
    top_h = 360
    # Gradient red for top
    for row in range(top_h):
        r_col = int(220 - (row / top_h) * 50)
        draw.line([0, row, W, row], fill=(r_col, 25, 30, 255))

    # Dải viền vàng neon ngăn cách
    draw.line([0, top_h, W, top_h], fill=(250, 204, 21, 255), width=6)

    # Bottom banner zone
    bot_h = 280
    for row in range(H - bot_h, H):
        factor = (row - (H - bot_h)) / bot_h
        draw.line([0, row, W, row], fill=(int(20 + factor * 20), int(22 + factor * 20), int(30 + factor * 30), 255))
    draw.line([0, H - bot_h, W, H - bot_h], fill=(239, 68, 68, 255), width=4)

    save_bg(img, "khung_giat_tit_do_den.jpg")


# 11. KHUNG VIỀN PHÁT SÁNG NEON (Dual Neon Glow Border)
def gen_neon_glow_border():
    img = Image.new("RGBA", (W, H), (11, 15, 23, 255))
    draw = ImageDraw.Draw(img, "RGBA")

    # Viền neon kép quanh toàn bộ khung hình: Cyan bên ngoài, Magenta bên trong
    border_inset = 24
    draw.rectangle([border_inset, border_inset, W - border_inset, H - border_inset],
                   outline=(56, 189, 248, 220), width=5)
    draw.rectangle([border_inset + 8, border_inset + 8, W - border_inset - 8, H - border_inset - 8],
                   outline=(236, 72, 153, 160), width=3)

    # Corner glow brackets (4 góc góc vuông phong cách CapCut)
    bracket_len = 80
    bracket_w = 7
    col_bracket = (250, 204, 21, 255)
    # Top-Left
    draw.line([border_inset, border_inset, border_inset + bracket_len, border_inset], fill=col_bracket, width=bracket_w)
    draw.line([border_inset, border_inset, border_inset, border_inset + bracket_len], fill=col_bracket, width=bracket_w)
    # Top-Right
    draw.line([W - border_inset - bracket_len, border_inset, W - border_inset, border_inset], fill=col_bracket, width=bracket_w)
    draw.line([W - border_inset, border_inset, W - border_inset, border_inset + bracket_len], fill=col_bracket, width=bracket_w)
    # Bottom-Left
    draw.line([border_inset, H - border_inset, border_inset + bracket_len, H - border_inset], fill=col_bracket, width=bracket_w)
    draw.line([border_inset, H - border_inset, border_inset, H - border_inset - bracket_len], fill=col_bracket, width=bracket_w)
    # Bottom-Right
    draw.line([W - border_inset - bracket_len, H - border_inset, W - border_inset, H - border_inset], fill=col_bracket, width=bracket_w)
    draw.line([W - border_inset, H - border_inset, W - border_inset, H - border_inset - bracket_len], fill=col_bracket, width=bracket_w)

    save_bg(img, "khung_neon_glow_border.jpg")


# 12. XANH ĐẠI DƯƠNG BIỂN SÂU (Deep Ocean Blue)
def gen_deep_ocean():
    y, x = np.ogrid[:H, :W]
    cx, cy = W * 0.5, H * 0.3
    dx, dy = x - cx, y - cy
    dist = np.sqrt(dx**2 + dy**2)
    norm_d = np.clip(dist / math.hypot(cx, cy), 0, 1)

    c_center = np.array([20, 160, 220], dtype=np.float32)
    c_mid = np.array([8, 65, 120], dtype=np.float32)
    c_edge = np.array([3, 16, 38], dtype=np.float32)

    img_arr = np.zeros((H, W, 3), dtype=np.float32)
    for ch in range(3):
        base = np.where(norm_d < 0.5,
                        c_center[ch] * (1 - norm_d * 2) + c_mid[ch] * (norm_d * 2),
                        c_mid[ch] * (2 - norm_d * 2) + c_edge[ch] * (norm_d * 2 - 1))
        # God rays from above
        rays = (np.sin((x + y * 0.3) / 75.0) + 1.0) * 18.0 * (1.0 - norm_d * 0.7)
        img_arr[:, :, ch] = np.clip(base + rays, 0, 255)

    img = Image.fromarray(img_arr.astype(np.uint8))
    img = add_sparkles_and_stars(img, num_stars=60, color=(160, 240, 255), max_size=5)
    save_bg(img, "xanh_dai_duong_deepsea.jpg")


if __name__ == "__main__":
    print("🚀 Bắt đầu tạo 12 ảnh khung nền đẹp chuẩn 1080x1920 HD...")
    gen_gold_starburst()
    gen_cyan_neon()
    gen_sunset_fire()
    gen_purple_galaxy()
    gen_emerald_luxury()
    gen_pastel_dream()
    gen_carbon_titan()
    gen_metallic_gold()
    gen_cinematic_letterbox()
    gen_aggressive_red_black()
    gen_neon_glow_border()
    gen_deep_ocean()
    print("✨ Hoàn tất tạo 12 ảnh khung nền đẹp!")
