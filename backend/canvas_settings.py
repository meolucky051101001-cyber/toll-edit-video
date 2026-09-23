"""
Quản lý cấu hình tỷ lệ khung hình và background chống re-up cho video.
Được áp dụng khi render hàng loạt để né các thuật toán phát hiện bản quyền trên TikTok, Facebook, YouTube.
"""
import json
import os
import logging
from pathlib import Path
from typing import Dict, Any, List

logger = logging.getLogger("canvas_settings")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
def _resolve_settings_file() -> Path:
    bot_system = WORKSPACE / "bot_system"
    target = bot_system / "canvas_settings.json"
    if target.exists() or bot_system.is_dir():
        return target
    return WORKSPACE / "canvas_settings.json"

SETTINGS_FILE = _resolve_settings_file()
MOTION_BG_DIR = WORKSPACE / "motion_backgrounds"
IMAGE_BG_DIR = WORKSPACE / "background_images"

DEFAULT_CANVAS_SETTINGS: Dict[str, Any] = {
    "anti_reup_enabled": True,   # Bật chế độ chống re-up an toàn theo yêu cầu
    "aspect_ratio": "9:16",      # "original", "9:16", "16:9", "1:1"
    "bg_type": "image",          # Mặc định dùng ảnh nền vàng (người dùng đang dùng nền vàng)
    "bg_color": "#f59e0b",       # Mã hex màu nền vàng rực rỡ
    "bg_image": "tia_sang_vang_ngoi_sao.jpg", # Tệp ảnh nền vàng
    "bg_image_file": "tia_sang_vang_ngoi_sao.jpg", # Tên tệp ảnh nền vàng tia sáng ngôi sao
    "auto_crop_black_bars": True, # Tự động phát hiện và cắt bỏ 2 viền đen trên/dưới của video phôi
    "crop_padding": 0,           # Bù lề an toàn viền đen (-30px đến +30px, 0 là chuẩn xác, <0 mở rộng video, >0 gọt nhẹ)
    "show_safe_guides": True,    # Hiển thị thước đo an toàn viền đen trên Canvas
    "bg_motion_file": "song_bien.mp4", # Tên video nền chuyển động sóng biển
    "motion_bg_enabled": False,  # Bật/tắt background sóng biển tùy ý (Mặc định: TẮT để dùng nền vàng của người dùng)
    "protect_top_thumb": True,   # Tự động phát hiện và bảo vệ thẻ chữ thumb trắng ở phía trên
    "video_scale": 1.0,          # 1.0 = KHỚP 100% CHIỀU NGANG, SÁT 2 BÊN MÉP (KHÔNG THU NHỎ THƯỚC PHIM)
    "border_radius": 0,          # Bo góc video chính: 0px để sát mép vuông vắn, không bo góc cắt xén video
    "drop_shadow": False,        # Đổ bóng viền video (tắt khi 100% full width để sát mép)
    "mirror": False,             # Lật ngang video chống nhận diện AI
    "blur_sigma": 25,            # Độ mờ nền (10 - 40)
    "darken_bg": 0.35,           # Độ tối nền (0.0 - 0.7)
    # Tùy chỉnh Font chữ, Màu chữ & Khung nền phụ đề
    "sub_font_name": "Arial",    # Font chữ (Arial mặc định, Roboto, Montserrat, Be Vietnam Pro,...)
    "sub_font_color": "#000000", # Màu chữ phụ đề (Mặc định: Đen #000000)
    "sub_font_size": 18,         # Cỡ chữ (px) (12 - 36)
    "sub_font_bold": True,       # Chữ in đậm
    "sub_bg_color": "#ffffff",   # Màu khung nền (Mặc định: Trắng #ffffff)
    "sub_bg_opacity": 1.0,       # Độ mờ đục khung nền (0.1 - 1.0)
    "sub_box_padding_x": 16,     # Độ rộng lề ngang khung nền (px)
    "sub_box_padding_y": 8,      # Độ cao lề dọc khung nền (px)
    "sub_box_radius": 8,         # Độ bo góc khung nền (px)
    "sub_pos_y_pct": 0.82,       # Vị trí dọc phụ đề (0.10 - 0.95 từ đỉnh xuống)
    "sub_pos_x_pct": 0.50,       # Vị trí ngang phụ đề (0.10 - 0.90 từ trái sang, 0.50 là căn giữa)
}

