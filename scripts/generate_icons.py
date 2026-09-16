import os
import math
from PIL import Image, ImageDraw

def create_base_canvas(size=512, c1=(124, 58, 237), c2=(67, 56, 202), radius=110):
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    margin = 20

    # Squircle background with diagonal gradient
    bg = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    for y in range(margin, size - margin):
        for x in range(margin, size - margin):
            ratio = (x + y - 2 * margin) / float(2 * (size - 2 * margin))
            r = int(c1[0] * (1 - ratio) + c2[0] * ratio)
            g = int(c1[1] * (1 - ratio) + c2[1] * ratio)
            b = int(c1[2] * (1 - ratio) + c2[2] * ratio)
            bg.putpixel((x, y), (r, g, b, 255))
    
    bg_mask = Image.new('L', (size, size), 0)
    b_draw = ImageDraw.Draw(bg_mask)
    b_draw.rounded_rectangle([margin, margin, size - margin, size - margin], radius=radius, fill=255)
    
    squircle = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    squircle.paste(bg, (0, 0), bg_mask)
    
    # Border highlight
    b_draw_border = ImageDraw.Draw(squircle)
    b_draw_border.rounded_rectangle([margin, margin, size - margin, size - margin], radius=radius, outline=(255, 255, 255, 60), width=4)
    
    return Image.alpha_composite(img, squircle)


