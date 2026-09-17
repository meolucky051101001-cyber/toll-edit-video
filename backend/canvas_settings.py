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
SETTINGS_FILE = WORKSPACE / "canvas_settings.json"
MOTION_BG_DIR = WORKSPACE / "motion_backgrounds"

DEFAULT_CANVAS_SETTINGS: Dict[str, Any] = {
    "anti_reup_enabled": False,  # Mặc định luôn TẮT chế độ chống re-up (chỉ bật khi người dùng chủ động gạt sang BẬT)
    "aspect_ratio": "9:16",      # "original", "9:16", "16:9", "1:1"
    "bg_type": "video_motion",   # "blur", "color", "gradient", "image", "video_motion", "none"
    "bg_color": "#0a0e17",       # Mã hex màu nền
    "bg_image": "",              # Đường dẫn ảnh nền tùy chọn
    "bg_motion_file": "song_bien.mp4", # Tên video nền chuyển động ("song_bien.mp4", "bien_xanh.mp4", "rung_cay.mp4")
    "video_scale": 0.88,         # 0.70 đến 1.0 (thu nhỏ để lộ nền chống quét)
    "border_radius": 16,         # Bo góc video chính (px)
    "drop_shadow": True,         # Đổ bóng viền video
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

            bg_type = str(merged.get("bg_type", "blur")).lower()
            if bg_type not in ("blur", "color", "gradient", "image", "video_motion", "none"):
                bg_type = "blur"
            merged["bg_type"] = bg_type

            scale = float(merged.get("video_scale", 0.88))
            merged["video_scale"] = round(max(0.60, min(1.0, scale)), 2)

            merged["border_radius"] = max(0, min(60, int(merged.get("border_radius", 16))))
            merged["drop_shadow"] = bool(merged.get("drop_shadow", True))
            merged["mirror"] = bool(merged.get("mirror", False))
            merged["blur_sigma"] = max(5, min(60, int(merged.get("blur_sigma", 25))))
            merged["darken_bg"] = round(max(0.0, min(0.8, float(merged.get("darken_bg", 0.35)))), 2)
            merged["bg_motion_file"] = str(merged.get("bg_motion_file", "song_bien.mp4"))
            merged["anti_reup_enabled"] = bool(merged.get("anti_reup_enabled", False))
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

        current["anti_reup_enabled"] = bool(current.get("anti_reup_enabled", False))

        ratio = str(current.get("aspect_ratio", "original")).lower()
        if ratio not in ("original", "9:16", "16:9", "1:1"):
            ratio = "original"
        current["aspect_ratio"] = ratio

        bg_type = str(current.get("bg_type", "blur")).lower()
        if bg_type not in ("blur", "color", "gradient", "image", "video_motion", "none"):
            bg_type = "blur"
        current["bg_type"] = bg_type

        scale = float(current.get("video_scale", 0.88))
        current["video_scale"] = round(max(0.60, min(1.0, scale)), 2)
        current["border_radius"] = max(0, min(60, int(current.get("border_radius", 16))))
        current["drop_shadow"] = bool(current.get("drop_shadow", True))
        current["mirror"] = bool(current.get("mirror", False))
        current["blur_sigma"] = max(5, min(60, int(current.get("blur_sigma", 25))))
        current["darken_bg"] = round(max(0.0, min(0.8, float(current.get("darken_bg", 0.35)))), 2)
        current["bg_motion_file"] = str(current.get("bg_motion_file", "song_bien.mp4"))

        WORKSPACE.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info(f"Đã lưu cấu hình canvas_settings: ratio={ratio}, bg={bg_type}, scale={scale}, motion={current['bg_motion_file']}")
        return {"status": "ok", "settings": current, "message": "Đã lưu cài đặt tỷ lệ và background thành công"}
    except Exception as e:
        logger.error(f"Lỗi khi lưu canvas_settings: {e}")
        return {"status": "error", "message": str(e)}