def get_available_motion_backgrounds() -> List[Dict[str, Any]]:
    """Trả về danh sách các video nền chuyển động có sẵn (sóng biển, cảnh biển, rừng cây,...)."""
    preset_info = {
        "song_bien.mp4": {
            "id": "song_bien",
            "title": "Sóng Biển Vỗ Bờ",
            "desc": "Sóng biển vỗ bờ cát trắng tự nhiên, góc quay drone trên cao",
            "icon": "🌊",
            "category": "sea"
        },
        "bien_xanh.mp4": {
            "id": "bien_xanh",
            "title": "Bờ Biển Nhiệt Đới",
            "desc": "Cảnh biển nhiệt đới Lovina Beach xanh biếc, nước trong vắt",
            "icon": "🏖️",
            "category": "beach"
        },
        "rung_cay.mp4": {
            "id": "rung_cay",
            "title": "Rừng Cây Thiên Nhiên",
            "desc": "Cảnh rừng cây thiên nhiên xanh mát, tán lá chuyển động nhẹ",
            "icon": "🌲",
            "category": "forest"
        },
    }

    dirs_to_check = [
        MOTION_BG_DIR,
        Path("C:/tool v1/workspace/motion_backgrounds"),
        Path("C:/tool v2/workspace/motion_backgrounds")
    ]

    found_files = {}
    for d in dirs_to_check:
        if d.exists():
            for f in d.glob("*.mp4"):
                if f.name not in found_files and f.stat().st_size > 0:
                    found_files[f.name] = f

    results = []
    # Thêm các preset theo thứ tự ưu tiên
    for fname, meta in preset_info.items():
        if fname in found_files:
            fpath = found_files[fname]
            clean_path = str(fpath).replace("\\", "/")
            results.append({
                "id": meta["id"],
                "filename": fname,
                "title": meta["title"],
                "desc": meta["desc"],
                "icon": meta["icon"],
                "category": meta["category"],
                "path": clean_path,
                "url": f"/api/workflow/stream-file?path={clean_path}"
            })

    # Thêm các video mp4 khác do người dùng thêm vào thư mục motion_backgrounds
    for fname, fpath in found_files.items():
        if fname not in preset_info:
            stem = Path(fname).stem
            clean_path = str(fpath).replace("\\", "/")
            results.append({
                "id": stem,
                "filename": fname,
                "title": stem.replace("_", " ").title(),
                "desc": "Video nền chuyển động tùy chọn",
                "icon": "🎬",
                "category": "custom",
                "path": clean_path,
                "url": f"/api/workflow/stream-file?path={clean_path}"
            })

    return results

def resolve_motion_bg_path(filename_or_path: str) -> str:
    """Xác định đường dẫn tệp thực tế của video background chuyển động."""
    if not filename_or_path:
        filename_or_path = "song_bien.mp4"
    p = Path(filename_or_path)
    if p.is_file() and p.exists():
        return str(p).replace("\\", "/")
    # Kiểm tra trong các thư mục motion_backgrounds
    for d in [MOTION_BG_DIR, Path("C:/tool v1/workspace/motion_backgrounds"), Path("C:/tool v2/workspace/motion_backgrounds")]:
        candidate = d / p.name
        if candidate.is_file() and candidate.exists():
            return str(candidate).replace("\\", "/")
    return ""