def generate_video_maker_icon(output_ico, output_png):
    # Tool Làm Video (AutoDub / Studio / Edit)
    base = create_base_canvas(512, c1=(147, 51, 234), c2=(79, 70, 229), radius=110)
    draw = ImageDraw.Draw(base)

    # Clapperboard body
    body_box = [110, 210, 402, 390]
    draw.rounded_rectangle(body_box, radius=24, fill=(255, 255, 255, 240))

    # Inner dark screen for video preview
    screen_box = [126, 226, 386, 374]
    draw.rounded_rectangle(screen_box, radius=16, fill=(30, 27, 75, 255))

    # Play button in center
    cx, cy = 240, 300
    play_tri = [(cx - 22, cy - 35), (cx - 22, cy + 35), (cx + 38, cy)]
    draw.polygon(play_tri, fill=(244, 114, 182, 255)) # vibrant pink/rose play triangle

    # AI Audio soundwave beside play button
    waves = [
        (cx + 65, cy, 28),
        (cx + 80, cy, 46),
        (cx + 95, cy, 22),
    ]
    for wx, wy, wh in waves:
        draw.rounded_rectangle([wx - 4, wy - wh // 2, wx + 4, wy + wh // 2], radius=4, fill=(167, 139, 250, 255))

    # Clapper top bar (slate stick)
    top_box = [105, 125, 407, 195]
    stick = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(stick)
    s_draw.rounded_rectangle(top_box, radius=18, fill=(24, 24, 27, 255))

    mask = Image.new('L', (512, 512), 0)
    m_draw = ImageDraw.Draw(mask)
    m_draw.rounded_rectangle(top_box, radius=18, fill=255)

    slashes = Image.new('RGBA', (512, 512), (0, 0, 0, 0))
    sl_draw = ImageDraw.Draw(slashes)
    for sx in range(90, 420, 60):
        points = [(sx, 195), (sx + 30, 125), (sx + 55, 125), (sx + 25, 195)]
        sl_draw.polygon(points, fill=(255, 255, 255, 255))

    stick = Image.alpha_composite(stick, slashes)
    stick.putalpha(mask)
    base = Image.alpha_composite(base, stick)

    draw = ImageDraw.Draw(base)

    # Little AI star / sparkle in top right
    star_x, star_y = 395, 115
    for dx, dy, r in [(0, 0, 18), (-22, 18, 10)]:
        x = star_x + dx
        y = star_y + dy
        star_poly = [
            (x, y - r), (x + r // 3, y - r // 3),
            (x + r, y), (x + r // 3, y + r // 3),
            (x, y + r), (x - r // 3, y + r // 3),
            (x - r, y), (x - r // 3, y - r // 3)
        ]
        draw.polygon(star_poly, fill=(253, 224, 71, 255)) # golden yellow star

    base.save(output_png, 'PNG')
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(output_ico, format='ICO', sizes=sizes)
    print(f"Generated {output_ico}")


def generate_video_research_icon(output_ico, output_png):
    # Tool Tìm Kiếm & Tải Video AI (Cyan / Blue / Ocean)
    base = create_base_canvas(512, c1=(6, 182, 212), c2=(37, 99, 235), radius=110)
    draw = ImageDraw.Draw(base)

    # Video Player Card
    card_box = [105, 120, 407, 340]
    draw.rounded_rectangle(card_box, radius=24, fill=(255, 255, 255, 240))

    inner_box = [120, 135, 392, 325]
    draw.rounded_rectangle(inner_box, radius=18, fill=(15, 23, 42, 255))

    # Play symbol in screen
    cx, cy = 230, 230
    play_tri = [(cx - 25, cy - 35), (cx - 25, cy + 35), (cx + 35, cy)]
    draw.polygon(play_tri, fill=(56, 189, 248, 255))

    # Magnifying glass (Search) in lower right
    mx, my = 330, 310
    glass_r = 75
    # Handle
    hx1 = mx + int(glass_r * 0.65)
    hy1 = my + int(glass_r * 0.65)
    hx2 = hx1 + 65
    hy2 = hy1 + 65
    draw.line([(hx1, hy1), (hx2, hy2)], fill=(251, 146, 60, 255), width=24)
    draw.ellipse([hx2 - 12, hy2 - 12, hx2 + 12, hy2 + 12], fill=(251, 146, 60, 255))

    # Glass rim
    draw.ellipse([mx - glass_r, my - glass_r, mx + glass_r, my + glass_r], fill=(255, 255, 255, 250), outline=(234, 88, 12, 255), width=10)
    # Glass interior
    draw.ellipse([mx - glass_r + 10, my - glass_r + 10, mx + glass_r - 10, my + glass_r - 10], fill=(254, 243, 199, 230))

    # Download arrow inside search glass
    draw.polygon([(mx, my + 24), (mx - 22, my - 2), (mx + 22, my - 2)], fill=(217, 70, 239, 255))
    draw.rectangle([mx - 8, my - 28, mx + 8, my], fill=(217, 70, 239, 255))

    base.save(output_png, 'PNG')
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(output_ico, format='ICO', sizes=sizes)
    print(f"Generated {output_ico}")


def generate_batch_render_icon(output_ico, output_png):
    # Chạy Tự Động Video Phôi (Orange / Crimson / Gold)
    base = create_base_canvas(512, c1=(249, 115, 22), c2=(225, 29, 72), radius=110)
    draw = ImageDraw.Draw(base)

    # Film Reel & Lightning Bolt / Fast Forward
    cx, cy = 256, 256
    r = 135
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 240), outline=(254, 240, 138, 255), width=8)
    draw.ellipse([cx - r + 16, cy - r + 16, cx + r - 16, cy + r - 16], fill=(24, 24, 27, 255))

    # Center hub
    hr = 45
    draw.ellipse([cx - hr, cy - hr, cx + hr, cy + hr], fill=(255, 255, 255, 240))
    draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(24, 24, 27, 255))

    # Film holes
    for angle in range(0, 360, 60):
        rad = math.radians(angle)
        hx = cx + int(82 * math.cos(rad))
        hy = cy + int(82 * math.sin(rad))
        draw.ellipse([hx - 22, hy - 22, hx + 22, hy + 22], fill=(255, 255, 255, 240))

    # Lightning bolt over center
    bolt = [
        (cx + 8, cy - 110),
        (cx - 35, cy + 5),
        (cx - 2, cy + 5),
        (cx - 15, cy + 105),
        (cx + 45, cy - 15),
        (cx + 8, cy - 15)
    ]
    draw.polygon(bolt, fill=(250, 204, 21, 255))
    draw.polygon(bolt, outline=(255, 255, 255, 220))

    base.save(output_png, 'PNG')
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(output_ico, format='ICO', sizes=sizes)
    print(f"Generated {output_ico}")


if __name__ == '__main__':
    out_dir = r"C:\Users\admin\Projects\ai-video-research-tool\assets\icons"
    os.makedirs(out_dir, exist_ok=True)
    
    tool_v1_dir = r"C:\tool v1\assets"
    os.makedirs(tool_v1_dir, exist_ok=True)

    generate_video_maker_icon(
        os.path.join(out_dir, "tool_lam_video.ico"),
        os.path.join(out_dir, "tool_lam_video.png")
    )
    generate_video_maker_icon(
        os.path.join(tool_v1_dir, "tool_lam_video.ico"),
        os.path.join(tool_v1_dir, "tool_lam_video.png")
    )

    generate_video_research_icon(
        os.path.join(out_dir, "tool_nghien_cuu_video.ico"),
        os.path.join(out_dir, "tool_nghien_cuu_video.png")
    )

    generate_batch_render_icon(
        os.path.join(out_dir, "chay_video_phoi.ico"),
        os.path.join(out_dir, "chay_video_phoi.png")
    )
    generate_batch_render_icon(
        os.path.join(tool_v1_dir, "chay_video_phoi.ico"),
        os.path.join(tool_v1_dir, "chay_video_phoi.png")
    )