def get_available_image_backgrounds() -> List[Dict[str, Any]]:
    """Trả về danh sách các hình ảnh nền tĩnh có sẵn (Tia sáng vàng ngôi sao, xanh neon, đỏ cam, tím galaxy,...)."""
    preset_info = {
        "tia_sang_vang_ngoi_sao.jpg": {
            "id": "tia_sang_vang_ngoi_sao",
            "title": "⭐ Tia Sáng Vàng Ngôi Sao (Khuyên Dùng)",
            "desc": "Nền tia sáng vàng rực rỡ kèm ngôi sao lấp lánh, che viền đen trên dưới hoàn hảo",
            "icon": "⭐",
            "category": "gold",
            "color": "#f59e0b"
        },
        "tia_sang_xanh_neon.jpg": {
            "id": "tia_sang_xanh_neon",
            "title": "🌌 Tia Sáng Xanh Neon Cyberpunk",
            "desc": "Xanh cyan công nghệ hiện đại, tia sáng và hạt ánh sáng neon nổi bật",
            "icon": "🌌",
            "category": "cyan",
            "color": "#06b6d4"
        },
        "hoang_hon_do_cam.jpg": {
            "id": "hoang_hon_do_cam",
            "title": "🌅 Hoàng Hôn Đỏ Cam Rực Lửa",
            "desc": "Đỏ cam kịch tính, ấm áp, cuốn hút người xem",
            "icon": "🌅",
            "category": "sunset",
            "color": "#ea580c"
        },
        "tim_khoi_galaxy.jpg": {
            "id": "tim_khoi_galaxy",
            "title": "🔮 Tím Khói Galaxy Huyền Ảo",
            "desc": "Tím vũ trụ huyền bí kết hợp ánh sao, phong cách sang trọng ma mị",
            "icon": "🔮",
            "category": "purple",
            "color": "#a855f7"
        },
        "xanh_ngoc_emerald.jpg": {
            "id": "xanh_ngoc_emerald",
            "title": "💎 Xanh Ngọc Lục Bảo Quý Phái",
            "desc": "Tông xanh ngọc tươi mát, thanh lịch, chuẩn phong cách sang trọng",
            "icon": "💎",
            "category": "emerald",
            "color": "#10b981"
        },
        "hong_pastel_mong_mo.jpg": {
            "id": "hong_pastel_mong_mo",
            "title": "🌸 Hồng & Tím Pastel Mộng Mơ",
            "desc": "Phong cách ngọt ngào, dịu mắt, phù hợp video thời trang, đời sống",
            "icon": "🌸",
            "category": "pastel",
            "color": "#ec4899"
        },
        "xam_carbon_titan.jpg": {
            "id": "xam_carbon_titan",
            "title": "🛡️ Xám Bạc Titan Tối Giản",
            "desc": "Tông xám sạch sẽ, hiện đại, làm nổi bật tối đa nội dung thước phim",
            "icon": "🛡️",
            "category": "dark",
            "color": "#94a3b8"
        },
        "vang_dong_metallic.jpg": {
            "id": "vang_dong_metallic",
            "title": "👑 Vàng Đồng Hoàng Gia Metallic",
            "desc": "Ánh kim đồng sang chảnh, cổ điển, phù hợp video review, lịch sử",
            "icon": "👑",
            "category": "metallic",
            "color": "#eab308"
        }
    }

    dirs_to_check = [
        IMAGE_BG_DIR,
        Path("C:/tool v1/workspace/background_images"),
        Path("C:/tool v2/workspace/background_images")
    ]

    valid_exts = {".jpg", ".jpeg", ".png", ".webp"}
    found_files = {}
    for d in dirs_to_check:
        if d.exists():
            for f in d.glob("*"):
                if f.is_file() and f.suffix.lower() in valid_exts and f.name not in found_files and f.stat().st_size > 0:
                    found_files[f.name] = f

    results = []
    # Thêm các preset theo thứ tự ưu tiên
    for fname, meta in preset_info.items():
        if fname in found_files:
            fpath = found_files[fname]
            clean_path = str(fpath).replace("\\", "/")
            results.append({
                "id": meta["id"],
                "filename": fname,
                "title": meta["title"],
                "desc": meta["desc"],
                "icon": meta["icon"],
                "category": meta["category"],
                "path": clean_path,
                "url": f"/api/workflow/stream-file?path={clean_path}"
            })

    # Thêm các ảnh khác do người dùng tải vào thư mục background_images
    for fname, fpath in found_files.items():
        if fname not in preset_info:
            stem = Path(fname).stem
            clean_path = str(fpath).replace("\\", "/")
            results.append({
                "id": stem,
                "filename": fname,
                "title": stem.replace("_", " ").title(),
                "desc": "Ảnh nền tùy chọn",
                "icon": "🖼️",
                "category": "custom",
                "path": clean_path,
                "url": f"/api/workflow/stream-file?path={clean_path}"
            })

    return results

def resolve_image_bg_path(filename_or_path: str) -> str:
    """Xác định đường dẫn tệp thực tế của ảnh background."""
    if not filename_or_path:
        filename_or_path = "tia_sang_vang_ngoi_sao.jpg"
    p = Path(filename_or_path)
    if p.is_file() and p.exists():
        return str(p).replace("\\", "/")
    for d in [IMAGE_BG_DIR, Path("C:/tool v1/workspace/background_images"), Path("C:/tool v2/workspace/background_images")]:
        candidate = d / p.name
        if candidate.is_file() and candidate.exists():
            return str(candidate).replace("\\", "/")
    return ""

def get_canvas_settings() -> Dict[str, Any]:
    """Đọc cấu hình tỷ lệ và background từ tệp JSON."""
    try:
        if SETTINGS_FILE.exists():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            merged = dict(DEFAULT_CANVAS_SETTINGS)
            merged.update(data)
            ratio = str(merged.get("aspect_ratio", "original")).lower()
            if ratio not in ("original", "9:16", "16:9", "1:1"):
                ratio = "original"
            merged["aspect_ratio"] = ratio

            bg_type = str(merged.get("bg_type", "image")).lower()
            if bg_type not in ("blur", "color", "gradient", "image", "video_motion", "none"):
                bg_type = "image"
            merged["bg_type"] = bg_type

            scale = float(merged.get("video_scale", 1.0))
            merged["video_scale"] = round(max(0.60, min(1.0, scale)), 2)

            merged["border_radius"] = max(0, min(60, int(merged.get("border_radius", 0))))
            merged["drop_shadow"] = bool(merged.get("drop_shadow", False))
            merged["mirror"] = bool(merged.get("mirror", False))
            merged["blur_sigma"] = max(5, min(60, int(merged.get("blur_sigma", 25))))
            merged["darken_bg"] = round(max(0.0, min(0.8, float(merged.get("darken_bg", 0.35)))), 2)
            merged["bg_motion_file"] = str(merged.get("bg_motion_file", "song_bien.mp4"))
            merged["motion_bg_enabled"] = bool(merged.get("motion_bg_enabled", False))
            merged["bg_image_file"] = str(merged.get("bg_image_file", "tia_sang_vang_ngoi_sao.jpg"))
            merged["bg_color"] = str(merged.get("bg_color", "#f59e0b"))
            merged["auto_crop_black_bars"] = bool(merged.get("auto_crop_black_bars", True))
            merged["crop_padding"] = max(-30, min(30, int(merged.get("crop_padding", 0))))
            merged["show_safe_guides"] = bool(merged.get("show_safe_guides", True))
            merged["anti_reup_enabled"] = bool(merged.get("anti_reup_enabled", True))
            return merged
    except Exception as e:
        logger.warning(f"Lỗi khi đọc canvas_settings.json: {e}")
    return dict(DEFAULT_CANVAS_SETTINGS)

def save_canvas_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """Lưu cấu hình tỷ lệ và background mới cho xử lý hàng loạt."""
    try:
        current = get_canvas_settings()
        for k, v in new_settings.items():
            if k in DEFAULT_CANVAS_SETTINGS:
                current[k] = v

        current["anti_reup_enabled"] = bool(current.get("anti_reup_enabled", True))

        ratio = str(current.get("aspect_ratio", "9:16")).lower()
        if ratio not in ("original", "9:16", "16:9", "1:1"):
            ratio = "9:16"
        current["aspect_ratio"] = ratio

        bg_type = str(current.get("bg_type", "image")).lower()
        if bg_type not in ("blur", "color", "gradient", "image", "video_motion", "none"):
            bg_type = "image"
        current["bg_type"] = bg_type

        scale = float(current.get("video_scale", 1.0))
        current["video_scale"] = round(max(0.60, min(1.0, scale)), 2)
        current["border_radius"] = max(0, min(60, int(current.get("border_radius", 0))))
        current["drop_shadow"] = bool(current.get("drop_shadow", False))
        current["mirror"] = bool(current.get("mirror", False))
        current["blur_sigma"] = max(5, min(60, int(current.get("blur_sigma", 25))))
        current["darken_bg"] = round(max(0.0, min(0.8, float(current.get("darken_bg", 0.35)))), 2)
        current["bg_motion_file"] = str(current.get("bg_motion_file", "song_bien.mp4"))
        current["motion_bg_enabled"] = bool(current.get("motion_bg_enabled", False))
        current["bg_image_file"] = str(current.get("bg_image_file", "tia_sang_vang_ngoi_sao.jpg"))
        current["bg_color"] = str(current.get("bg_color", "#f59e0b"))
        current["auto_crop_black_bars"] = bool(current.get("auto_crop_black_bars", True))
        current["crop_padding"] = max(-30, min(30, int(current.get("crop_padding", 0))))
        current["protect_top_thumb"] = bool(current.get("protect_top_thumb", True))
        current["show_safe_guides"] = bool(current.get("show_safe_guides", True))

        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Đã lưu cấu hình canvas_settings: ratio={ratio}, bg={bg_type}, scale={scale}, image={current['bg_image_file']}, motion={current['bg_motion_file']}, padding={current['crop_padding']}")
        return {"status": "ok", "settings": current, "message": "Đã lưu cài đặt tỷ lệ và background thành công"}
    except Exception as e:
        logger.error(f"Lỗi khi lưu canvas_settings: {e}")
        return {"status": "error", "message": str(e)}
