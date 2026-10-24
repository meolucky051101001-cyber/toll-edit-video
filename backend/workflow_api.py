"""
Workflow API Module for AutoDub.
Cung cấp API phục vụ trang Studio Quy Trình, xem trước video hoàn tất vs video gốc,
lồng background chống re-up, và chèn khung chống re-up hàng loạt theo folder.
"""
import os
import json
import logging
import mimetypes
import re
import time
import threading
import subprocess
import hashlib
import html
from pathlib import Path
from typing import Optional, Dict, Any, List
import cv2
import numpy as np
import asyncio
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Query, Body, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from dashboard_media import media_status

from canvas_settings import (
    get_canvas_settings,
    save_canvas_settings,
    get_available_motion_backgrounds,
    resolve_motion_bg_path,
    get_available_image_backgrounds,
    resolve_image_bg_path
)

logger = logging.getLogger("workflow_api")

try:
    from backend.config.paths import AppPaths
    from backend.environment import read_environment
except ImportError:
    from config.paths import AppPaths
    from environment import read_environment

_BACKEND_ROOT = Path(__file__).resolve().parent
PATHS = AppPaths.from_environment(
    _BACKEND_ROOT.parent, read_environment(_BACKEND_ROOT)
)

router = APIRouter()

def detect_letterbox_crop(video_path: str, padding: int = 0) -> Optional[Dict[str, int]]:
    """
    Tự động phát hiện viền đen trên và dưới của video (letterboxing).
    Bảo toàn nội dung tuyệt đối: Quét đa khung hình (10%, 25%, 50%, 75%, 90%),
    lấy ranh giới an toàn nhất (min(top), max(bottom)).
    Áp dụng bù lề an toàn padding:
      - padding < 0 (ví dụ -6px): Nới rộng vùng video ra ngoài, đảm bảo 0% che vào nội dung.
      - padding > 0 (ví dụ +4px): Gọt nhẹ vào trong để loại bỏ vệt xám mờ cạnh viền đen cũ.
    Trả về dict {'w': cw, 'h': ch, 'x': cx, 'y': cy, 'top': top, 'bottom': bottom, 'orig_w': w, 'orig_h': h}
    """
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0 or total_frames <= 0:
            cap.release()
            return None
        
        sample_indices = [int(total_frames * p) for p in [0.10, 0.25, 0.50, 0.75, 0.90]]
        crops = []
        for idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            row_max = np.max(gray, axis=1)
            top = 0
            while top < (h // 2 - 60) and row_max[top] < 22:
                top += 1
            bottom = h
            while bottom > (h // 2 + 60) and row_max[bottom - 1] < 22:
                bottom -= 1
            crops.append((top, bottom, 0, w))
        cap.release()
        if not crops:
            return None
        # Conservative: Lấy mép cao nhất của top, thấp nhất của bottom để bảo toàn toàn bộ nội dung
        top = min(c[0] for c in crops)
        bottom = max(c[1] for c in crops)

        # Chỉ áp dụng nếu có viền đen trên hoặc dưới đáng kể (>= 16px)
        if top < 16 and (h - bottom) < 16:
            return None

        # Áp dụng bù lề an toàn padding do người dùng kiểm soát
        if padding != 0:
            top = max(0, min(h // 2 - 40, top + padding))
            bottom = min(h, max(h // 2 + 40, bottom - padding))

        # Luôn giữ 100% chiều ngang (2 bên sát mép, không thu nhỏ hay cắt ngang)
        cw = (w // 2) * 2
        ch = ((bottom - top) // 2) * 2
        cx = 0
        cy = (top // 2) * 2
        if cw < 50 or ch < 50:
            return None
        return {
            'w': cw, 'h': ch, 'x': cx, 'y': cy,
            'top': top, 'bottom': bottom,
            'orig_w': w, 'orig_h': h
        }
    except Exception as e:
        logger.warning(f"Lỗi khi detect letterbox crop cho {video_path}: {e}")
        return None

def probe_stream_durations(file_path: str) -> Dict[str, Optional[float]]:
    """
    Lấy thời lượng chính xác của luồng video và audio để tránh lỗi đơ hình
    khi video phôi bị cắt sớm hơn âm thanh (Asymmetric Stream Truncation).
    """
    durations = {"video": None, "audio": None, "format": None}
    try:
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(file_path)
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            data = json.loads(p.stdout)
            fmt_dur = data.get("format", {}).get("duration")
            if fmt_dur:
                durations["format"] = float(fmt_dur)
            for s in data.get("streams", []):
                ctype = s.get("codec_type")
                sdur = s.get("duration")
                if ctype in ("video", "audio") and sdur:
                    try:
                        durations[ctype] = float(sdur)
                    except ValueError:
                        pass
    except Exception as e:
        logger.warning(f"Lỗi khi probe stream durations cho {file_path}: {e}")
    return durations

def generate_smart_thumb_mask(video_path: str, tw: int = 1080, th: int = 1920, padding: int = 0) -> Optional[Dict[str, Any]]:
    """
    Phương án 1: Tự động phát hiện và bảo vệ thẻ chữ thumb trắng ở phía trên.
    Tạo ra một Alpha Mask (tw x th) chuẩn 8-bit grayscale:
      - Vùng viền đen: alpha = 0 (trong suốt để lộ nền vàng / sóng biển).
      - Vùng thẻ chữ thumb: alpha = 255 (giữ nguyên gốc 100% sắc nét, viền nét đen và đổ bóng).
      - Vùng video chính ở giữa: alpha = 255 (giữ nguyên gốc 100%).
      - Khử răng cưa mịn mép bằng GaussianBlur (5, 5).
    Trả về Dict chứa {'mask_path': str, 'thumb_found': bool, 'y_vid_top': int, 'y_vid_bot': int}
    """
    try:
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return None
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if w <= 0 or h <= 0 or total_frames <= 0:
            cap.release()
            return None

        sample_pts = [0.10, 0.25, 0.50, 0.75, 0.90] if total_frames > 5 else [0.0]
        sample_indices = [int(total_frames * p) for p in sample_pts]
        top_list = []
        bot_list = []
        thumb_contours = []

        for idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            row_max = np.max(gray, axis=1)

            thresh = max(22, min(50, int(np.mean(row_max[:5])) + 12))
            mid = h // 2
            yt = mid
            while yt > 0 and row_max[yt] >= thresh:
                yt -= 1
            yb = mid
            while yb < h - 1 and row_max[yb] >= thresh:
                yb += 1

            top_list.append(yt)
            bot_list.append(yb)

            if yt > 20:
                top_part = gray[:yt, :]
                _, bin_top = cv2.threshold(top_part, 160, 255, cv2.THRESH_BINARY)
                cnts, _ = cv2.findContours(bin_top, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for c in cnts:
                    if cv2.contourArea(c) > (w * yt * 0.08):
                        thumb_contours.append(c)

        cap.release()
        if not top_list or not bot_list:
            return None

        # Ranh giới an toàn: min của top để không lẹm vào video, max của bottom
        top = min(top_list)
        bottom = max(bot_list)

        # Kiểm tra xem có viền đen đáng kể hoặc có thumb không
        if top < 16 and (h - bottom) < 16 and not thumb_contours:
            return None

        mask = np.zeros((th, tw), dtype=np.uint8)
        sx = tw / float(w)
        sy = th / float(h)

        scaled_ytop = max(0, min(th, int(top * sy)))
        scaled_ybot = max(0, min(th, int(bottom * sy)))

        # Khóa bảo vệ vùng video chính ở giữa
        mask[scaled_ytop:scaled_ybot, :] = 255

        thumb_found = False
        thumb_box_info = None
        if thumb_contours:
            best_cnt = max(thumb_contours, key=cv2.contourArea)
            scaled_cnt = (best_cnt * [sx, sy]).astype(np.int32)
            hull = cv2.convexHull(scaled_cnt)
            cv2.drawContours(mask[:scaled_ytop, :], [hull], -1, 255, -1)
            thumb_found = True
            bx, by, bw_b, bh_b = cv2.boundingRect(scaled_cnt)
            thumb_box_info = {
                "x": int(bx), "y": int(by), "w": int(bw_b), "h": int(bh_b)
            }

        mask = cv2.GaussianBlur(mask, (5, 5), 0)

        out_dir = WORKSPACE / "temp_masks"
        out_dir.mkdir(parents=True, exist_ok=True)
        h_str = hashlib.md5(f"{video_path}_{top}_{bottom}_{thumb_found}".encode()).hexdigest()[:10]
        out_file = out_dir / f"mask_{h_str}_{tw}x{th}.png"
        cv2.imwrite(str(out_file), mask)

        return {
            "mask_path": str(out_file).replace("\\", "/"),
            "thumb_found": thumb_found,
            "thumb_box": thumb_box_info,
            "y_vid_top": scaled_ytop,
            "y_vid_bot": scaled_ybot,
            "top": top,
            "bottom": bottom
        }
    except Exception as e:
        logger.warning(f"Lỗi khi tạo smart thumb mask cho {video_path}: {e}")
        return None

def build_custom_ass_file(job_dir: Path, cfg: Dict[str, Any], tw: int = 1080, th: int = 1920) -> Optional[Path]:
    """
    Giữ nguyên 100% tọa độ AI OCR tracking che phụ đề tiếng Trung cho từng câu trong final.ass.
    Chỉ cập nhật phông chữ, màu chữ, màu khung nền và độ mờ đục theo tùy chỉnh của người dùng.
    """
    orig_ass = job_dir / "final.ass"
    if not orig_ass.is_file():
        return None

    try:
        ass_text = orig_ass.read_text(encoding="utf-8", errors="replace")
        font_name = str(cfg.get("sub_font_name", "Arial")).strip() or "Arial"
        raw_size = int(cfg.get("sub_font_size", 18))
        font_size = int(raw_size * 2.1) if raw_size <= 24 else raw_size
        bold_val = -1 if cfg.get("sub_font_bold", True) else 0

        font_color_hex = str(cfg.get("sub_font_color", "#000000"))
        bg_color_hex = str(cfg.get("sub_bg_color", "#ffffff"))
        bg_opacity = float(cfg.get("sub_bg_opacity", 1.0))

        def hex_to_ass_color(hex_str: str, default_hex: str = "000000", alpha: float = 1.0) -> str:
            s = str(hex_str).strip().lstrip("#")
            if len(s) == 3:
                s = "".join(c * 2 for c in s)
            if len(s) != 6:
                s = default_hex
            r, g, b = s[0:2], s[2:4], s[4:6]
            a_int = max(0, min(255, int((1.0 - alpha) * 255)))
            return f"&H{a_int:02X}{b}{g}{r}"

        font_color_ass = hex_to_ass_color(font_color_hex, default_hex="000000", alpha=1.0)
        bg_color_ass = hex_to_ass_color(bg_color_hex, default_hex="ffffff", alpha=bg_opacity)

        stroke_width = int(cfg.get("sub_stroke_width", 0))
        stroke_color_hex = str(cfg.get("sub_stroke_color", "#000000"))
        shadow_enabled = bool(cfg.get("sub_shadow", False))
        stroke_color_ass = hex_to_ass_color(stroke_color_hex, default_hex="000000", alpha=1.0)
        shadow_val = 2 if shadow_enabled else 0

        new_bg_style = f"Style: BgStyle,Arial,{font_size},{bg_color_ass},{bg_color_ass},{bg_color_ass},{bg_color_ass},0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1"
        new_txt_style = f"Style: TextStyle,{font_name},{font_size},{font_color_ass},&H000000FF,{stroke_color_ass},&H00000000,{bold_val},0,0,0,100,100,0,0,1,{stroke_width},{shadow_val},5,10,10,10,1"

        ass_text = re.sub(r"Style:\s*BgStyle,[^\r\n]+", new_bg_style, ass_text)
        ass_text = re.sub(r"Style:\s*TextStyle,[^\r\n]+", new_txt_style, ass_text)

        custom_ass = job_dir / "custom_styled.ass"
        custom_ass.write_text(ass_text, encoding="utf-8")
        return custom_ass
    except Exception as e:
        logger.warning(f"Error customizing ASS file: {e}")
        return None


def build_ass_force_style(cfg: Dict[str, Any], default_font_size: int = 18) -> str:
    """Xây dựng chuỗi force_style cho bộ lọc subtitles của FFmpeg từ cấu hình canvas_settings."""
    font_name = str(cfg.get("sub_font_name", "Arial")).strip() or "Arial"
    font_size = int(cfg.get("sub_font_size", default_font_size))
    bold_val = -1 if cfg.get("sub_font_bold", True) else 0

    def hex_to_ass(hex_str: str, default_hex: str = "000000", alpha: float = 1.0) -> str:
        s = str(hex_str).strip().lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        if len(s) != 6:
            s = default_hex
        r, g, b = s[0:2], s[2:4], s[4:6]
        a_int = max(0, min(255, int((1.0 - alpha) * 255)))
        return f"&H{a_int:02X}{b}{g}{r}"

    font_color_hex = str(cfg.get("sub_font_color", "#000000"))
    bg_color_hex = str(cfg.get("sub_bg_color", "#ffffff"))
    bg_opacity = float(cfg.get("sub_bg_opacity", 1.0))
    padding_y = int(cfg.get("sub_box_padding_y", 8))

    stroke_width = int(cfg.get("sub_stroke_width", 0))
    stroke_color_hex = str(cfg.get("sub_stroke_color", "#000000"))
    shadow_enabled = bool(cfg.get("sub_shadow", False))
    outline_color_ass = hex_to_ass(stroke_color_hex, default_hex="000000", alpha=1.0)
    shadow_val = 2 if shadow_enabled else 0

    primary = hex_to_ass(font_color_hex, default_hex="000000", alpha=1.0)
    back = hex_to_ass(bg_color_hex, default_hex="ffffff", alpha=bg_opacity)

    pos_y_pct = float(cfg.get("sub_pos_y_pct", 0.82))
    margin_v = max(15, int(1920 * max(0.02, 1.0 - pos_y_pct))) if default_font_size >= 18 else max(10, int(1080 * max(0.02, 1.0 - pos_y_pct)))

    if bg_opacity <= 0.05:
        # Không có hộp nền: Sử dụng BorderStyle=1 (chữ có outline/stroke & shadow)
        border_style = 1
        outline_val = stroke_width if stroke_width > 0 else 0
        outline_color = outline_color_ass
    else:
        # Có hộp nền: Sử dụng BorderStyle=3 (hộp bọc văn bản)
        border_style = 3
        outline_val = max(1, padding_y // 2)
        outline_color = back

    return (
        f"FontName={font_name},"
        f"FontSize={font_size},"
        f"PrimaryColour={primary},"
        f"OutlineColour={outline_color},"
        f"BackColour={back},"
        f"BorderStyle={border_style},"
        f"Outline={outline_val},"
        f"Shadow={shadow_val},"
        f"Alignment=2,"
        f"MarginV={margin_v},"
        f"Bold={bold_val}"
    )


ROOT = _BACKEND_ROOT
WORKSPACE = PATHS.workspace
INPUT_DIR = PATHS.input_dir
OUTPUT_DIR = PATHS.output_dir
DOWNLOADS_DIR = WORKSPACE / "downloads"

# ===== BATCH FOLDER FRAMING STATE & WORKER =====
batch_framing_state: Dict[str, Any] = {
    "status": "idle",  # "idle", "running", "done", "stopped", "error"
    "current": 0,
    "total": 0,
    "percent": 0.0,
    "current_file": "",
    "input_folder": "",
    "output_folder": "",
    "completed_files": [],
    "error_files": [],
    "message": ""
}
batch_framing_stop_flag = threading.Event()
batch_framing_thread: Optional[threading.Thread] = None


def _valid_final_video(path: Path, defer=False) -> bool:
    return media_status(path, defer=defer)["status"] == "valid"


def _valid_audio(path: Path) -> bool:
    try:
        import soundfile as sf
        info = sf.info(str(path))
        return info.frames > 0 and info.samplerate > 0 and info.channels > 0
    except (OSError, RuntimeError, ValueError):
        return False


def _valid_srt(path: Path) -> bool:
    try:
        segments = parse_srt(path.read_text(encoding="utf-8-sig"))
        return bool(segments) and all(s["end"] > s["start"] >= 0 and s["text"] for s in segments)
    except (OSError, UnicodeError):
        return False


def safe_check_job_artifacts(stem: str, defer_media=False) -> Dict[str, bool]:
    """Kiểm tra sự tồn tại của các artifact một cách an toàn trên Windows filesystem."""
    res = {
        "step1": False,
        "step2": False,
        "step3": False,
        "step3_5": False,
        "step4": False,
        "step5": False,
        "step6": False,
    }
    try:
        job_dir = WORKSPACE / stem
        if job_dir.is_dir():
            res["step1"] = _valid_audio(job_dir / "original.wav")
            res["step2"] = any(_valid_audio(p) for pattern in ("no_vocals.wav", "*_(Instrumental)_*.wav")
                               for p in job_dir.rglob(pattern))
            res["step3"] = _valid_srt(job_dir / "original.srt")
            res["step3_5"] = res["step3"]
            res["step4"] = _valid_srt(job_dir / "translated.srt")
            res["step5"] = _valid_audio(job_dir / "mixed.wav")
            try:
                for item in job_dir.iterdir():
                    if item.name == f"final_{stem}.mp4" and _valid_final_video(item, defer=defer_media):
                        res["step6"] = True
                        break
            except OSError:
                pass
    except OSError:
        pass

    # Kiểm tra thêm trong banve
    try:
        banve_cand = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
        if banve_cand.is_file() and _valid_final_video(banve_cand, defer=defer_media):
            res["step6"] = True
    except OSError:
        pass

    return res


def parse_srt(srt_text: str) -> List[Dict[str, Any]]:
    """Phân tích nội dung tệp phụ đề SRT thành danh sách timestamp và text."""
    subtitles = []
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    
    for block in blocks:
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(lines) < 2:
            continue
            
        time_line_idx = 1 if lines[0].isdigit() else 0
        if time_line_idx >= len(lines):
            continue
            
        time_match = re.search(r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})', lines[time_line_idx])
        if not time_match:
            continue
            
        start_str, end_str = time_match.group(1), time_match.group(2)
        text = " ".join(lines[time_line_idx + 1:])
        
        def srt_to_sec(ts):
            ts = ts.replace(',', '.')
            parts = ts.split(':')
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            
        try:
            start_sec = srt_to_sec(start_str)
            end_sec = srt_to_sec(end_str)
            subtitles.append({
                "index": len(subtitles) + 1,
                "start": start_sec,
                "end": end_sec,
                "start_str": start_str,
                "end_str": end_str,
                "text": text
            })
        except Exception:
            continue
            
    return subtitles


def get_current_control_token() -> str:
    """Lấy token xác thực từ file workspace để bypass xác thực cục bộ."""
    token_path = WORKSPACE / ".dashboard_control_token"
    if token_path.is_file():
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


# ===== ROUTES =====

@router.get("/quy-trinh", response_class=HTMLResponse)
async def serve_workflow_page():
    """Phục vụ trang giao diện Studio Quy trình & Chống Re-up."""
    TEMPLATES_DIR = ROOT / "templates"
    template_path = TEMPLATES_DIR / "workflow.html"
    if template_path.exists():
        content = template_path.read_text(encoding="utf-8")
        token = get_current_control_token()
        content = content.replace("__REPLACE_TOKEN__", token)
        content = content.replace("CONTROL_PLANE_TOKEN_PLACEHOLDER", token)
        curr_paths = AppPaths.from_environment(_BACKEND_ROOT.parent, read_environment(_BACKEND_ROOT))
        downloads_dir = curr_paths.workspace / "downloads"
        default_in = str(downloads_dir) if downloads_dir.is_dir() else str(curr_paths.input_dir)
        configured_paths = {
            "input": default_in,
            "output": str(curr_paths.output_dir),
            "framed_output": str(curr_paths.output_dir / "chong_reup"),
            "downloads": str(downloads_dir),
        }
        content = content.replace(
            "__CONFIGURED_INPUT_DIR__", html.escape(configured_paths["input"], quote=True)
        )
        content = content.replace(
            "__CONFIGURED_OUTPUT_DIR__",
            html.escape(configured_paths["output"], quote=True),
        )
        content = content.replace(
            "__CONFIGURED_FRAMED_OUTPUT_DIR__",
            html.escape(configured_paths["framed_output"], quote=True),
        )
        path_script = (
            "<script>window.AUTO_DUB_PATHS = "
            + json.dumps(configured_paths, ensure_ascii=True)
            + ";</script>"
        )
        content = content.replace("</head>", f"{path_script}</head>", 1)
        return HTMLResponse(content)
    return HTMLResponse("<h2>Chưa tìm thấy file template workflow.html</h2>", status_code=404)


@router.get("/api/canvas-settings")
async def api_get_canvas_settings():
    """Lấy cấu hình tỷ lệ và background hiện tại kèm danh sách motion & image backgrounds."""
    import importlib, canvas_settings
    try:
        importlib.reload(canvas_settings)
    except Exception:
        pass
    cfg = canvas_settings.get_canvas_settings()
    cfg["motion_backgrounds"] = canvas_settings.get_available_motion_backgrounds()
    cfg["image_backgrounds"] = canvas_settings.get_available_image_backgrounds()
    return cfg


@router.post("/api/canvas-settings")
async def api_save_canvas_settings(payload: Dict[str, Any] = Body(...)):
    """Lưu cấu hình tỷ lệ và background mới cho xử lý hàng loạt."""
    import importlib, canvas_settings
    try:
        importlib.reload(canvas_settings)
    except Exception:
        pass
    return canvas_settings.save_canvas_settings(payload)


@router.get("/api/workflow/motion-backgrounds")
async def api_get_motion_backgrounds():
    """Lấy danh sách các video nền chuyển động có sẵn (sóng biển, cảnh biển, rừng cây,...)."""
    import importlib, canvas_settings
    try:
        importlib.reload(canvas_settings)
    except Exception:
        pass
    return {"motion_backgrounds": canvas_settings.get_available_motion_backgrounds()}


@router.get("/api/workflow/image-backgrounds")
async def api_get_image_backgrounds():
    """Lấy danh sách các ảnh nền có sẵn (Tia sáng vàng ngôi sao, xanh neon, đỏ cam,...)."""
    import importlib, canvas_settings
    try:
        importlib.reload(canvas_settings)
    except Exception:
        pass
    return {"image_backgrounds": canvas_settings.get_available_image_backgrounds()}


@router.get("/api/workflow/detect-crop")
async def api_detect_crop(path: str = Query(...), padding: int = Query(0)):
    """Tự động phát hiện viền đen kèm tọa độ vùng video an toàn tuyệt đối và thẻ chữ thumb để trực quan hóa trên Canvas."""
    clean_p = unquote(path).strip().strip('"').strip("'")
    if not clean_p or not os.path.isfile(clean_p):
        return JSONResponse({"status": "error", "message": "Tệp không tồn tại"}, status_code=404)
    crop_info = detect_letterbox_crop(clean_p, padding=padding)
    thumb_info = generate_smart_thumb_mask(clean_p, tw=1080, th=1920, padding=padding)
    return JSONResponse({
        "status": "ok",
        "has_letterbox": crop_info is not None,
        "crop_box": crop_info,
        "thumb_info": thumb_info
    })

FOLDER_PICKER_LOCK = asyncio.Lock()

@router.api_route("/api/workflow/choose-folder", methods=["GET", "POST"])
async def api_choose_workflow_folder(
    request: Request,
    initial_dir: Optional[str] = Query(None),
    title: Optional[str] = Query(None),
    must_exist: bool = Query(True)
):
    """Mở hộp thoại chọn thư mục Windows trực tiếp (Folder Picker Dialog)."""
    initial = initial_dir or ""
    dialog_title = title or "Chọn thư mục"
    need_exist = must_exist

    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                initial = body.get("initial_dir") or body.get("path") or initial
                dialog_title = body.get("title") or dialog_title
                if "must_exist" in body:
                    need_exist = bool(body["must_exist"])
        except Exception:
            pass

    initial = str(initial).strip().strip('"').strip("'")
    if not initial or not Path(initial).is_dir():
        if initial and Path(initial).parent.is_dir():
            initial = str(Path(initial).parent)
        else:
            initial = str(INPUT_DIR if "đầu vào" in dialog_title.lower() or "input" in dialog_title.lower() else OUTPUT_DIR)

    if FOLDER_PICKER_LOCK.locked():
        raise HTTPException(status_code=409, detail="Hộp thoại chọn thư mục đang mở trên màn hình.")

    async with FOLDER_PICKER_LOCK:
        import sys
        script_path = ROOT / "choose_video_folder.py"
        python_exe = sys.executable

        proc = await asyncio.create_subprocess_exec(
            python_exe, str(script_path), initial, dialog_title, str(need_exist).lower(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                raise HTTPException(status_code=500, detail="Không mở được cửa sổ chọn thư mục Windows.")
            res = json.loads(stdout.decode("utf-8", errors="replace"))
            return {"status": "ok", "path": res.get("path", "")}
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise HTTPException(status_code=408, detail="Quá thời gian chọn thư mục.")


@router.get("/api/workflow/videos")
async def api_get_workflow_videos():
    """Liệt kê danh sách video trong phôi, downloads và banve kèm tình trạng 6 bước."""
    items = []
    seen_stems = set()

    # 1. Quét video phôi
    if INPUT_DIR.exists():
        try:
            for p in INPUT_DIR.glob("*.mp4"):
                try:
                    stem = p.stem
                    seen_stems.add(stem)
                    steps = safe_check_job_artifacts(stem, defer_media=True)
                    size_mb = round(p.stat().st_size / (1024 * 1024), 2)
                    mtime = p.stat().st_mtime
                    items.append({
                        "stem": stem,
                        "filename": p.name,
                        "source": "phoi",
                        "size_mb": size_mb,
                        "created_at": mtime,
                        "steps": steps,
                        "thumbnail_url": f"/api/workflow/thumbnail/{stem}"
                    })
                except OSError:
                    continue
        except OSError:
            pass

    # 2. Quét video downloads từ XHS / Telegram
    if DOWNLOADS_DIR.exists():
        try:
            for dl in DOWNLOADS_DIR.glob("*.mp4"):
                try:
                    stem = dl.stem
                    if stem in seen_stems:
                        continue
                    seen_stems.add(stem)
                    steps = safe_check_job_artifacts(stem, defer_media=True)
                    size_mb = round(dl.stat().st_size / (1024 * 1024), 2)
                    mtime = dl.stat().st_mtime
                    items.append({
                        "stem": stem,
                        "filename": dl.name,
                        "source": "downloads",
                        "size_mb": size_mb,
                        "created_at": mtime,
                        "steps": steps,
                        "thumbnail_url": f"/api/workflow/thumbnail/{stem}"
                    })
                except OSError:
                    continue
        except OSError:
            pass

    # Sắp xếp mới nhất lên đầu
    items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"videos": items}


@router.get("/api/workflow/video-data")
async def api_get_workflow_video_data(stem: str = Query(...)):
    """
    Trả về dữ liệu video hoàn tất (lồng tiếng + che sub) vs video gốc,
    cùng phụ đề, âm thanh và thông tin 6 bước chi tiết.
    """
    job_dir = WORKSPACE / stem
    
    # 1. Tìm video phôi/download gốc (raw_video)
    raw_video_url = ""
    cand_phoi = INPUT_DIR / f"{stem}.mp4"
    cand_dl = DOWNLOADS_DIR / f"{stem}.mp4"
    try:
        if cand_phoi.is_file():
            raw_video_url = f"/api/workflow/stream-file?path={cand_phoi}"
        elif cand_dl.is_file():
            raw_video_url = f"/api/workflow/stream-file?path={cand_dl}"
        elif job_dir.is_dir() and (job_dir / "step1_extracted.mp4").is_file():
            raw_video_url = f"/api/workflow/stream-file?path={job_dir / 'step1_extracted.mp4'}"
    except OSError:
        pass

    # 2. Tìm video thành phẩm hoàn tất (final_video - ĐÃ LỒNG TIẾNG & CHE SUB)
    final_video_url = ""
    try:
        if job_dir.is_dir():
            for item in job_dir.iterdir():
                if item.name == f"final_{stem}.mp4" and _valid_final_video(item):
                    final_video_url = f"/api/workflow/stream-file?path={item}"
                    break
        if not final_video_url and OUTPUT_DIR.exists():
            banve_cand = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
            if banve_cand.is_file() and _valid_final_video(banve_cand):
                final_video_url = f"/api/workflow/stream-file?path={banve_cand}"
    except OSError:
        pass

    # Mặc định: Nếu đã có bản hoàn tất, phát bản hoàn tất (đầy đủ âm thanh lồng tiếng & che sub)
    # Nếu chưa hoàn tất, phát bản gốc
    active_video_url = final_video_url if final_video_url else raw_video_url

    # 3. Phân tích phụ đề gốc và phụ đề dịch
    orig_subtitles = []
    trans_subtitles = []
    try:
        if job_dir.is_dir():
            orig_srt_file = job_dir / "original.srt"
            if orig_srt_file.is_file():
                try:
                    orig_subtitles = parse_srt(orig_srt_file.read_text(encoding="utf-8", errors="replace"))
                except Exception as e:
                    logger.warning(f"Error parsing original.srt: {e}")

            trans_srt_file = job_dir / "translated.srt"
            if trans_srt_file.is_file():
                try:
                    trans_subtitles = parse_srt(trans_srt_file.read_text(encoding="utf-8", errors="replace"))
                except Exception as e:
                    logger.warning(f"Error parsing translated.srt: {e}")
    except OSError:
        pass

    combined_subs = []
    if trans_subtitles:
        for i, ts in enumerate(trans_subtitles):
            orig_text = orig_subtitles[i]["text"] if i < len(orig_subtitles) else ""
            combined_subs.append({
                "index": ts["index"],
                "start": ts["start"],
                "end": ts["end"],
                "start_str": ts["start_str"],
                "end_str": ts["end_str"],
                "text": ts["text"],
                "original_text": orig_text
            })
    elif orig_subtitles:
        for os_item in orig_subtitles:
            combined_subs.append({
                "index": os_item["index"],
                "start": os_item["start"],
                "end": os_item["end"],
                "start_str": os_item["start_str"],
                "end_str": os_item["end_str"],
                "text": os_item["text"],
                "original_text": os_item["text"]
            })

    # 4. Stream URLs cho từng bước
    step1_audio = ""
    step5_audio = ""
    try:
        if job_dir.is_dir():
            if (job_dir / "original.wav").is_file():
                step1_audio = f"/api/workflow/stream-file?path={job_dir / 'original.wav'}"
            if (job_dir / "mixed.wav").is_file():
                step5_audio = f"/api/workflow/stream-file?path={job_dir / 'mixed.wav'}"
    except OSError:
        pass

    has_demucs = False
    try:
        if job_dir.is_dir() and (job_dir / "htdemucs").is_dir():
            has_demucs = True
    except OSError:
        pass

    return {
        "stem": stem,
        "thumbnail_url": f"/api/workflow/thumbnail/{stem}",
        "video_url": active_video_url,
        "final_video_url": final_video_url,
        "raw_video_url": raw_video_url,
        "has_final": bool(final_video_url),
        "has_ai_sub": bool(trans_subtitles),
        "has_chinese_sub": bool(orig_subtitles),
        "chinese_subtitles": orig_subtitles,
        "vietnamese_subtitles": trans_subtitles,
        "subtitles": combined_subs,
        "steps": {
            "step1": {"ready": bool(step1_audio), "audio_url": step1_audio, "title": "Trích xuất âm thanh (original.wav)"},
            "step2": {"ready": has_demucs, "title": "Tách âm nền Demucs (htdemucs)"},
            "step3": {"ready": bool(orig_subtitles), "count": len(orig_subtitles), "title": "Nhận diện giọng Faster-Whisper (original.srt)"},
            "step3_5": {"ready": bool(orig_subtitles), "title": "Quét chữ & che phụ đề cũ (OCR Subtitle Band)"},
            "step4": {"ready": bool(trans_subtitles), "count": len(trans_subtitles), "title": "Dịch phụ đề Gemini AI (translated.srt)"},
            "step5": {"ready": bool(step5_audio), "audio_url": step5_audio, "title": "Lồng tiếng AI RVC / TTS (mixed.wav)"},
            "step6": {"ready": bool(final_video_url), "video_url": final_video_url, "title": "Xuất bản video NVENC GPU (final.mp4)"}
        }
    }


def is_safe_stream_path(target_path: Path) -> bool:
    """Kiểm tra đường dẫn file có nằm trong các thư mục được phép phục vụ không (chống Path Traversal)."""
    allowed_roots: List[Path] = [
        WORKSPACE.resolve(),
        INPUT_DIR.resolve(),
        OUTPUT_DIR.resolve(),
        DOWNLOADS_DIR.resolve(),
    ]
    # Bổ sung các thư mục batch framing nếu đang hoạt động
    for folder_key in ("input_folder", "output_folder"):
        fld = batch_framing_state.get(folder_key)
        if fld:
            try:
                allowed_roots.append(Path(fld).resolve())
            except Exception:
                pass

    resolved = target_path.resolve()
    for root in allowed_roots:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return True
        except (ValueError, AttributeError):
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                pass
    return False


@router.get("/api/workflow/stream-file")
async def api_stream_workflow_file(path: str = Query(...)):
    """Stream file media an toàn cho trình phát web."""
    try:
        clean_p = Path(path).resolve()
        if not is_safe_stream_path(clean_p):
            raise HTTPException(status_code=403, detail="Truy cập tệp ngoài phạm vi được phép bị từ chối")

        if not clean_p.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy tệp")

        valid_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".wav", ".mp3", ".aac", ".jpg", ".png", ".webp"}
        if clean_p.suffix.lower() not in valid_exts:
            raise HTTPException(status_code=403, detail="Định dạng tệp không được hỗ trợ")

        media_type = mimetypes.guess_type(clean_p.name)[0] or "application/octet-stream"
        return FileResponse(str(clean_p), media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lỗi stream file: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ===== BATCH FOLDER FRAMING ENDPOINTS =====

@router.post("/api/workflow/batch-framing/scan")
async def api_scan_batch_folder(payload: Dict[str, Any] = Body(...)):
    """Quét toàn bộ video có trong một thư mục bất kỳ do người dùng chỉ định."""
    folder_path = payload.get("folder_path", "").strip()
    if not folder_path:
        raise HTTPException(status_code=400, detail="Vui lòng nhập đường dẫn thư mục")

    p = Path(folder_path)
    if not p.is_dir():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy thư mục: {folder_path}")

    found_videos = []
    valid_exts = {".mp4", ".mov", ".mkv", ".webm"}
    try:
        for f in p.glob("*"):
            if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 1000:
                found_videos.append({
                    "name": f.name,
                    "stem": f.stem,
                    "path": str(f).replace("\\", "/"),
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
                    "modified": f.stat().st_mtime
                })
        found_videos.sort(key=lambda x: x["modified"], reverse=True)
        return {
            "status": "ok",
            "folder_path": str(p).replace("\\", "/"),
            "total": len(found_videos),
            "videos": found_videos
        }
    except Exception as e:
        logger.error(f"Lỗi quét thư mục: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def build_watermark_filter(cfg: Dict[str, Any], tw: int, th: int) -> str:
    """Tạo bộ lọc FFmpeg drawtext cho Watermark bản quyền mờ mờ cố định ở góc bên phải video hoặc vị trí tùy chỉnh."""
    if not cfg.get("watermark_enabled", False):
        return ""
    text = str(cfg.get("watermark_text", "")).strip()
    if not text:
        return ""

    # Thoát ký tự đặc biệt cho ffmpeg drawtext (bao gồm dấu xuống dòng)
    clean_text = (
        text.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace(":", "\\:")
        .replace("%", "\\%")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )
    try:
        opacity = float(cfg.get("watermark_opacity", 0.45))
    except (ValueError, TypeError):
        opacity = 0.45
    opacity = max(0.05, min(1.0, opacity))

    try:
        base_font_size = int(cfg.get("watermark_font_size", 22))
    except (ValueError, TypeError):
        base_font_size = 22
    scale_factor = max(0.6, min(2.5, th / 1920.0))
    font_size = max(12, int(base_font_size * scale_factor))

    # Màu chữ watermark
    raw_color = str(cfg.get("watermark_color", "#ffffff")).strip()
    if raw_color.startswith("#"):
        color_hex = "0x" + raw_color[1:]
    elif raw_color.startswith("0x"):
        color_hex = raw_color
    else:
        color_hex = "white"

    pos = str(cfg.get("watermark_position", "top-right")).lower()
    try:
        base_margin_x = int(cfg.get("watermark_margin_x", 25))
        base_margin_y = int(cfg.get("watermark_margin_y", 25))
    except (ValueError, TypeError):
        base_margin_x = 25
        base_margin_y = 25
    margin_x = max(10, int(base_margin_x * (tw / 1080.0)))
    margin_y = max(10, int(base_margin_y * (th / 1920.0)))

    # Vị trí hiển thị (4 góc hoặc Tự do)
    if pos == "custom":
        try:
            pos_x_pct = float(cfg.get("watermark_pos_x_pct", 0.85))
            pos_y_pct = float(cfg.get("watermark_pos_y_pct", 0.05))
        except (ValueError, TypeError):
            pos_x_pct, pos_y_pct = 0.85, 0.05
        pos_x_pct = max(0.02, min(0.98, pos_x_pct))
        pos_y_pct = max(0.02, min(0.98, pos_y_pct))
        x_expr = f"(w*{pos_x_pct:.4f})-(tw/2)"
        y_expr = f"(h*{pos_y_pct:.4f})-(th/2)"
    elif pos == "bottom-right":
        x_expr = f"w-tw-{margin_x}"
        y_expr = f"h-th-{margin_y}"
    elif pos == "top-left":
        x_expr = f"{margin_x}"
        y_expr = f"{margin_y}"
    elif pos == "bottom-left":
        x_expr = f"{margin_x}"
        y_expr = f"h-th-{margin_y}"
    else:  # Mặc định góc trên phải
        x_expr = f"w-tw-{margin_x}"
        y_expr = f"{margin_y}"

    # Tùy chọn khung huy hiệu mờ bo quanh chữ
    box_filter = ""
    if cfg.get("watermark_bg_enabled", False):
        raw_bg_color = str(cfg.get("watermark_bg_color", "#000000")).strip()
        bg_hex = ("0x" + raw_bg_color[1:]) if raw_bg_color.startswith("#") else (raw_bg_color if raw_bg_color.startswith("0x") else "black")
        try:
            bg_op = float(cfg.get("watermark_bg_opacity", 0.40))
        except (ValueError, TypeError):
            bg_op = 0.40
        bg_op = max(0.05, min(1.0, bg_op))
        box_border_w = max(4, int(8 * scale_factor))
        box_filter = f":box=1:boxcolor={bg_hex}@{bg_op:.2f}:boxborderw={box_border_w}"

    fontfile = r"C\:/Windows/Fonts/arial.ttf"
    shadow_opacity = round(opacity * 0.75, 2)
    filt = (
        f",drawtext=fontfile='{fontfile}':text='{clean_text}':"
        f"fontsize={font_size}:fontcolor={color_hex}@{opacity:.2f}:"
        f"shadowcolor=black@{shadow_opacity:.2f}:shadowx=2:shadowy=2"
        f"{box_filter}:x={x_expr}:y={y_expr}"
    )
    return filt


def _batch_framing_worker(input_dir: Path, output_dir: Path, cfg: Dict[str, Any]):
    """Tiến trình ngầm lồng khung chống re-up hàng loạt cho cả thư mục."""
    global batch_framing_state
    batch_framing_stop_flag.clear()
    
    aspect = str(cfg.get("aspect_ratio", "9:16")).lower()
    bg_type = str(cfg.get("bg_type", "image")).lower()
    motion_bg_enabled = bool(cfg.get("motion_bg_enabled", False))
    if motion_bg_enabled:
        bg_type = "video_motion"
    elif bg_type == "video_motion" and not motion_bg_enabled:
        bg_type = "image"
        
    bg_motion_file = str(cfg.get("bg_motion_file", "song_bien.mp4"))
    bg_image_file = str(cfg.get("bg_image_file", "tia_sang_vang_ngoi_sao.jpg"))
    auto_crop_black_bars = bool(cfg.get("auto_crop_black_bars", True))
    video_scale = float(cfg.get("video_scale", 1.0))
    mirror = bool(cfg.get("mirror", False))
    delogo = bool(cfg.get("delogo", False))

    if aspect == "9:16":
        tw, th = 1080, 1920
    elif aspect == "16:9":
        tw, th = 1920, 1080
    elif aspect == "1:1":
        tw, th = 1080, 1080
    else:
        tw, th = 1080, 1920

    fw = max(100, int(tw * video_scale)) & ~1
    fh = max(100, int(th * video_scale)) & ~1
    flip_str = "hflip," if mirror else ""

    real_motion_path = resolve_motion_bg_path(bg_motion_file) if bg_type == "video_motion" else ""

    # Lấy danh sách video
    valid_exts = {".mp4", ".mov", ".mkv", ".webm"}
    video_files = [f for f in input_dir.glob("*") if f.is_file() and f.suffix.lower() in valid_exts and f.stat().st_size > 1000]
    total = len(video_files)
    
    batch_framing_state.update({
        "status": "running",
        "current": 0,
        "total": total,
        "percent": 0.0,
        "completed_files": [],
        "error_files": [],
        "message": f"Bắt đầu xử lý {total} video..."
    })

    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, v_file in enumerate(video_files, 1):
        if batch_framing_stop_flag.is_set():
            batch_framing_state["status"] = "stopped"
            batch_framing_state["message"] = "Đã dừng tiến trình theo yêu cầu của bạn"
            return

        batch_framing_state["current"] = idx
        batch_framing_state["current_file"] = v_file.name
        batch_framing_state["percent"] = round(((idx - 1) / total) * 100, 1)

        out_name = f"ChongReup_{v_file.stem}.mp4"
        out_path = output_dir / out_name

        # Xây dựng filter graph
        # Đảm bảo khi lật video (mirror), phụ đề tiếng Việt KHÔNG BAO GIỜ bị lật ngược chữ
        raw_stem = v_file.stem
        if raw_stem.startswith("Dubbed_"):
            raw_stem = raw_stem[7:]
        elif raw_stem.startswith("final_"):
            raw_stem = raw_stem[6:]
        
        job_dir = WORKSPACE / raw_stem
        srt_file = None
        if job_dir.is_dir() and (job_dir / "translated.srt").is_file():
            srt_file = job_dir / "translated.srt"
        
        input_v = v_file
        srt_filter = ""
        # Nếu lật ngược và có file phụ đề dịch + phôi gốc, dùng phôi gốc lật rồi ghép sub lên trên
        if mirror and srt_file:
            cand_phoi = INPUT_DIR / f"{raw_stem}.mp4"
            cand_dl = DOWNLOADS_DIR / f"{raw_stem}.mp4"
            if cand_phoi.is_file():
                input_v = cand_phoi
            elif cand_dl.is_file():
                input_v = cand_dl
            
            custom_ass = build_custom_ass_file(job_dir, cfg, tw, th) if job_dir.is_dir() else None
            if custom_ass and custom_ass.is_file():
                clean_ass = str(custom_ass).replace("\\", "/").replace(":", "\\:")
                srt_filter = f",ass='{clean_ass}'"
            else:
                clean_srt = str(srt_file).replace("\\", "/").replace(":", "\\:")
                style_str = build_ass_force_style(cfg, default_font_size=16)
                srt_filter = f",subtitles='{clean_srt}':force_style='{style_str}'"

        # Kiểm tra bất đối xứng thời lượng video vs audio (ngăn ngừa đơ hình ở cuối)
        stream_durs = probe_stream_durations(str(input_v))
        v_dur = stream_durs.get("video")
        a_dur = stream_durs.get("audio") or stream_durs.get("format")
        duration_args = []
        if v_dur and a_dur and (v_dur < a_dur - 1.0):
            logger.info(f"Video {v_file.name} có luồng video ({v_dur:.2f}s) ngắn hơn audio ({a_dur:.2f}s) -> Tự động cắt khớp {v_dur:.2f}s để tránh đơ màn hình")
            duration_args = ["-t", f"{v_dur:.3f}"]

        protect_top_thumb = bool(cfg.get("protect_top_thumb", True))
        crop_padding = int(cfg.get("crop_padding", 0))
        smart_mask_info = None
        if protect_top_thumb:
            smart_mask_info = generate_smart_thumb_mask(str(input_v), tw=tw, th=th, padding=crop_padding)

        if smart_mask_info and smart_mask_info.get("thumb_found"):
            # PHƯƠNG ÁN 1: Tự động bảo vệ thẻ chữ Thumb trên, che toàn bộ viền đen trên & dưới bằng nền mới
            mask_file = smart_mask_info["mask_path"]
            if bg_type == "video_motion" and real_motion_path and os.path.isfile(real_motion_path):
                extra_inputs = ["-stream_loop", "-1", "-i", real_motion_path, "-loop", "1", "-i", mask_file]
                fc = (
                    f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                    f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[2:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
            elif bg_type == "image":
                real_image_path = resolve_image_bg_path(bg_image_file)
                if real_image_path and os.path.isfile(real_image_path):
                    extra_inputs = ["-loop", "1", "-i", real_image_path, "-loop", "1", "-i", mask_file]
                    fc = (
                        f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                        f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                        f"[2:v]scale={tw}:{th},setsar=1[msk];"
                        f"[fg][msk]alphamerge[fg_alpha];"
                        f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                    )
                else:
                    safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                    extra_inputs = ["-loop", "1", "-i", mask_file]
                    fc = (
                        f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                        f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                        f"[1:v]scale={tw}:{th},setsar=1[msk];"
                        f"[fg][msk]alphamerge[fg_alpha];"
                        f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                    )
            elif bg_type == "blur":
                extra_inputs = ["-loop", "1", "-i", mask_file]
                fc = (
                    f"[0:v]split=2[v_orig][v_bg];"
                    f"[v_bg]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=25:5,setsar=1,colorlevels=rimax=0.65:gimax=0.65:bimax=0.65[bg];"
                    f"[v_orig]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[1:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
            else:
                safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                extra_inputs = ["-loop", "1", "-i", mask_file]
                fc = (
                    f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                    f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[1:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
        else:
            # Tự động cắt bỏ 2 viền đen trên/dưới của video phôi nếu có
            crop_prefix = ""
            if auto_crop_black_bars:
                crop_box = detect_letterbox_crop(str(input_v), padding=crop_padding)
                if crop_box:
                    crop_prefix = f"crop={crop_box['w']}:{crop_box['h']}:{crop_box['x']}:{crop_box['y']},"

            # Chế độ co dãn: Khi video_scale >= 0.99 (mặc định), giữ nguyên 100% chiều ngang, 2 bên sát mép (x=0)
            # Background chỉ che 2 phần đen trên và dưới
            if video_scale >= 0.99:
                scale_fg = f"{crop_prefix}{flip_str}scale={tw}:-2,setsar=1"
                overlay_coord = "0:(H-h)/2"
            else:
                scale_fg = f"{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1"
                overlay_coord = "(W-w)/2:(H-h)/2"

            extra_inputs = []
            if bg_type == "video_motion" and real_motion_path and os.path.isfile(real_motion_path):
                extra_inputs = ["-stream_loop", "-1", "-i", real_motion_path]
                fc = (
                    f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                    f"[0:v]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )
            elif bg_type == "image":
                real_image_path = resolve_image_bg_path(bg_image_file)
                if real_image_path and os.path.isfile(real_image_path):
                    extra_inputs = ["-loop", "1", "-i", real_image_path]
                    fc = (
                        f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                        f"[0:v]{scale_fg}[fg];"
                        f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                    )
                else:
                    safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                    fc = (
                        f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                        f"[0:v]{scale_fg}[fg];"
                        f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                    )
            elif bg_type == "blur":
                fc = (
                    f"[0:v]split=2[bg_in][fg_in];"
                    f"[bg_in]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=25:5,setsar=1,colorlevels=rimax=0.65:gimax=0.65:bimax=0.65[bg];"
                    f"[fg_in]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )
            else:
                safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                fc = (
                    f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                    f"[0:v]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )

        # Lồng Watermark mờ mờ cố định ở góc phải nếu được kích hoạt
        wm_filt = build_watermark_filter(cfg, tw, th)
        if wm_filt:
            fc = fc.replace("[outv]", f"{wm_filt}[outv]")

        # Thử mã hóa bằng GPU NVENC trước, nếu lỗi thì fallback sang CPU x264
        cmd_nvenc = [
            "ffmpeg", "-y",
            "-i", str(input_v)
        ] + extra_inputs + [
            "-filter_complex", fc,
            "-map", "[outv]",
            "-map", "0:a?",
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-b:v", "7500k",
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
        ] + duration_args + [
            str(out_path)
        ]

        success = False
        try:
            res = subprocess.run(cmd_nvenc, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            if res.returncode == 0 and _valid_final_video(out_path):
                success = True
        except Exception as e:
            logger.warning(f"NVENC failed for {v_file.name}: {e}")

        if not success:
            # Fallback x264
            cmd_cpu = [
                "ffmpeg", "-y",
                "-i", str(v_file)
            ] + extra_inputs + [
                "-filter_complex", fc,
                "-map", "[outv]",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest",
            ] + duration_args + [
                str(out_path)
            ]
            try:
                res2 = subprocess.run(cmd_cpu, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
                if res2.returncode == 0 and _valid_final_video(out_path):
                    success = True
            except Exception as e2:
                logger.error(f"CPU render failed for {v_file.name}: {e2}")

        if success:
            clean_out_str = str(out_path).replace("\\", "/")
            batch_framing_state["completed_files"].append({
                "name": out_name,
                "path": clean_out_str,
                "size_mb": round(out_path.stat().st_size / (1024 * 1024), 2),
                "url": f"/api/workflow/stream-file?path={clean_out_str}"
            })
        else:
            batch_framing_state["error_files"].append(v_file.name)

        batch_framing_state["percent"] = round((idx / total) * 100, 1)

    batch_framing_state["status"] = "done"
    batch_framing_state["current"] = total
    batch_framing_state["percent"] = 100.0
    batch_framing_state["message"] = f"Hoàn tất! Đã chèn khung chống re-up cho {len(batch_framing_state['completed_files'])}/{total} video."


@router.post("/api/workflow/batch-framing/start")
async def api_start_batch_framing(payload: Dict[str, Any] = Body(...)):
    """Khởi chạy tiến trình lồng khung chống re-up hàng loạt cho cả thư mục."""
    global batch_framing_thread, batch_framing_state
    if batch_framing_state.get("status") == "running":
        return {"status": "error", "message": "Đang có một tiến trình chèn khung hàng loạt đang chạy!"}

    input_folder = payload.get("input_folder", "").strip()
    output_folder = payload.get("output_folder", "").strip()
    if not input_folder:
        raise HTTPException(status_code=400, detail="Vui lòng nhập thư mục đầu vào")
    if not output_folder:
        output_folder = str(Path(input_folder) / "chong_reup")

    input_dir = Path(input_folder)
    output_dir = Path(output_folder)
    if not input_dir.is_dir():
        raise HTTPException(status_code=404, detail="Thư mục đầu vào không tồn tại")

    batch_framing_state.update({
        "status": "starting",
        "current": 0,
        "total": 0,
        "percent": 0.0,
        "input_folder": str(input_dir).replace("\\", "/"),
        "output_folder": str(output_dir).replace("\\", "/"),
        "completed_files": [],
        "error_files": [],
        "message": "Đang khởi tạo danh sách video..."
    })

    batch_framing_thread = threading.Thread(
        target=_batch_framing_worker,
        args=(input_dir, output_dir, payload),
        daemon=True
    )
    batch_framing_thread.start()
    return {"status": "ok", "message": "Đã bắt đầu tiến trình chèn khung chống re-up hàng loạt"}


@router.get("/api/workflow/batch-framing/status")
async def api_get_batch_framing_status():
    """Lấy trạng thái tiến trình chèn khung hàng loạt theo thời gian thực."""
    return batch_framing_state


@router.post("/api/workflow/batch-framing/stop")
async def api_stop_batch_framing():
    """Dừng tiến trình chèn khung hàng loạt ngay lập tức."""
    batch_framing_stop_flag.set()
    batch_framing_state["status"] = "stopping"
    batch_framing_state["message"] = "Đang dừng lại..."
    return {"status": "ok", "message": "Đã gửi yêu cầu dừng"}

# =========================================================================
# FEATURE: THUMBNAIL MANAGEMENT (CAPTURE, UPLOAD, VIEW, DOWNLOAD)
# =========================================================================

THUMBNAILS_DIR = WORKSPACE / "thumbnails"
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

def _find_video_for_stem(stem: str) -> Optional[Path]:
    """Tìm đường dẫn tệp video cho stem đã chọn."""
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    job_dir = WORKSPACE / clean_stem
    cand_phoi = INPUT_DIR / f"{stem}.mp4"
    cand_dl = DOWNLOADS_DIR / f"{stem}.mp4"
    cand_banve = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
    cand_step1 = job_dir / "step1_extracted.mp4" if job_dir.is_dir() else None
    
    for cand in (cand_banve, cand_phoi, cand_dl, cand_step1):
        if cand and cand.is_file() and cand.stat().st_size > 1000:
            return cand
            
    for d in (DOWNLOADS_DIR, INPUT_DIR, OUTPUT_DIR):
        if d.is_dir():
            for f in d.glob("*"):
                if f.is_file() and f.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") and (f.stem == stem or clean_stem in f.stem):
                    return f
    return None

@router.get("/api/workflow/thumbnail/{stem}")
async def api_get_video_thumbnail(stem: str):
    """Lấy ảnh thumbnail của video theo stem. Nếu chưa có, tự động trích xuất frame tại 1.0s."""
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    
    if thumb_file.is_file() and thumb_file.stat().st_size > 500:
        return FileResponse(thumb_file, media_type="image/jpeg")
        
    job_dir = WORKSPACE / clean_stem
    if job_dir.is_dir() and (job_dir / "thumbnail.jpg").is_file():
        try:
            import shutil
            shutil.copy2(job_dir / "thumbnail.jpg", thumb_file)
            return FileResponse(thumb_file, media_type="image/jpeg")
        except Exception:
            pass

    video_source = _find_video_for_stem(stem)
    if video_source:
        try:
            cmd = [
                "ffmpeg", "-y",
                "-ss", "1.0",
                "-i", str(video_source),
                "-vframes", "1",
                "-q:v", "2",
                str(thumb_file)
            ]
            res = subprocess.run(cmd, capture_output=True, timeout=12)
            if res.returncode == 0 and thumb_file.is_file() and thumb_file.stat().st_size > 500:
                return FileResponse(thumb_file, media_type="image/jpeg")
        except Exception as e:
            logger.warning(f"Lỗi tự động trích xuất thumbnail cho {stem}: {e}")

    raise HTTPException(status_code=404, detail="Thumbnail not found")


@router.post("/api/workflow/thumbnail/capture")
async def api_capture_thumbnail(payload: Dict[str, Any] = Body(...)):
    """Chụp khung hình tại giây chỉ định của video làm Thumbnail."""
    stem = payload.get("stem", "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    
    try:
        timestamp_s = float(payload.get("timestamp_s", 1.0))
    except (ValueError, TypeError):
        timestamp_s = 1.0
    if timestamp_s < 0:
        timestamp_s = 0.0
        
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    
    video_source = _find_video_for_stem(stem)
    if not video_source:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp video để chụp thumbnail")
        
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{timestamp_s:.3f}",
            "-i", str(video_source),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_file)
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=12)
        if res.returncode == 0 and thumb_file.is_file() and thumb_file.stat().st_size > 500:
            job_dir = WORKSPACE / clean_stem
            if job_dir.is_dir():
                try:
                    import shutil
                    shutil.copy2(thumb_file, job_dir / "thumbnail.jpg")
                except Exception:
                    pass
            return {
                "status": "ok",
                "success": True,
                "message": f"Đã chụp khung hình tại {timestamp_s:.2f}s làm Thumbnail thành công!",
                "thumbnail_url": f"/api/workflow/thumbnail/{clean_stem}?t={time.time()}",
                "timestamp_s": timestamp_s
            }
        else:
            raise RuntimeError(f"FFmpeg capture thất bại: {res.stderr.decode('utf-8', 'ignore')}")
    except Exception as e:
        logger.error(f"Lỗi chụp thumbnail cho {stem}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/workflow/thumbnail/upload")
async def api_upload_thumbnail(stem: str = Form(...), file: UploadFile = File(...)):
    """Tải tệp ảnh từ máy tính lên để làm Thumbnail cho video."""
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    
    content = await file.read()
    if len(content) < 500:
        raise HTTPException(status_code=400, detail="Tệp ảnh không hợp lệ hoặc quá nhỏ")
        
    thumb_file.write_bytes(content)
    
    job_dir = WORKSPACE / clean_stem
    if job_dir.is_dir():
        try:
            (job_dir / "thumbnail.jpg").write_bytes(content)
        except Exception:
            pass
            
    return {
        "status": "ok",
        "success": True,
        "message": f"Đã tải lên Thumbnail cho video thành công!",
        "thumbnail_url": f"/api/workflow/thumbnail/{clean_stem}?t={time.time()}"
    }


@router.get("/api/workflow/thumbnail/download/{stem}")
async def api_download_thumbnail(stem: str):
    """Tải tệp thumbnail về máy tính."""
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    if thumb_file.is_file():
        return FileResponse(thumb_file, filename=f"thumbnail_{clean_stem}.jpg", media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Chưa có thumbnail cho video này")


def _prepend_thumbnail_file(video_path: Path, thumb_path: Path, duration_s: float = 0.8) -> bool:
    """Ghép ảnh thumbnail vào đầu video_path bằng FFmpeg NVENC (fallback CPU)."""
    if not video_path.is_file() or not thumb_path.is_file():
        return False
    duration_s = max(0.2, min(5.0, float(duration_s)))
    cmd_probe = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate,sample_rate,channel_layout,channels",
        "-of", "json", str(video_path)
    ]
    p_res = subprocess.run(cmd_probe, capture_output=True, text=True, encoding="utf-8", errors="replace")
    streams = []
    try:
        streams = json.loads(p_res.stdout).get("streams", [])
    except Exception:
        pass

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    tw = v_stream.get("width") or 1080
    th = v_stream.get("height") or 1920
    fps = v_stream.get("r_frame_rate") or "30/1"
    sr = (a_stream.get("sample_rate") if a_stream else None) or "44100"

    temp_out = video_path.parent / f"_prep_{video_path.name}"

    # Chỉ scale và crop ảnh thumbnail [0:v] về đúng kích thước tw x th và fps của video chính [1:v]
    # Giữ nguyên video chính [1:v] với setsar=1,fps={fps} để tránh tốn tài nguyên scale/crop toàn bộ video
    filter_complex = (
        f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1,fps={fps}[thumb_v];"
        f"[1:v]setsar=1,fps={fps}[main_v];"
        f"[thumb_v][main_v]concat=n=2:v=1:a=0[outv]"
    )

    audio_maps = []
    if a_stream:
        ch_layout = a_stream.get("channel_layout") or ("stereo" if a_stream.get("channels") == 2 else "mono")
        filter_complex += (
            f";aevalsrc=0:d={duration_s}:s={sr}[silent_raw];"
            f"[silent_raw]aformat=sample_rates={sr}:channel_layouts={ch_layout}[silent_a];"
            f"[1:a]aformat=sample_rates={sr}:channel_layouts={ch_layout}[main_a];"
            f"[silent_a][main_a]concat=n=2:v=0:a=1[outa]"
        )
        audio_maps = ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"]

    cmd_nvenc = [
        "ffmpeg", "-y",
        "-loop", "1", "-t", str(duration_s), "-i", str(thumb_path),
        "-i", str(video_path),
        "-filter_complex", filter_complex,
        "-map", "[outv]",
    ] + audio_maps + [
        "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "4000k",
        "-shortest", "-movflags", "+faststart",
        str(temp_out)
    ]

    success = False
    try:
        res = subprocess.run(cmd_nvenc, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if res.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 1000:
            success = True
    except Exception as e:
        logger.warning(f"Prepend NVENC failed: {e}")

    if not success:
        cmd_cpu = [
            "ffmpeg", "-y",
            "-loop", "1", "-t", str(duration_s), "-i", str(thumb_path),
            "-i", str(video_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]",
        ] + audio_maps + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-shortest", "-movflags", "+faststart",
            str(temp_out)
        ]
        res2 = subprocess.run(cmd_cpu, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        if res2.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 1000:
            success = True

    if success:
        try:
            video_path.unlink(missing_ok=True)
            temp_out.rename(video_path)
            return True
        except Exception:
            import shutil
            shutil.copy2(temp_out, video_path)
            temp_out.unlink(missing_ok=True)
            return True
    else:
        if temp_out.is_file():
            temp_out.unlink(missing_ok=True)
        return False


@router.post("/api/workflow/thumbnail/prepend")
async def api_prepend_thumbnail_to_video(payload: Dict[str, Any] = Body(...)):
    """Ghép ảnh Thumbnail (đã chèn chữ) vào đầu video trong khoảng thời lượng duration_s (mặc định 0.8s)."""
    stem = payload.get("stem", "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    duration_s = float(payload.get("duration_s", 0.8))
    duration_s = max(0.2, min(5.0, duration_s))

    # 1. Tìm thumbnail
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    job_dir = WORKSPACE / clean_stem
    if not thumb_file.is_file() and job_dir.is_dir() and (job_dir / "thumbnail.jpg").is_file():
        thumb_file = job_dir / "thumbnail.jpg"

    # 2. Tìm video nguồn và video thành phẩm
    source_video = None
    if INPUT_DIR.exists():
        cand = INPUT_DIR / f"{stem}.mp4"
        if cand.is_file():
            source_video = cand
    if not source_video and DOWNLOADS_DIR.exists():
        cand = DOWNLOADS_DIR / f"{stem}.mp4"
        if cand.is_file():
            source_video = cand

    final_video = None
    if job_dir.is_dir():
        for item in job_dir.iterdir():
            if item.name == f"final_{stem}.mp4" and _valid_final_video(item):
                final_video = item
                break
    if not final_video and OUTPUT_DIR.exists():
        cand_banve = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
        if cand_banve.is_file() and _valid_final_video(cand_banve):
            final_video = cand_banve

    target_video = final_video if final_video else source_video
    if not target_video or not target_video.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp video để ghép thumbnail")

    # Nếu chưa có thumbnail, tự động trích xuất frame tại 1.0s
    if not thumb_file.is_file():
        cmd_extract = [
            "ffmpeg", "-y",
            "-ss", "1.0",
            "-i", str(target_video),
            "-vframes", "1",
            "-q:v", "2",
            str(thumb_file)
        ]
        subprocess.run(cmd_extract, capture_output=True, timeout=10)

    if not thumb_file.is_file():
        raise HTTPException(status_code=400, detail="Không thể tạo hoặc tìm thấy ảnh thumbnail")

    job_dir.mkdir(parents=True, exist_ok=True)
    dest_final = job_dir / f"final_{stem}.mp4"

    # Nếu target_video khác dest_final (ví dụ đang dùng source_video hoặc cand_banve), tạo bản sao sang dest_final trước
    if str(target_video.resolve()) != str(dest_final.resolve()):
        import shutil
        shutil.copy2(target_video, dest_final)
        target_video = dest_final

    ok = _prepend_thumbnail_file(dest_final, thumb_file, duration_s)
    if not ok:
        raise HTTPException(status_code=500, detail="Không thể ghép thumbnail vào đầu video bằng FFmpeg")

    stream_url = f"/api/workflow/stream-file?path={dest_final}&t={time.time()}"
    return {
        "status": "ok",
        "success": True,
        "message": f"Đã ghép Thumbnail ({duration_s:.1f}s) vào đầu video thành công!",
        "video_url": stream_url,
        "final_video_url": stream_url,
        "has_final": True
    }


# =========================================================================
# FEATURE: CAPCUT-STYLE TIMELINE CUT, CLONE & CONCATENATION
# =========================================================================

# Giới hạn hiệu ứng clip P2
_TIMELINE_SPEED_MIN = 0.25
_TIMELINE_SPEED_MAX = 4.0
_TIMELINE_REVERSE_MAX_SRC_SEC = 60.0  # reverse buffer toàn bộ frame trong RAM


def _timeline_clip_effects(c: Dict[str, Any]) -> Dict[str, Any]:
    """Đọc & kẹp giới hạn thuộc tính clip kiểu CapCut (speed/volume/fade/crop/...)."""

    def _num(key: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(c.get(key, default) or default)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    speed = _num("speed", 1.0, _TIMELINE_SPEED_MIN, _TIMELINE_SPEED_MAX)
    crop = None
    crop_raw = c.get("crop") if isinstance(c.get("crop"), dict) else None
    if crop_raw:
        try:
            cx = min(0.95, max(0.0, float(crop_raw.get("x", 0.0))))
            cy = min(0.95, max(0.0, float(crop_raw.get("y", 0.0))))
            cw = min(1.0, max(0.05, float(crop_raw.get("w", 1.0))))
            ch = min(1.0, max(0.05, float(crop_raw.get("h", 1.0))))
            cw = min(cw, 1.0 - cx)
            ch = min(ch, 1.0 - cy)
            crop = {"x": cx, "y": cy, "w": cw, "h": ch}
        except Exception:
            crop = None
    rot = int(_num("rotate", 0, 0, 359))
    rot = rot - (rot % 90)

    # P4: filter màu/hiệu ứng per-clip (eq/colorbalance/boxblur/vignette)
    fx = None
    fx_raw = c.get("fx") if isinstance(c.get("fx"), dict) else None
    if fx_raw:
        try:
            fx = {
                "brightness": max(-1.0, min(1.0, float(fx_raw.get("brightness", 0.0) or 0.0))),
                "contrast": max(0.0, min(3.0, float(fx_raw.get("contrast", 1.0) or 1.0))),
                "saturation": max(0.0, min(3.0, float(fx_raw.get("saturation", 1.0) or 1.0))),
                "temperature": max(-1.0, min(1.0, float(fx_raw.get("temperature", 0.0) or 0.0))),
                "blur": max(0.0, min(20.0, float(fx_raw.get("blur", 0.0) or 0.0))),
                "vignette": bool(fx_raw.get("vignette")),
            }
            neutral = (abs(fx["brightness"]) < 1e-3 and abs(fx["contrast"] - 1.0) < 1e-3
                       and abs(fx["saturation"] - 1.0) < 1e-3 and abs(fx["temperature"]) < 1e-3
                       and fx["blur"] < 0.5 and not fx["vignette"])
            if neutral:
                fx = None
        except Exception:
            fx = None

    return {
        "speed": speed,
        "volume": _num("volume", 1.0, 0.0, 2.0),
        "muted": bool(c.get("muted")),
        "keep_pitch": bool(c.get("keepPitch", True)),
        "fade_in": _num("fadeIn", 0.0, 0.0, 10.0),
        "fade_out": _num("fadeOut", 0.0, 0.0, 10.0),
        "crop": crop,
        "rotate": rot if rot in (90, 180, 270) else 0,
        "flip_h": bool(c.get("flipH")),
        "flip_v": bool(c.get("flipV")),
        "reverse": bool(c.get("reverse")),
        "freeze": _num("freeze", 0.0, 0.0, 10.0),
        "detach_audio": bool(c.get("detachAudio")),
        "fx": fx,
    }


def _atempo_chain(speed: float) -> List[str]:
    """
    Chuỗi atempo để giữ cao độ giọng khi đổi tốc độ.
    FFmpeg chỉ nhận atempo trong [0.5, 2.0] nên phải phân tích thành tích các hệ số.
    """
    parts: List[str] = []
    s = float(speed)
    while s > 2.0 + 1e-9:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5 - 1e-9:
        parts.append("atempo=0.5")
        s *= 2.0
    if abs(s - 1.0) > 1e-6:
        parts.append(f"atempo={s:.5f}")
    return parts


def _build_timeline_filter_graph(
    clips: List[Dict[str, Any]],
    has_audio: bool,
    src_w: int = 0,
    src_h: int = 0,
) -> Dict[str, Any]:
    """
    Sinh filter_complex hoàn chỉnh cho timeline CapCut (P2) — HÀM THUẦN để unit test.

    Mỗi clip có thể mang: speed (0.25–4x, setpts + atempo giữ pitch), volume/mute,
    fadeIn/fadeOut (video+audio), crop {x,y,w,h} chuẩn hóa (luôn format=yuv420p TRƯỚC
    crop theo bug NVDEC nv12), rotate 90/180/270, flipH/flipV, reverse (giới hạn 60s
    nguồn vì buffer cả đoạn trong RAM), freeze-frame đầu đoạn (tpad + im lặng audio
    tương ứng), detachAudio (audio giữ tốc độ gốc → chuyển sang mode amix+adelay),
    fx (P4: eq/colorbalance/boxblur/vignette per-clip) và trans (P4: chuyển cảnh sang
    đoạn kế — chuỗi xfade/acrossfade thay concat, seg_starts rút ngắn theo duration).

    Trả về: {"filter_lines", "mode": "concat"|"mix", "total_dur", "seg_starts",
              "seg_durs", "effects"} — filter_lines kết thúc bằng [outv] (và [outa] nếu có âm thanh).
    """
    effects = [_timeline_clip_effects(c) for c in clips]
    if not effects:
        return {"filter_lines": [], "mode": "concat", "total_dur": 0.0, "seg_starts": [], "seg_durs": [], "effects": []}

    filter_lines: List[str] = []
    v_parts: List[str] = []
    a_parts: List[str] = []
    seg_starts: List[float] = []
    seg_durs: List[float] = []

    tl_cursor = 0.0
    for idx, (c, eff) in enumerate(zip(clips, effects)):
        st = c["start"]
        en = c["end"]
        src_dur = max(0.05, en - st)
        speed = eff["speed"]
        freeze = eff["freeze"]
        tl_dur = src_dur / speed + freeze
        tl_start = tl_cursor
        tl_cursor += tl_dur
        seg_starts.append(tl_start)
        seg_durs.append(tl_dur)

        # ---- VIDEO ----
        v_tag = f"v{idx}"
        vchain = [f"trim=start={st:.3f}:end={en:.3f}"]
        if abs(speed - 1.0) > 1e-6:
            vchain.append(f"setpts=(PTS-STARTPTS)/{speed:.5f}")
        else:
            vchain.append("setpts=PTS-STARTPTS")
        # Ràng buộc repo: format=yuv420p PHẢI đứng trước crop/transpose (bug NVDEC nv12 xanh nửa phải)
        vchain.append("format=yuv420p")
        # P4: filter màu/hiệu ứng per-clip (đặt ngay sau format, trước crop/rotate)
        if eff["fx"]:
            fx = eff["fx"]
            if abs(fx["brightness"]) > 1e-3 or abs(fx["contrast"] - 1.0) > 1e-3 or abs(fx["saturation"] - 1.0) > 1e-3:
                vchain.append(
                    f"eq=brightness={fx['brightness']:.3f}:contrast={fx['contrast']:.3f}:saturation={fx['saturation']:.3f}"
                )
            if abs(fx["temperature"]) > 1e-3:
                t_amt = fx["temperature"] * 0.3
                vchain.append(f"colorbalance=rs={t_amt:.3f}:bs={-t_amt:.3f}")
            if fx["blur"] > 0:
                vchain.append(f"boxblur={min(20, max(1, int(round(fx['blur']))))}")
            if fx["vignette"]:
                vchain.append("vignette=PI/5")
        if freeze > 0:
            vchain.append(f"tpad=start_duration={freeze:.3f}:start_mode=add:color=black")
        if eff["crop"]:
            cr = eff["crop"]
            vchain.append(
                "crop=w='trunc(iw*%.5f/2)*2':h='trunc(ih*%.5f/2)*2':"
                "x='trunc(iw*%.5f/2)*2':y='trunc(ih*%.5f/2)*2'"
                % (cr["w"], cr["h"], cr["x"], cr["y"])
            )
        if eff["rotate"] == 90:
            vchain.append("transpose=1")
        elif eff["rotate"] == 180:
            vchain.append("transpose=1,transpose=1")
        elif eff["rotate"] == 270:
            vchain.append("transpose=2")
        if eff["flip_h"]:
            vchain.append("hflip")
        if eff["flip_v"]:
            vchain.append("vflip")
        allow_reverse = eff["reverse"] and src_dur <= _TIMELINE_REVERSE_MAX_SRC_SEC
        if eff["reverse"] and not allow_reverse:
            logger.warning(
                f"Timeline clip #{idx}: reverse bị bỏ qua vì đoạn dài {src_dur:.1f}s "
                f">(giới hạn {_TIMELINE_REVERSE_MAX_SRC_SEC:.0f}s)"
            )
        if allow_reverse:
            vchain.append("reverse")
        if eff["fade_in"] > 0:
            vchain.append(f"fade=t=in:st=0:d={eff['fade_in']:.3f}")
        if eff["fade_out"] > 0:
            vchain.append(f"fade=t=out:st={max(0.0, tl_dur - eff['fade_out']):.3f}:d={eff['fade_out']:.3f}")
        # Sau crop/rotate kích thước thay đổi → ép về đúng kích thước nguồn để concat không lệch
        needs_refit = bool(eff["crop"]) or eff["rotate"] in (90, 270)
        if needs_refit and src_w > 0 and src_h > 0:
            vchain.append(f"scale={src_w}:{src_h}:force_original_aspect_ratio=decrease")
            vchain.append(f"pad={src_w}:{src_h}:(ow-iw)/2:(oh-ih)/2:color=black")
        filter_lines.append("[0:v]" + ",".join(vchain) + f"[{v_tag}]")
        v_parts.append(f"[{v_tag}]")

        # ---- AUDIO ----
        if has_audio:
            a_tag = f"a{idx}"
            achain = [f"atrim=start={st:.3f}:end={en:.3f}"]
            if eff["muted"]:
                achain.append("volume=0")  # vẫn giữ nhánh audio để concat đều
            else:
                if not eff["detach_audio"] and abs(speed - 1.0) > 1e-6:
                    if eff["keep_pitch"]:
                        achain.append("asetpts=PTS-STARTPTS")
                        achain.extend(_atempo_chain(speed))
                    else:
                        achain.append(f"asetpts=(PTS-STARTPTS)/{speed:.5f}")
                else:
                    achain.append("asetpts=PTS-STARTPTS")
                if abs(eff["volume"] - 1.0) > 1e-6:
                    achain.append(f"volume={eff['volume']:.5f}")
            if freeze > 0:
                # im lặng đúng khoảng khung hình đóng băng để A/V khớp
                achain.append(f"adelay={int(round(freeze * 1000))}:all=1")
            if allow_reverse:
                achain.append("areverse")
            a_dur = src_dur if eff["detach_audio"] else tl_dur
            if eff["fade_in"] > 0:
                achain.append(f"afade=t=in:st=0:d={eff['fade_in']:.3f}")
            if eff["fade_out"] > 0:
                achain.append(f"afade=t=out:st={max(0.0, a_dur - eff['fade_out']):.3f}:d={eff['fade_out']:.3f}")
            filter_lines.append("[0:a]" + ",".join(achain) + f"[{a_tag}]")
            a_parts.append(f"[{a_tag}]")

    n = len(clips)

    # P4: đọc transition giữa các cặp clip liền kề (client đã dồn `at` của đoạn sau khi đặt)
    trans_list: List[tuple] = []  # (idx, type, dur)
    for i in range(n - 1):
        traw = clips[i].get("trans") if isinstance(clips[i].get("trans"), dict) else None
        if not traw:
            continue
        ttype = str(traw.get("type") or "").strip().lower()
        if not ttype:
            continue
        tdur = max(0.0, min(1.0, float(traw.get("dur", 0.5) or 0.5)))
        tdur = min(tdur, 0.9 * min(seg_durs[i], seg_durs[i + 1]))
        if tdur < 0.05:
            continue
        trans_list.append((i, ttype, tdur))

    # transition rút ngắn output: vị trí output của các đoạn sau dời về trái tương ứng
    for (_i, _t, tdur) in trans_list:
        for j in range(_i + 1, n):
            seg_starts[j] -= tdur
    total_dur = sum(seg_durs) - sum(td for (_i, _t, td) in trans_list)
    any_detach = has_audio and any(e["detach_audio"] for e in effects)

    if not trans_list:
        if has_audio and not any_detach:
            # Mode proven: concat A/V đồng bộ từng cặp (không đổi hành vi cũ khi không có detach)
            concat_in = "".join(v + a for v, a in zip(v_parts, a_parts))
            filter_lines.append(f"{concat_in}concat=n={n}:v=1:a=1[outv][outa]")
            mode = "concat"
        elif has_audio:
            # Mode tách audio: video concat riêng; audio từng clip đặt đúng vị trí timeline rồi trộn
            filter_lines.append("".join(v_parts) + f"concat=n={n}:v=1:a=0[outv]")
            for i, (a_tag, tl_start) in enumerate(zip(a_parts, seg_starts)):
                filter_lines.append(f"{a_tag}adelay={int(round(tl_start * 1000))}:all=1[ad{i}]")
            filter_lines.append(
                "".join(f"[ad{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0:duration=longest[mixout]"
            )
            filter_lines.append(f"[mixout]apad=whole_dur={total_dur:.3f}[outa]")
            mode = "mix"
        else:
            filter_lines.append("".join(v_parts) + f"concat=n={n}:v=1:a=0[outv]")
            mode = "concat"
    else:
        # P4: có transition — nối video bằng chuỗi xfade (cặp có trans) / concat (cặp thường);
        # offset của xfade = thời điểm bắt đầu chuyển cảnh trên output
        acc_v = seg_durs[0]
        prev_tag = "[v0]"
        for i in range(n - 1):
            tr = next((t for t in trans_list if t[0] == i), None)
            last_join = (i == n - 2)
            if tr:
                _i, ttype, tdur = tr
                offset = max(0.0, acc_v - tdur)
                out_tag = "[outv]" if last_join else f"[vx{i}]"
                filter_lines.append(
                    f"{prev_tag}[v{i + 1}]xfade=transition={ttype}:duration={tdur:.3f}:offset={offset:.3f}{out_tag}"
                )
                acc_v = acc_v + seg_durs[i + 1] - tdur
            else:
                out_tag = "[outv]" if last_join else f"[vc{i}]"
                filter_lines.append(f"{prev_tag}[v{i + 1}]concat=n=2:v=1:a=0{out_tag}")
                acc_v = acc_v + seg_durs[i + 1]
            prev_tag = out_tag
        if has_audio and not any_detach:
            # audio đisong song: acrossfade tại cặp có trans, concat tại cặp thường
            prev_a = "[a0]"
            for i in range(n - 1):
                tr = next((t for t in trans_list if t[0] == i), None)
                last_join = (i == n - 2)
                if tr:
                    out_tag = "[outa]" if last_join else f"[ax{i}]"
                    filter_lines.append(f"{prev_a}[a{i + 1}]acrossfade=d={tr[2]:.3f}:c1=tri:c2=tri{out_tag}")
                else:
                    out_tag = "[outa]" if last_join else f"[ac{i}]"
                    filter_lines.append(f"{prev_a}[a{i + 1}]concat=n=2:v=1:a=0{out_tag}")
                prev_a = out_tag
            mode = "concat"
        elif has_audio:
            # mode mix: audio amix theo seg_starts (đã tính transition), video xfade như trên
            for i, (a_tag, tl_start) in enumerate(zip(a_parts, seg_starts)):
                filter_lines.append(f"{a_tag}adelay={int(round(tl_start * 1000))}:all=1[ad{i}]")
            filter_lines.append(
                "".join(f"[ad{i}]" for i in range(n)) + f"amix=inputs={n}:normalize=0:duration=longest[mixout]"
            )
            filter_lines.append(f"[mixout]apad=whole_dur={total_dur:.3f}[outa]")
            mode = "mix"
        else:
            mode = "concat"

    return {
        "filter_lines": filter_lines,
        "mode": mode,
        "total_dur": total_dur,
        "seg_starts": seg_starts,
        "seg_durs": seg_durs,
        "effects": effects,
        "v_parts": v_parts,
        "a_parts": a_parts,
    }


# =========================================================================
# P3: TEXT / OVERLAY CLIPS (chữ tự do + logo/sticker, burn ASS + overlay filter)
# =========================================================================

def _hex_to_ass_color(hex_color: str, alpha: str = "00") -> str:
    """'#RRGGBB' → '&HAABBGGRR' (ASS dùng BGR; alpha 00 = đục)."""
    m = re.fullmatch(r"#?([0-9A-Fa-f]{6})", (hex_color or "").strip())
    if not m:
        return f"&H{alpha}FFFFFF"
    v = m.group(1).upper()
    r, g, b = v[0:2], v[2:4], v[4:6]
    return f"&H{alpha}{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    """Giây → 'H:MM:SS.CC' (centisecond) theo chuẩn ASS."""
    s = max(0.0, float(seconds))
    cs = int(round(s * 100))
    h, rem = divmod(cs, 360000)
    mm, rem = divmod(rem, 6000)
    ss, cc = divmod(rem, 100)
    return f"{h}:{mm:02d}:{ss:02d}.{cc:02d}"


def _build_text_ass_clips(
    text_clips: List[Dict[str, Any]],
    play_res_x: int = 1080,
    play_res_y: int = 1920,
) -> str:
    """
    P3: sinh nội dung file ASS cho các clip chữ tự do — HÀM THUẦN để unit test.

    Mỗi clip: {at, dur (giây TRONG FILE XUẤT — đã map qua _timeline_to_output_t),
               text, pos {x,y} chuẩn hóa (tâm chữ), style {fontSize, color, outlineColor,
               outlineWidth, bgOpacity, bold}, animIn ('none'|'fade'|'slideUp'|'pop'),
               animOut ('none'|'fade')}
    Mỗi clip một Style riêng (để dùng BorderStyle=3 — khung nền — khi bgOpacity > 0).
    Vị trí dùng \\an5 + \\pos theo PlayRes thật nên khớp preview ≤ 1-2 px.
    """
    res_x = max(2, int(play_res_x or 1080))
    res_y = max(2, int(play_res_y or 1920))
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {res_x}",
        f"PlayResY: {res_y}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
    ]

    def _clip_style(i: int, c: Dict[str, Any]) -> str:
        style = c.get("style") or {}
        fs = max(12.0, float(style.get("fontSize", 64) or 64))
        color = _hex_to_ass_color(style.get("color", "#FFFFFF"))
        oc = _hex_to_ass_color(style.get("outlineColor", "#000000"))
        ow = max(0.0, float(style.get("outlineWidth", 3) or 3))
        bg_op = min(1.0, max(0.0, float(style.get("bgOpacity", 0) or 0)))
        bold = -1 if style.get("bold") else 0
        back_alpha = f"{int(round((1.0 - bg_op) * 255)):02X}"
        border_style = 3 if bg_op > 0.01 else 1
        return (
            f"Style: TC{i},Arial,{fs:.0f},{color},&H00FFFFFF,{oc},"
            f"&H{back_alpha}000000,{bold},0,0,0,100,100,0,0,{border_style},{ow:.1f},0,5,10,10,10,1"
        )

    body_styles: List[str] = []
    body_events: List[str] = []
    for i, c in enumerate(text_clips):
        body_styles.append(_clip_style(i, c))
        at = max(0.0, float(c.get("at", 0.0) or 0.0))
        dur = max(0.1, float(c.get("dur", 3.0) or 3.0))
        pos = c.get("pos") if isinstance(c.get("pos"), dict) else {}
        px = min(1.0, max(0.0, float(pos.get("x", 0.5)))) * res_x
        py = min(1.0, max(0.0, float(pos.get("y", 0.8)))) * res_y

        anim_in = (c.get("animIn") or "none").strip().lower()
        anim_out = (c.get("animOut") or "none").strip().lower()
        tags = ["\\an5"]
        if anim_in == "slideup":
            tags.append(f"\\move({px:.0f},{min(res_y - 2, py + 140):.0f},{px:.0f},{py:.0f},0,250)")
        else:
            tags.append(f"\\pos({px:.0f},{py:.0f})")
        if anim_in == "pop":
            tags.append("\\fscx40\\fscy40\\t(0,220,\\fscx100\\fscy100)")
        if anim_in == "fade" or anim_out == "fade":
            in_ms = 250 if anim_in == "fade" else 0
            out_ms = 250 if anim_out == "fade" else 0
            tags.append(f"\\fad({in_ms},{out_ms})")

        raw_text = str(c.get("text") or "").replace("{", "(").replace("}", ")").strip()
        ass_text = raw_text.replace("\r\n", "\n").replace("\n", "\\N") or " "
        body_events.append(
            f"Dialogue: 0,{_ass_time(at)},{_ass_time(at + dur)},TC{i},,0,0,0,,"
            f"{{{''.join(tags)}}}{ass_text}"
        )

    lines.extend(body_styles)
    lines.extend(["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"])
    lines.extend(body_events)
    return "\n".join(lines) + "\n"


def _timeline_to_output_t(clips_with_at: List[Dict[str, Any]], graph: Dict[str, Any], t: float) -> float:
    """
    Map giây trên TIMELINE (có gap) → giây trong FILE XUẤT (gap bị concat ghép lại).
    Dùng cho text/overlay để chữ xuất hiện đúng nội dung như preview (P3).
    """
    seg_starts = graph.get("seg_starts") or []
    seg_durs = graph.get("seg_durs") or []
    if not seg_starts:
        return max(0.0, float(t or 0.0))
    best = None
    best_at = None
    for i, c in enumerate(clips_with_at):
        at_i = max(0.0, float(c.get("at", 0.0) or 0.0))
        if at_i <= float(t or 0.0) + 1e-6 and (best_at is None or at_i >= best_at):
            best, best_at = i, at_i
    if best is None:
        return float(seg_starts[0])
    at_i = best_at or 0.0
    dur_i = float(seg_durs[best]) if best < len(seg_durs) else 0.0
    local = min(max(0.0, float(t or 0.0) - at_i), dur_i)
    return float(seg_starts[best]) + local


def _decode_overlay_assets(overlay_clips: List[Dict[str, Any]], workspace_dir: Path) -> List[Optional[Path]]:
    """Giải mã dataURL ảnh overlay → file trong <workspace>/overlay_assets (P3). None nếu hỏng."""
    import base64

    assets_dir = Path(workspace_dir) / "overlay_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Optional[Path]] = []
    for i, c in enumerate(overlay_clips or []):
        src = str(c.get("src") or "")
        m = re.match(r"data:image/(png|jpeg|jpg|webp);base64,", src)
        if not m:
            paths.append(None)
            continue
        ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
        try:
            raw = base64.b64decode(src[m.end():], validate=False)
        except Exception:
            paths.append(None)
            continue
        cid = re.sub(r"[^\w\-]", "", str(c.get("id", "")))[:40] or f"ov{i}"
        p = assets_dir / f"{cid}.{ext}"
        p.write_bytes(raw)
        paths.append(p)
    return paths


def _decode_audio_assets(audio_clips: List[Dict[str, Any]], workspace_dir: Path) -> List[Optional[Path]]:
    """P5: giải mã dataURL nhạc/SFX → file trong <workspace>/audio_assets. None nếu hỏng."""
    import base64

    assets_dir = Path(workspace_dir) / "audio_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    paths: List[Optional[Path]] = []
    for i, c in enumerate(audio_clips or []):
        src = str(c.get("src") or "")
        m = re.match(r"data:audio/(mpeg|mp3|wav|x-wav|ogg|aac|m4a|mp4);base64,", src)
        if not m:
            paths.append(None)
            continue
        ext = {"mpeg": "mp3", "x-wav": "wav", "mp4": "m4a"}.get(m.group(1), m.group(1))
        try:
            raw = base64.b64decode(src[m.end():], validate=False)
        except Exception:
            paths.append(None)
            continue
        cid = re.sub(r"[^\w\-]", "", str(c.get("id", "")))[:40] or f"au{i}"
        p = assets_dir / f"{cid}.{ext}"
        p.write_bytes(raw)
        paths.append(p)
    return paths


def _build_music_filter_lines(
    music_meta: List[Dict[str, Any]],
    base_tag: str,
    total_dur: float,
) -> Dict[str, Any]:
    """
    P5: sinh filter trộn các clip nhạc/SFX lên audio nền (hàm thuần để unit test).

    music_meta: [{a_idx (index input ffmpeg), at_out, dur_out, volume, fadeIn, fadeOut,
                  muted, duck}] — at_out/dur_out đã map sang trục file xuất.
    base_tag: tag audio nền (vd '[basea]') — audio đã mix sẵn trong video (giọng AI + BGM).
    Ducking: sidechaincompress nén nhạc theo mức âm của audio nền (giọng nói).
    Trả {lines, used_split} — lines kết thúc bằng [outa], độ dài bung đủ total_dur.
    """
    lines: List[str] = []
    active = [m for m in music_meta if not m.get("muted")]
    ducked = [m for m in active if m.get("duck")]
    if not active:
        # tất cả bị tắt tiếng → giữ nguyên audio nền để không mất [outa]
        lines.append(f"{base_tag}anull[outa]")
        return {"lines": lines, "used_split": False}

    used_split = bool(ducked)
    mix_inputs: List[str] = []
    if used_split:
        split_tags = "".join(f"[bs{j}]" for j in range(len(ducked) + 1))
        lines.append(f"{base_tag}asplit={len(ducked) + 1}{split_tags}")
        base_mix_tag = "[bs0]"
    else:
        base_mix_tag = base_tag
    mix_inputs.append(base_mix_tag)

    duck_i = 0
    for k, m in enumerate(active):
        chain = ["aformat=sample_rates=44100:channel_layouts=stereo"]
        chain.append(f"atrim=start=0:end={m['dur_out']:.3f}")
        if abs(float(m.get("volume", 1.0)) - 1.0) > 1e-6:
            chain.append(f"volume={float(m.get('volume', 1.0)):.5f}")
        fade_in = float(m.get("fade_in", 0.0) or 0.0)
        fade_out = float(m.get("fade_out", 0.0) or 0.0)
        if fade_in > 0:
            chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            chain.append(f"afade=t=out:st={max(0.0, m['dur_out'] - fade_out):.3f}:d={fade_out:.3f}")
        chain.append(f"adelay={int(round(float(m.get('at_out', 0.0)) * 1000))}:all=1")
        src = f"[{int(m.get('a_idx', 0))}:a]"
        if m.get("duck"):
            out_tag = f"[amd{k}]"
            lines.append(f"{src}{','.join(chain)}[amraw{k}]")
            lines.append(
                f"[amraw{k}][bs{duck_i + 1}]sidechaincompress=threshold=0.03:ratio=6:attack=80:release=500{out_tag}"
            )
            duck_i += 1
        else:
            out_tag = f"[am{k}]"
            lines.append(f"{src}{','.join(chain)}{out_tag}")
        mix_inputs.append(out_tag)

    lines.append(
        f"{''.join(mix_inputs)}amix=inputs={len(mix_inputs)}:normalize=0:duration=longest[mxout]"
    )
    lines.append(f"[mxout]apad=whole_dur={max(0.1, float(total_dur or 0.0)):.3f}[outa]")
    return {"lines": lines, "used_split": used_split}


def _retime_subtitles_to_timeline(
    trans_subs: List[Dict[str, Any]],
    orig_subs: List[Dict[str, Any]],
    clips: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Map phụ đề nguồn sang trục timeline đã ghép, tính cả speed/freeze (P2) và
    transition (P4 — transition rút ngắn output nên vị trí các đoạn sau dời về trái).
    Trả về danh sách {index, start, end, start_str, end_str, text, original_text}.
    """
    updated: List[Dict[str, Any]] = []
    accum = 0.0
    new_idx = 1
    n_clips = len(clips)
    for idx_c, c in enumerate(clips):
        c_st = float(c.get("start", 0.0))
        c_en = float(c.get("end", c_st + 1.0))
        eff = _timeline_clip_effects(c)
        clip_tl_dur = max(0.01, (c_en - c_st) / eff["speed"] + eff["freeze"])
        base_out = accum

        for idx_s, s in enumerate(trans_subs):
            s_st = float(s.get("start", 0.0))
            s_en = float(s.get("end", s_st + 1.0))
            ov_st = max(s_st, c_st)
            ov_en = min(s_en, c_en)
            if ov_en > ov_st + 0.2:
                rel_st = base_out + eff["freeze"] + (ov_st - c_st) / eff["speed"]
                rel_en = base_out + eff["freeze"] + (ov_en - c_st) / eff["speed"]
                txt = (s.get("text") or "").strip()
                orig_txt = orig_subs[idx_s].get("text", "") if idx_s < len(orig_subs) else ""
                updated.append({
                    "index": new_idx,
                    "start": round(rel_st, 3),
                    "end": round(rel_en, 3),
                    "start_str": format_srt_timestamp(rel_st),
                    "end_str": format_srt_timestamp(rel_en),
                    "text": txt,
                    "original_text": orig_txt,
                })
                new_idx += 1
        accum += clip_tl_dur
        traw = c.get("trans") if isinstance(c.get("trans"), dict) else None
        if idx_c < n_clips - 1 and traw:
            tdur = max(0.0, min(1.0, float(traw.get("dur", 0.5) or 0.5)))
            if tdur >= 0.05:
                accum -= min(tdur, 0.9 * min(clip_tl_dur, 999.0))
    return updated


def _run_ffmpeg_ctl(cmd: List[str], job_ctl: Optional[Dict[str, Any]] = None, timeout: int = 900):
    """
    P6.3: chạy ffmpeg với hỗ trợ tiến độ + huỷ job.
    job_ctl: {"cancelled": bool, "proc": Popen|None, "progress": fn(0..1)|None, "total_dur": float}
    Không có job_ctl → hành vi cũ (subprocess.run).
    Huỷ: endpoint đặt cancelled=True + kill proc → readline EOF → trả CompletedProcess rc=99.
    """
    if not job_ctl:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    job_ctl["proc"] = proc
    err_tail: List[str] = []
    time_re = re.compile(r"time=\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
    total = float(job_ctl.get("total_dur") or 0.0)
    cb = job_ctl.get("progress")
    try:
        for line in proc.stderr:
            err_tail.append(line)
            if len(err_tail) > 200:
                err_tail.pop(0)
            if cb and total > 0:
                m = time_re.search(line)
                if m:
                    secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    try:
                        cb(min(1.0, secs / total))
                    except Exception:
                        pass
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
    rc = 99 if job_ctl.get("cancelled") else proc.returncode
    return subprocess.CompletedProcess(cmd, rc, "", "".join(err_tail))


def _apply_timeline_edits_file(
    video_path: Path,
    clips: List[Dict[str, Any]],
    out_path: Path,
    text_clips: Optional[List[Dict[str, Any]]] = None,
    overlay_clips: Optional[List[Dict[str, Any]]] = None,
    audio_clips: Optional[List[Dict[str, Any]]] = None,
    workspace_dir: Optional[Path] = None,
    job_ctl: Optional[Dict[str, Any]] = None,
    export_frame: Optional[Dict[str, int]] = None,
) -> bool:
    """
    Cắt, ghép nối và nhân bản các đoạn video theo danh sách clips:
    [{'start': 0.0, 'end': 5.2, 'at': 0.0, 'speed': 1.0, ...}, ...]
    Hỗ trợ kéo thả đổi thứ tự, nhân bản từng đoạn hoặc nhân bản toàn bộ video.
    P2: speed/volume/fade/crop/rotate/flip/reverse/freeze/detachAudio.
    P3: text_clips (burn ASS) + overlay_clips (overlay filter ảnh) đặt đúng vị trí file xuất
        (at/dur trên timeline có gap được map qua _timeline_to_output_t).
    P5: audio_clips (nhạc nền/SFX dataURL) trộn lên audio sẵn có, có volume/fade/ducking.
    P6.3: job_ctl ({"cancelled", "proc", "progress", "total_dur"}) — render nền có tiến độ + huỷ.
    P6.2: export_frame {"w","h"} — scale/pad khung xuất (9:16, 1:1, 16:9).
    Sử dụng GPU NVENC (fallback CPU libx264) đảm bảo âm thanh và hình ảnh đồng bộ 100%.
    """
    if not video_path.is_file() or not clips:
        return False

    cmd_probe = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,width,height,r_frame_rate,sample_rate,channel_layout,channels",
        "-of", "json", str(video_path)
    ]
    p_res = subprocess.run(cmd_probe, capture_output=True, text=True, encoding="utf-8", errors="replace")
    streams = []
    try:
        streams = json.loads(p_res.stdout).get("streams", [])
    except Exception:
        pass

    v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    a_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    src_w = int(v_stream.get("width") or 0)
    src_h = int(v_stream.get("height") or 0)

    # Lọc danh sách clips hợp lệ (giữ nguyên thuộc tính hiệu ứng P2/P3/P4/P5 client gửi kèm)
    _effect_keys = ("at", "speed", "volume", "muted", "keepPitch", "fadeIn", "fadeOut",
                    "crop", "rotate", "flipH", "flipV", "reverse", "freeze", "detachAudio",
                    "trans", "fx")
    valid_clips = []
    for c in clips:
        try:
            st = max(0.0, float(c.get("start", 0.0)))
            en = max(st + 0.05, float(c.get("end", st + 1.0)))
            entry = {"start": st, "end": en}
            for k in _effect_keys:
                if k in c:
                    entry[k] = c[k]
            valid_clips.append(entry)
        except Exception:
            pass

    if not valid_clips:
        return False

    graph = _build_timeline_filter_graph(valid_clips, bool(a_stream), src_w, src_h)
    if not graph or not graph.get("filter_lines"):
        return False

    audio_maps = ["-map", "[outa]", "-c:a", "aac", "-b:a", "192k"] if a_stream else []
    base_dir = Path(workspace_dir) if workspace_dir else out_path.parent

    # ---- P3/P5: hậu xử lý video (text ASS + overlay ảnh) và audio (nhạc/SFX) ----
    text_clips = [c for c in (text_clips or []) if isinstance(c, dict)]
    overlay_clips = [c for c in (overlay_clips or []) if isinstance(c, dict)]
    audio_clips = [c for c in (audio_clips or []) if isinstance(c, dict)]
    overlay_inputs: List[str] = []
    music_inputs: List[str] = []
    needs_video_post = bool(text_clips or overlay_clips)
    needs_audio_post = bool(audio_clips) and bool(a_stream)

    if needs_video_post or needs_audio_post:
        # đổi tag đầu ra của graph thành [basev]/[basea] rồi xâu chuỗi hậu xử lý
        # (quét toàn bộ vì mode "mix" của P2 có [outv] và [outa] ở hai dòng khác nhau)
        for i, ln in enumerate(graph["filter_lines"]):
            if "[outv]" in ln:
                graph["filter_lines"][i] = ln.replace("[outv]", "[basev]", 1)
                break
        if needs_audio_post:
            for i, ln in enumerate(graph["filter_lines"]):
                if "[outa]" in ln:
                    graph["filter_lines"][i] = ln.replace("[outa]", "[basea]", 1)
                    break

    if needs_video_post:
        cur = "[basev]"
        if overlay_clips:
            asset_paths = _decode_overlay_assets(overlay_clips, base_dir)
            for i, (oc, asset) in enumerate(zip(overlay_clips, asset_paths)):
                if not asset:
                    logger.warning(f"Overlay clip #{i}: ảnh hỏng/không hỗ trợ — bỏ qua")
                    continue
                at_tl = max(0.0, float(oc.get("at", 0.0) or 0.0))
                dur_tl = max(0.1, float(oc.get("dur", 3.0) or 3.0))
                at_out = _timeline_to_output_t(valid_clips, graph, at_tl)
                end_out = _timeline_to_output_t(valid_clips, graph, at_tl + dur_tl)
                pos = oc.get("pos") if isinstance(oc.get("pos"), dict) else {}
                px = min(1.0, max(0.0, float(pos.get("x", 0.5))))
                py = min(1.0, max(0.0, float(pos.get("y", 0.85))))
                scale = min(3.0, max(0.05, float(oc.get("scale", 1.0) or 1.0)))
                opacity = min(1.0, max(0.0, float(oc.get("opacity", 1.0) or 1.0)))
                img_idx = 1 + len(overlay_inputs)
                overlay_inputs.append(str(asset))
                pre = ["format=rgba"]
                if opacity < 0.999:
                    pre.append(f"colorchannelmixer=aa={opacity:.3f}")
                if abs(scale - 1.0) > 1e-3:
                    pre.append(f"scale='max(2,trunc(iw*{scale:.4f}/2)*2)':-2")
                ov_tag = f"vo{i}"
                graph["filter_lines"].append(f"[{img_idx}:v]{','.join(pre)}[ovi{i}]")
                graph["filter_lines"].append(
                    f"{cur}[ovi{i}]overlay=x='main_w*{px:.4f}-w/2':y='main_h*{py:.4f}-h/2'"
                    f":enable='between(t,{at_out:.3f},{end_out:.3f})'[{ov_tag}]"
                )
                cur = f"[{ov_tag}]"

        if text_clips:
            mapped = []
            for tc in text_clips:
                at_tl = max(0.0, float(tc.get("at", 0.0) or 0.0))
                dur_tl = max(0.1, float(tc.get("dur", 3.0) or 3.0))
                at_out = _timeline_to_output_t(valid_clips, graph, at_tl)
                end_out = _timeline_to_output_t(valid_clips, graph, at_tl + dur_tl)
                mapped.append({**tc, "at": at_out, "dur": max(0.1, end_out - at_out)})
            ass_path = base_dir / "text_clips.ass"
            ass_path.write_text(_build_text_ass_clips(mapped, src_w or 1080, src_h or 1920), encoding="utf-8")
            try:
                ass_ref = os.path.relpath(str(ass_path), os.getcwd()).replace("\\", "/")
            except ValueError:
                ass_ref = str(ass_path).replace("\\", "/").replace(":", "\\:")
            graph["filter_lines"].append(f"{cur}subtitles='{ass_ref}'[outv]")

    if needs_audio_post:
        # P5: nhạc/SFX trộn lên audio nền có sẵn; vị trí map qua timeline→output (gap-aware)
        music_meta = []
        asset_paths = _decode_audio_assets(audio_clips, base_dir)
        for i, (ac, asset) in enumerate(zip(audio_clips, asset_paths)):
            if not asset:
                logger.warning(f"Audio clip #{i}: file nhạc hỏng/không hỗ trợ — bỏ qua")
                continue
            at_tl = max(0.0, float(ac.get("at", 0.0) or 0.0))
            dur_tl = max(0.1, float(ac.get("dur", 3.0) or 3.0))
            at_out = _timeline_to_output_t(valid_clips, graph, at_tl)
            end_out = _timeline_to_output_t(valid_clips, graph, at_tl + dur_tl)
            music_inputs.append(str(asset))
            music_meta.append({
                "a_idx": 1 + len(overlay_inputs) + len(music_inputs) - 1,
                "at_out": at_out,
                "dur_out": max(0.1, end_out - at_out),
                "volume": min(2.0, max(0.0, float(ac.get("volume", 1.0) or 1.0))),
                "fade_in": max(0.0, float(ac.get("fadeIn", 0.0) or 0.0)),
                "fade_out": max(0.0, float(ac.get("fadeOut", 0.0) or 0.0)),
                "muted": bool(ac.get("muted")),
                "duck": bool(ac.get("duck")),
            })
        if music_meta:
            music_res = _build_music_filter_lines(music_meta, "[basea]", graph.get("total_dur", 0.0))
            graph["filter_lines"].extend(music_res["lines"])
        else:
            graph["filter_lines"].append("[basea]anull[outa]")

    # Repair: nếu [outv] bị đổi thành [basev] mà chuỗi hậu xử lý không tái tạo [outv]
    # (ví dụ chỉ có nhạc/SFX, không có text/overlay) → nối lại qua null
    if (needs_video_post or needs_audio_post) and "[outv]" not in " ".join(graph["filter_lines"]):
        graph["filter_lines"].append("[basev]null[outv]")

    # P6.2: preset khung xuất — scale/pad về 9:16 / 1:1 / 16:9 nếu khác kích thước nguồn
    if export_frame:
        try:
            ew = int(export_frame.get("w") or 0)
            eh = int(export_frame.get("h") or 0)
        except Exception:
            ew = eh = 0
        if ew >= 64 and eh >= 64 and ew <= 4096 and eh <= 4096 and (ew != src_w or eh != src_h):
            for i in range(len(graph["filter_lines"]) - 1, -1, -1):
                if "[outv]" in graph["filter_lines"][i]:
                    graph["filter_lines"][i] = graph["filter_lines"][i].replace("[outv]", "[preexp]", 1)
                    break
            graph["filter_lines"].append(
                f"[preexp]scale={ew}:{eh}:force_original_aspect_ratio=decrease,"
                f"pad={ew}:{eh}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p[outv]"
            )

    filter_complex = ";".join(graph["filter_lines"])
    temp_out = out_path.parent / f"_temp_edit_{out_path.name}"
    temp_out.unlink(missing_ok=True)

    cmd_nvenc = [
        "ffmpeg", "-y",
        "-i", str(video_path),
    ] + [x for p in (overlay_inputs + music_inputs) for x in ("-i", p)] + [
        "-filter_complex", filter_complex,
        "-map", "[outv]"
    ] + audio_maps + [
        "-c:v", "h264_nvenc", "-preset", "p4", "-b:v", "4500k",
        "-movflags", "+faststart",
        str(temp_out)
    ]

    success = False
    if job_ctl is not None:
        job_ctl.setdefault("total_dur", graph.get("total_dur") or 0.0)
        if job_ctl.get("cancelled"):
            return False
    try:
        res = _run_ffmpeg_ctl(cmd_nvenc, job_ctl, timeout=600)
        if res.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 1000:
            success = True
    except Exception as e:
        logger.warning(f"Timeline edit NVENC failed: {e}")

    if not success:
        if job_ctl is not None and job_ctl.get("cancelled"):
            return False
        cmd_cpu = [
            "ffmpeg", "-y",
            "-i", str(video_path),
        ] + [x for p in (overlay_inputs + music_inputs) for x in ("-i", p)] + [
            "-filter_complex", filter_complex,
            "-map", "[outv]"
        ] + audio_maps + [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-movflags", "+faststart",
            str(temp_out)
        ]
        try:
            res2 = _run_ffmpeg_ctl(cmd_cpu, job_ctl, timeout=600)
        except Exception as e:
            logger.warning(f"Timeline edit CPU failed: {e}")
            res2 = subprocess.CompletedProcess(cmd_cpu, 1, "", "")
        if res2.returncode == 0 and temp_out.is_file() and temp_out.stat().st_size > 1000:
            success = True

    if success:
        try:
            out_path.unlink(missing_ok=True)
            temp_out.rename(out_path)
            return True
        except Exception as e:
            logger.error(f"Error renaming edited video file: {e}")
            return False
    return False


@router.post("/api/workflow/timeline/apply-edits")
async def api_apply_timeline_edits(payload: Dict[str, Any] = Body(...)):
    """Render đồng bộ (giữ tương thích client cũ; client mới dùng /apply-edits-async P6.3)."""
    return _execute_apply_edits(payload)


def _execute_apply_edits(payload: Dict[str, Any], job_ctl: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Thân dùng chung cho render đồng bộ và render nền P6.3.
    Payload:
      - stem: str
      - clips: List[Dict] — mỗi clip:
          {start, end, speed (0.25–4x), volume (0–2), muted, keepPitch, fadeIn, fadeOut,
           crop {x,y,w,h} chuẩn hóa, rotate (0/90/180/270), flipH, flipV, reverse,
           freeze (giây đóng băng đầu đoạn), detachAudio}
      - target_version: Optional[str] ('final' hoặc 'raw')
      - text_clips: Optional[List[Dict]] — P3: {id, text, at, dur, pos {x,y}, style {...}, animIn, animOut}
        (at/dur trên timeline; backend tự map về vị trí file xuất và burn bằng ASS)
      - overlay_clips: Optional[List[Dict]] — P3: {id, src dataURL ảnh, at, dur, pos, scale, opacity}
      - audio_clips: Optional[List[Dict]] — P5: {id, src dataURL nhạc, at, dur, volume, fadeIn,
        fadeOut, muted, duck} — trộn lên audio nền có sẵn (giọng AI + BGM)
    job_ctl: P6.3 — {"cancelled", "proc", "progress", "stage_cb", "total_dur"}
    """
    def _prog(pct: int, msg: str) -> None:
        if job_ctl:
            cb = job_ctl.get("stage_cb")
            if cb:
                try:
                    cb(pct, msg)
                except Exception:
                    pass

    stem = payload.get("stem", "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)

    clips = payload.get("clips", [])
    if not clips or not isinstance(clips, list):
        raise HTTPException(status_code=400, detail="Danh sách phân đoạn clip không hợp lệ")

    job_dir = WORKSPACE / clean_stem
    cand_phoi = INPUT_DIR / f"{stem}.mp4"
    cand_dl = DOWNLOADS_DIR / f"{stem}.mp4"

    source_video = None
    if cand_phoi.is_file():
        source_video = cand_phoi
    elif cand_dl.is_file():
        source_video = cand_dl

    final_video = None
    if job_dir.is_dir():
        for item in job_dir.iterdir():
            if item.name == f"final_{stem}.mp4" and _valid_final_video(item):
                final_video = item
                break

    target_version = payload.get("target_version", "final")
    input_video = (final_video if (target_version == "final" and final_video) else (source_video or final_video))
    if not input_video or not input_video.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp video để chỉnh sửa")

    job_dir.mkdir(parents=True, exist_ok=True)
    dest_final = job_dir / f"final_{stem}.mp4"
    _prog(10, "Đã tìm thấy video nguồn")

    text_clips_in = payload.get("text_clips") if isinstance(payload.get("text_clips"), list) else []
    overlay_clips_in = payload.get("overlay_clips") if isinstance(payload.get("overlay_clips"), list) else []
    audio_clips_in = payload.get("audio_clips") if isinstance(payload.get("audio_clips"), list) else []
    export_frame_in = payload.get("export_frame") if isinstance(payload.get("export_frame"), dict) else None
    _prog(20, "Bắt đầu render FFmpeg (GPU)...")
    ok = _apply_timeline_edits_file(
        input_video, clips, dest_final,
        text_clips=text_clips_in,
        overlay_clips=overlay_clips_in,
        audio_clips=audio_clips_in,
        workspace_dir=job_dir,
        job_ctl=job_ctl,
        export_frame=export_frame_in,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Lỗi khi cắt ghép video bằng FFmpeg")
    if job_ctl and job_ctl.get("cancelled"):
        raise HTTPException(status_code=499, detail="Đã huỷ render")
    _prog(85, "Đã render xong — căn lại phụ đề...")

    # Tự động đồng bộ và căn chỉnh lại tệp phụ đề translated.srt theo timeline clips mới
    # (P2: _retime_subtitles_to_timeline tính cả speed/freeze của từng clip)
    updated_subs = []
    trans_srt_file = job_dir / "translated.srt"
    orig_srt_file = job_dir / "original.srt"
    if trans_srt_file.is_file():
        try:
            old_trans_subs = parse_srt(trans_srt_file.read_text(encoding="utf-8", errors="replace"))
            old_orig_subs = parse_srt(orig_srt_file.read_text(encoding="utf-8", errors="replace")) if orig_srt_file.is_file() else []

            updated_subs = _retime_subtitles_to_timeline(old_trans_subs, old_orig_subs, clips)
            new_trans_srt_blocks = []
            new_orig_srt_blocks = []
            for u in updated_subs:
                new_trans_srt_blocks.append(f"{u['index']}\n{u['start_str']} --> {u['end_str']}\n{u['text']}\n")
                if u["original_text"]:
                    new_orig_srt_blocks.append(f"{u['index']}\n{u['start_str']} --> {u['end_str']}\n{u['original_text']}\n")

            if new_trans_srt_blocks:
                trans_srt_file.write_text("\n".join(new_trans_srt_blocks), encoding="utf-8")
            if new_orig_srt_blocks and orig_srt_file.is_file():
                orig_srt_file.write_text("\n".join(new_orig_srt_blocks), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error re-timing subtitles on timeline edit: {e}")
    _prog(93, "Đang cập nhật thumbnail...")

    # Tự động cập nhật thumbnail
    thumb_file = THUMBNAILS_DIR / f"{clean_stem}.jpg"
    try:
        cmd_th = [
            "ffmpeg", "-y", "-ss", "0.8",
            "-i", str(dest_final),
            "-frames:v", "1", "-q:v", "2",
            str(thumb_file)
        ]
        subprocess.run(cmd_th, capture_output=True, timeout=15)
        if job_dir.is_dir() and thumb_file.is_file():
            shutil.copy2(thumb_file, job_dir / "thumbnail.jpg")
    except Exception:
        pass

    stream_url = f"/api/workflow/stream-file?path={dest_final}&t={time.time()}"
    return {
        "status": "ok",
        "success": True,
        "message": f"Đã cắt và ghép {len(clips)} phân đoạn video chuẩn CapCut thành công!",
        "video_url": stream_url,
        "final_video_url": stream_url,
        "has_final": True,
        "total_clips": len(clips),
        "subtitles": updated_subs
    }


# =========================================================================
# FEATURE: CAPCUT-STYLE CUSTOM EXPORT & SRT DOWNLOAD
# =========================================================================

@router.get("/api/workflow/timeline/project")
async def api_get_timeline_project(stem: str = Query(...)):
    """
    Đọc project JSON đã autosave của một video (P6.1).
    Trả {exists: False} nếu chưa có hoặc file hỏng — client tự dùng timeline mặc định.
    """
    clean_stem = re.sub(r'[^\w\-_.]', '_', (stem or "").strip())
    if not clean_stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    project_file = WORKSPACE / clean_stem / "project.json"
    if not project_file.is_file():
        return {"exists": False}
    try:
        data = json.loads(project_file.read_text(encoding="utf-8"))
    except Exception:
        logger.warning(f"project.json của {clean_stem} hỏng JSON — bỏ qua khôi phục")
        return {"exists": False}
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list) or not data["tracks"]:
        return {"exists": False}
    return {"exists": True, "project": data, "saved_at": data.get("saved_at")}


@router.post("/api/workflow/timeline/project")
async def api_save_timeline_project(payload: Dict[str, Any] = Body(...)):
    """
    Lưu project JSON timeline cho một video (P6.1 autosave, khôi phục sau crash).
    Ghi atomic (tmp + replace) để không bao giờ để lại project.json dở dang.
    """
    stem = (payload.get("stem") or "").strip()
    project = payload.get("project")
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    if not isinstance(project, dict) or not isinstance(project.get("tracks"), list) or not project["tracks"]:
        raise HTTPException(status_code=400, detail="Project không hợp lệ (thiếu tracks)")

    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    job_dir = WORKSPACE / clean_stem
    job_dir.mkdir(parents=True, exist_ok=True)

    project = {**project, "stem": clean_stem, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    target = job_dir / "project.json"
    tmp = job_dir / "project.json.tmp"
    tmp.write_text(json.dumps(project, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return {"status": "ok", "saved": True, "path": str(target), "saved_at": project["saved_at"]}


@router.get("/api/workflow/audio/waveform")
async def api_audio_waveform(stem: str = Query(...), version: str = Query("final"), buckets: int = Query(1600)):
    """
    P5: dải biên độ (waveform peaks) của audio video cho track timeline.
    Bóc PCM 8kHz mono bằng ffmpeg, lấy max theo bucket, cache tại
    workspace/<stem>/waveform_<version>.json (theo mtime + size file nguồn).
    """
    clean_stem = re.sub(r'[^\w\-_.]', '_', (stem or "").strip())
    if not clean_stem:
        raise HTTPException(status_code=400, detail="Thiếu stem")
    n_buckets = max(200, min(4000, int(buckets or 1600)))

    job_dir = WORKSPACE / clean_stem
    src_file = None
    if version != "raw":
        cand_final = job_dir / f"final_{stem}.mp4"
        if cand_final.is_file() and _valid_final_video(cand_final):
            src_file = cand_final
    if src_file is None:
        for cand in (INPUT_DIR / f"{stem}.mp4", DOWNLOADS_DIR / f"{stem}.mp4"):
            if cand.is_file():
                src_file = cand
                break
    if src_file is None:
        return {"exists": False}

    cache_file = job_dir / f"waveform_{version}.json"
    st = src_file.stat()
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if (cached.get("mtime") == st.st_mtime and cached.get("size") == st.st_size
                    and cached.get("buckets") == n_buckets and isinstance(cached.get("peaks"), list)):
                return {"exists": True, "cached": True, "duration": cached.get("duration"),
                        "peaks": cached.get("peaks"), "buckets": n_buckets}
        except Exception:
            pass

    cmd = ["ffmpeg", "-v", "error", "-i", str(src_file), "-map", "0:a:0",
           "-ac", "1", "-ar", "8000", "-f", "s16le", "-"]
    try:
        res = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        logger.warning(f"Waveform: ffmpeg fail cho {clean_stem}: {e}")
        return {"exists": False}
    if res.returncode != 0 or not res.stdout:
        return {"exists": False}

    samples = np.frombuffer(res.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    duration = len(samples) / 8000.0
    if duration <= 0:
        return {"exists": False}
    n = min(n_buckets, max(1, int(duration * 20)))
    pad = (-len(samples)) % n
    if pad:
        samples = np.concatenate([samples, np.zeros(pad, dtype=np.float32)])
    peaks = np.abs(samples.reshape(n, -1)).max(axis=1)
    peaks = np.maximum(peaks, 1e-6)
    mx = float(peaks.max())
    peaks = (peaks / (mx if mx > 0 else 1.0)).round(4).tolist()

    payload = {"mtime": st.st_mtime, "size": st.st_size, "buckets": n,
               "duration": round(duration, 3), "peaks": peaks}
    try:
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
    return {"exists": True, "cached": False, "duration": round(duration, 3), "peaks": peaks, "buckets": n}


# =========================================================================
# P6.3: RENDER NỀN CHO APPLY-EDITS (job + tiến độ + huỷ + hàng đợi GPU 1 luồng)
# =========================================================================
_TIMELINE_RENDER_JOBS: Dict[str, Dict[str, Any]] = {}
_TIMELINE_RENDER_LOCK = threading.Lock()
_TIMELINE_RENDER_SEM = threading.Semaphore(1)  # GPU RTX 4050 chỉ render 1 job cùng lúc


def _timeline_render_worker(job_id: str, payload: Dict[str, Any]) -> None:
    job = _TIMELINE_RENDER_JOBS.get(job_id)
    if job is None:
        return
    job_ctl: Dict[str, Any] = {
        "cancelled": False,
        "proc": None,
        "total_dur": 0.0,
        "progress": lambda frac: job.update({
            "progress": 20 + int(max(0.0, min(1.0, frac)) * 60),
            "stage": "Đang render FFmpeg (GPU)...",
        }),
        "stage_cb": lambda pct, msg: job.update({"progress": int(pct), "stage": msg}),
    }
    job["job_ctl"] = job_ctl
    job.update({"status": "running", "progress": 5, "stage": "Chuẩn bị..."})
    with _TIMELINE_RENDER_SEM:
        if job_ctl["cancelled"]:
            job.update({"status": "cancelled", "stage": "Đã huỷ", "progress": 0})
            return
        try:
            result = _execute_apply_edits(payload, job_ctl=job_ctl)
            job.update({"status": "done", "progress": 100, "stage": "Hoàn tất", "result": result})
        except HTTPException as e:
            status = "cancelled" if job_ctl.get("cancelled") else "error"
            job.update({"status": status, "stage": str(e.detail)[:200], "error": str(e.detail)})
        except Exception as e:
            logger.exception("Timeline render job lỗi")
            job.update({"status": "error", "stage": str(e)[:200], "error": str(e)})


@router.post("/api/workflow/timeline/apply-edits-async")
async def api_apply_timeline_edits_async(payload: Dict[str, Any] = Body(...)):
    """P6.3: tạo job render nền (hàng đợi GPU 1 luồng), trả job_id ngay để client poll tiến độ."""
    stem = (payload.get("stem") or "").strip()
    if not stem or not isinstance(payload.get("clips"), list) or not payload.get("clips"):
        raise HTTPException(status_code=400, detail="Payload render nền không hợp lệ (thiếu stem/clips)")
    import uuid as _uuid

    job_id = _uuid.uuid4().hex[:12]
    job: Dict[str, Any] = {"status": "queued", "progress": 0, "stage": "Trong hàng đợi GPU...", "created_at": time.time()}
    with _TIMELINE_RENDER_LOCK:
        # dọn job cũ hơn 30 phút
        for k in [k for k, v in _TIMELINE_RENDER_JOBS.items() if time.time() - v.get("created_at", 0) > 1800]:
            _TIMELINE_RENDER_JOBS.pop(k, None)
        _TIMELINE_RENDER_JOBS[job_id] = job
    threading.Thread(target=_timeline_render_worker, args=(job_id, payload), daemon=True).start()
    return {"status": "ok", "job_id": job_id}


@router.get("/api/workflow/timeline/render-status")
async def api_timeline_render_status(job_id: str = Query(...)):
    job = _TIMELINE_RENDER_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job render")
    return {
        "status": job.get("status"),
        "progress": job.get("progress", 0),
        "stage": job.get("stage", ""),
        "result": job.get("result"),
    }


@router.post("/api/workflow/timeline/render-cancel")
async def api_timeline_render_cancel(payload: Dict[str, Any] = Body(...)):
    """P6.3: huỷ job render — kill tiến trình ffmpeg đang chạy."""
    job_id = str((payload or {}).get("job_id") or "")
    job = _TIMELINE_RENDER_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job render")
    ctl = job.get("job_ctl") or {}
    ctl["cancelled"] = True
    proc = ctl.get("proc")
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    if job.get("status") in ("queued", "running"):
        job.update({"status": "cancelled", "stage": "Đã huỷ bởi người dùng", "progress": 0})
    return {"status": "ok", "cancelled": True}


# =========================================================================
# P7: TÍCH HỢP CONCAT (jub0t/concat — AGPL-3.0, CHỈ gọi như tiến trình ngoài)
# =========================================================================

CONCAT_EXE_CANDIDATES = [
    Path(r"C:\tool v1\bin\concat\app\PFiles64\Concat\concat.exe"),
]


def _find_concat_exe() -> Optional[Path]:
    for p in CONCAT_EXE_CANDIDATES:
        if p.is_file():
            return p
    return None


@router.post("/api/workflow/concat/open")
async def api_concat_open(payload: Dict[str, Any] = Body(...)):
    """
    P7: mở Concat với thư mục project của video (tiến trình ngoài, detached).
    AGPL: chỉ thực thi nhị phân, không nhúng/sao chép mã nguồn Concat.
    """
    stem = re.sub(r'[^\w\-_.]', '_', (payload.get("stem") or "").strip())
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")
    exe = _find_concat_exe()
    if not exe:
        raise HTTPException(status_code=404, detail="Không tìm thấy concat.exe (bin/concat/app/PFiles64/Concat/concat.exe)")
    job_dir = WORKSPACE / stem
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [str(exe), "--project", str(job_dir)],
            cwd=str(job_dir),
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Không mở được Concat: {e}")
    return {"status": "ok", "pid": proc.pid, "project_dir": str(job_dir)}


@router.post("/api/workflow/concat/import")
async def api_concat_import(stem: str = Form(...), file: UploadFile = File(...)):
    """
    P7: nhập file video xuất từ Concat vào THƯ VIỆN (INPUT_DIR) như một video phôi mới,
    để chạy lại pipeline / chỉnh sửa tiếp trong Studio.
    """
    stem_clean = re.sub(r'[^\w\-_.]', '_', (stem or "").strip())
    if not stem_clean:
        raise HTTPException(status_code=400, detail="Thiếu stem")
    data = await file.read()
    if len(data) < 100_000:
        raise HTTPException(status_code=400, detail="File quá nhỏ — có thể không phải video")
    if len(data) > 2 * 1024 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File quá lớn (>2GB)")
    if not (data[:12].find(b"ftyp") >= 4 or data[:4] == b"\x1a\x45\xdf\xa3" or data[:3] == b"ID3" or data[:2] == b"\x00\x00"):
        logger.warning(f"Concat import: file không nhận diện được header video quen thuộc — vẫn lưu: {file.filename}")
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_name = f"concat_{stem_clean[:40]}_{ts}.mp4"
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = INPUT_DIR / out_name
    out_path.write_bytes(data)
    return {"status": "ok", "filename": out_name, "size_mb": round(len(data) / 1048576, 2)}


# =========================================================================
# P6.2: PROXY 540p CHO PREVIEW MƯỢT + PRESET KHUNG XUẤT
# =========================================================================
_PROXY_GENERATING: set = set()


def _resolve_timeline_source_video(stem: str, version: str) -> Optional[Path]:
    """Chọn file nguồn theo phiên bản: final (mặc định) hoặc raw (phôi)."""
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
    job_dir = WORKSPACE / clean_stem
    if version != "raw":
        cand_final = job_dir / f"final_{stem}.mp4"
        if cand_final.is_file() and _valid_final_video(cand_final):
            return cand_final
    for cand in (INPUT_DIR / f"{stem}.mp4", DOWNLOADS_DIR / f"{stem}.mp4"):
        if cand.is_file():
            return cand
    return None


def _proxy_path_for(stem: str, version: str) -> Path:
    return WORKSPACE / re.sub(r'[^\w\-_.]', '_', stem) / f"proxy_{version}.mp4"


def _proxy_is_fresh(proxy_path: Path, src_path: Path) -> bool:
    return proxy_path.is_file() and proxy_path.stat().st_size > 1_000 and \
        proxy_path.stat().st_mtime >= src_path.stat().st_mtime - 1.0


def _proxy_generate_worker(src_path: Path, proxy_path: Path, key: str) -> None:
    try:
        # đuôi .mp4 để ffmpeg đo được muxer (file .mp4.tmp sẽ lỗi "Invalid argument")
        tmp = proxy_path.parent / ("tmp_" + proxy_path.name)
        tmp.unlink(missing_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src_path),
             "-vf", "scale='min(540,iw)':-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
             "-c:a", "copy", "-movflags", "+faststart", str(tmp)],
            capture_output=True, timeout=600,
        )
        if tmp.is_file() and tmp.stat().st_size > 1_000:
            proxy_path.unlink(missing_ok=True)
            tmp.rename(proxy_path)
        else:
            tmp.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"Proxy generate fail cho {proxy_path.name}: {e}")
    finally:
        _PROXY_GENERATING.discard(key)


@router.post("/api/workflow/proxy/generate")
async def api_proxy_generate(payload: Dict[str, Any] = Body(...)):
    """
    P6.2: tạo proxy 540p nền cho preview mượt (không chặn request).
    Trả {ready: true, url} nếu proxy đã có và mới hơn nguồn; ngược lại khởi tạo
    thread generate và trả {ready: false, started: true}.
    """
    stem = (payload.get("stem") or "").strip()
    version = payload.get("version") or "final"
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu stem")
    src = _resolve_timeline_source_video(stem, version)
    if not src:
        raise HTTPException(status_code=404, detail="Không tìm thấy video nguồn")
    proxy_path = _proxy_path_for(stem, version)
    url = f"/api/workflow/stream-file?path={proxy_path}&t={time.time()}"
    if _proxy_is_fresh(proxy_path, src):
        return {"ready": True, "url": url}
    key = f"{stem}|{version}"
    if key not in _PROXY_GENERATING:
        _PROXY_GENERATING.add(key)
        proxy_path.parent.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=_proxy_generate_worker, args=(src, proxy_path, key), daemon=True).start()
    return {"ready": False, "started": True, "url": url}


@router.get("/api/workflow/proxy/status")
async def api_proxy_status(stem: str = Query(...), version: str = Query("final")):
    src = _resolve_timeline_source_video(stem, version)
    if not src:
        return {"ready": False, "exists": False}
    proxy_path = _proxy_path_for(stem, version)
    if _proxy_is_fresh(proxy_path, src):
        return {"ready": True, "exists": True,
                "url": f"/api/workflow/stream-file?path={proxy_path}&t={time.time()}"}
    return {"ready": False, "exists": proxy_path.is_file(), "generating": f"{stem}|{version}" in _PROXY_GENERATING}


@router.get("/static/js/{fname}")
async def api_static_js(fname: str):
    """P6.4: phục vụ module JS tách từ workflow.html (chỉ .js, chặn traversal)."""
    if "/" in fname or "\\" in fname or ".." in fname or not fname.endswith(".js"):
        raise HTTPException(status_code=404, detail="File không hợp lệ")
    p = Path(__file__).resolve().parent / "static" / "js" / fname
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file JS")
    return FileResponse(p, media_type="application/javascript")


@router.get("/api/workflow/download-srt")
async def api_download_srt(stem: str = Query(...), type: str = Query("translated")):
    """
    Tải tệp phụ đề .SRT (tiếng Việt, tiếng Trung, hoặc song ngữ).
    """
    job_dir = WORKSPACE / stem
    if not job_dir.is_dir():
        raise HTTPException(status_code=404, detail="Không tìm thấy thư mục công việc của video")

    orig_srt_file = job_dir / "original.srt"
    trans_srt_file = job_dir / "translated.srt"

    if type == "chinese" or type == "original":
        if not orig_srt_file.is_file():
            raise HTTPException(status_code=404, detail="Chưa có tệp phụ đề tiếng Trung")
        content = orig_srt_file.read_text(encoding="utf-8", errors="replace")
        filename = f"{stem}_TiengTrung.srt"
    elif type == "bilingual":
        orig_subs = parse_srt(orig_srt_file.read_text(encoding="utf-8", errors="replace")) if orig_srt_file.is_file() else []
        trans_subs = parse_srt(trans_srt_file.read_text(encoding="utf-8", errors="replace")) if trans_srt_file.is_file() else []
        if not trans_subs and not orig_subs:
            raise HTTPException(status_code=404, detail="Chưa có phụ đề để tạo song ngữ")
        
        # Ghép song ngữ
        lines = []
        count = max(len(trans_subs), len(orig_subs))
        for idx in range(count):
            ts = trans_subs[idx] if idx < len(trans_subs) else None
            os_sub = orig_subs[idx] if idx < len(orig_subs) else None
            s_str = ts["start_str"] if ts else os_sub["start_str"]
            e_str = ts["end_str"] if ts else os_sub["end_str"]
            viet_txt = ts["text"] if ts else ""
            orig_txt = os_sub["text"] if os_sub else ""

            lines.append(str(idx + 1))
            lines.append(f"{s_str} --> {e_str}")
            if viet_txt:
                lines.append(viet_txt)
            if orig_txt:
                lines.append(orig_txt)
            lines.append("")
        content = "\n".join(lines)
        filename = f"{stem}_SongNgu.srt"
    else: # "translated" or "vietnamese"
        if not trans_srt_file.is_file():
            raise HTTPException(status_code=404, detail="Chưa có tệp phụ đề tiếng Việt")
        content = trans_srt_file.read_text(encoding="utf-8", errors="replace")
        filename = f"{stem}_TiengViet.srt"

    safe_fn = re.sub(r'[^a-zA-Z0-9_.-]', '_', filename)
    from urllib.parse import quote
    encoded_fn = quote(filename)
    from fastapi.responses import Response
    return Response(
        content=content.encode("utf-8-sig"),
        media_type="application/x-subrip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_fn}"; filename*=UTF-8''{encoded_fn}',
            "Access-Control-Expose-Headers": "Content-Disposition"
        }
    )


def format_srt_timestamp(seconds: float) -> str:
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        seconds += 1
        millis = 0
    s = int(seconds)
    hours = s // 3600
    minutes = (s % 3600) // 60
    secs = s % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


@router.post("/api/workflow/save-subtitles")
async def api_save_workflow_subtitles(payload: Dict[str, Any] = Body(...)):
    """Lưu phụ đề đã chỉnh sửa từ giao diện trực tiếp vào tệp translated.srt."""
    stem = payload.get("stem")
    subs = payload.get("subtitles", [])
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu stem video")
    job_dir = WORKSPACE / stem
    if not job_dir.is_dir():
        job_dir.mkdir(parents=True, exist_ok=True)

    srt_blocks = []
    for idx, s in enumerate(subs, 1):
        text = (s.get("text") or "").strip()
        start = float(s.get("start", 0))
        end = float(s.get("end", start + 2.0))
        srt_blocks.append(f"{idx}\n{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n{text}\n")

    trans_srt = job_dir / "translated.srt"
    trans_srt.write_text("\n".join(srt_blocks), encoding="utf-8")
    return {"status": "ok", "saved_count": len(subs)}


@router.post("/api/workflow/export-custom")
async def api_export_custom_video(payload: Dict[str, Any] = Body(...)):
    """
    Xuất video chuẩn CapCut: Tùy chọn độ phân giải, bitrate (kbps), FPS, định dạng,
    xuất kèm file .SRT và áp dụng tùy chọn chống re-up.
    """
    stem = payload.get("stem", "").strip()
    if not stem:
        raise HTTPException(status_code=400, detail="Thiếu mã nhận diện video (stem)")

    resolution = str(payload.get("resolution", "1080p")).lower()
    bitrate_kbps = int(payload.get("bitrate_kbps", 8000))
    fps = str(payload.get("fps", "30")).lower()
    export_format = str(payload.get("format", "mp4")).lower()
    export_srt = bool(payload.get("export_srt", True))
    srt_type = str(payload.get("srt_type", "translated")).lower()
    output_folder = payload.get("output_folder", "").strip()
    use_anti_reup = bool(payload.get("use_anti_reup", False))
    cfg = payload.get("canvas_settings") or get_canvas_settings()

    if not output_folder:
        output_folder = str(OUTPUT_DIR)
    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    job_dir = WORKSPACE / stem
    cand_phoi = INPUT_DIR / f"{stem}.mp4"
    cand_dl = DOWNLOADS_DIR / f"{stem}.mp4"

    # Tìm source video
    source_video = None
    if cand_phoi.is_file():
        source_video = cand_phoi
    elif cand_dl.is_file():
        source_video = cand_dl
    elif job_dir.is_dir() and (job_dir / "step1_extracted.mp4").is_file():
        source_video = job_dir / "step1_extracted.mp4"

    # Tìm final video (đã lồng tiếng & che sub)
    final_video = None
    if job_dir.is_dir():
        for item in job_dir.iterdir():
            if item.name == f"final_{stem}.mp4" and _valid_final_video(item):
                final_video = item
                break
    if not final_video and OUTPUT_DIR.exists():
        cand_banve = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
        if cand_banve.is_file() and _valid_final_video(cand_banve):
            final_video = cand_banve

    if not source_video and not final_video:
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp video nguồn")

    # Xác định kích thước đích (tw, th)
    aspect = str(cfg.get("aspect_ratio", "9:16")).lower()
    res_table = {
        "720p": {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (720, 720), "original": (720, 1280)},
        "1080p": {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "original": (1080, 1920)},
        "2k": {"9:16": (1440, 2560), "16:9": (2560, 1440), "1:1": (1440, 1440), "original": (1440, 2560)},
        "1440p": {"9:16": (1440, 2560), "16:9": (2560, 1440), "1:1": (1440, 1440), "original": (1440, 2560)},
        "4k": {"9:16": (2160, 3840), "16:9": (3840, 2160), "1:1": (2160, 2160), "original": (2160, 3840)}
    }
    ratio_key = aspect if aspect in ("9:16", "16:9", "1:1") else "9:16"
    tw, th = res_table.get(resolution, res_table["1080p"]).get(ratio_key, (1080, 1920))

    # Tên file xuất
    clean_stem = re.sub(r'[^\w\-_.]', '_', stem)[:40]
    out_filename = f"CapCut_{clean_stem}_{resolution}_{bitrate_kbps}k.{export_format}"
    out_path = out_dir / out_filename

    # Chuẩn bị file phụ đề nếu có
    srt_file = job_dir / "translated.srt" if (job_dir / "translated.srt").is_file() else None

    # Thiết lập bộ lọc FFmpeg
    extra_inputs = []
    video_scale = float(cfg.get("video_scale", 1.0))
    mirror = bool(cfg.get("mirror", False))
    bg_type = str(cfg.get("bg_type", "image")).lower()
    motion_bg_enabled = bool(cfg.get("motion_bg_enabled", False))
    if motion_bg_enabled:
        bg_type = "video_motion"
    elif bg_type == "video_motion" and not motion_bg_enabled:
        bg_type = "image"

    bg_motion_file = str(cfg.get("bg_motion_file", "song_bien.mp4"))
    bg_image_file = str(cfg.get("bg_image_file", "tia_sang_vang_ngoi_sao.jpg"))
    auto_crop_black_bars = bool(cfg.get("auto_crop_black_bars", True))

    # Audio input
    audio_source = final_video if final_video else source_video
    if job_dir.is_dir() and (job_dir / "mixed.wav").is_file():
        audio_input_path = job_dir / "mixed.wav"
    else:
        audio_input_path = audio_source

    v_input_path = source_video if source_video else final_video

    flip_str = "hflip," if mirror else ""
    fw = max(100, int(tw * video_scale)) & ~1
    fh = max(100, int(th * video_scale)) & ~1

    srt_filter = ""
    custom_ass = build_custom_ass_file(job_dir, cfg, tw, th) if job_dir.is_dir() else None
    if custom_ass and custom_ass.is_file():
        clean_ass = str(custom_ass).replace("\\", "/").replace(":", "\\:")
        srt_filter = f",ass='{clean_ass}'"
    elif srt_file:
        clean_srt = str(srt_file).replace("\\", "/").replace(":", "\\:")
        style_str = build_ass_force_style(cfg, default_font_size=18)
        srt_filter = f",subtitles='{clean_srt}':force_style='{style_str}'"

    # Kiểm tra bất đối xứng thời lượng video vs audio (ngăn ngừa đơ hình ở cuối)
    stream_durs = probe_stream_durations(str(v_input_path))
    v_dur = stream_durs.get("video")
    a_dur = stream_durs.get("audio") or stream_durs.get("format")
    duration_args = []
    if v_dur and a_dur and (v_dur < a_dur - 1.0):
        logger.info(f"Video {v_input_path} có luồng video ({v_dur:.2f}s) ngắn hơn audio ({a_dur:.2f}s) -> Tự động cắt khớp {v_dur:.2f}s để tránh đơ màn hình")
        duration_args = ["-t", f"{v_dur:.3f}"]

    protect_top_thumb = bool(cfg.get("protect_top_thumb", True))
    crop_padding = int(cfg.get("crop_padding", 0))
    smart_mask_info = None

    if use_anti_reup:
        real_motion_path = resolve_motion_bg_path(bg_motion_file) if bg_type == "video_motion" else ""
        if protect_top_thumb:
            smart_mask_info = generate_smart_thumb_mask(str(v_input_path), tw=tw, th=th, padding=crop_padding)

        if smart_mask_info and smart_mask_info.get("thumb_found"):
            # PHƯƠNG ÁN 1: Tự động bảo vệ thẻ chữ Thumb trên, che toàn bộ viền đen trên & dưới bằng nền mới
            mask_file = smart_mask_info["mask_path"]
            if bg_type == "video_motion" and real_motion_path and os.path.isfile(real_motion_path):
                extra_inputs = ["-stream_loop", "-1", "-i", real_motion_path, "-loop", "1", "-i", mask_file]
                fc = (
                    f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                    f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[2:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
            elif bg_type == "image":
                real_image_path = resolve_image_bg_path(bg_image_file)
                if real_image_path and os.path.isfile(real_image_path):
                    extra_inputs = ["-loop", "1", "-i", real_image_path, "-loop", "1", "-i", mask_file]
                    fc = (
                        f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                        f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                        f"[2:v]scale={tw}:{th},setsar=1[msk];"
                        f"[fg][msk]alphamerge[fg_alpha];"
                        f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                    )
                else:
                    safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                    extra_inputs = ["-loop", "1", "-i", mask_file]
                    fc = (
                        f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                        f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                        f"[1:v]scale={tw}:{th},setsar=1[msk];"
                        f"[fg][msk]alphamerge[fg_alpha];"
                        f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                    )
            elif bg_type == "blur":
                extra_inputs = ["-loop", "1", "-i", mask_file]
                fc = (
                    f"[0:v]split=2[v_orig][v_bg];"
                    f"[v_bg]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=25:5,setsar=1,colorlevels=rimax=0.65:gimax=0.65:bimax=0.65[bg];"
                    f"[v_orig]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[1:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
            else:
                safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                extra_inputs = ["-loop", "1", "-i", mask_file]
                fc = (
                    f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                    f"[0:v]{flip_str}scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[fg];"
                    f"[1:v]scale={tw}:{th},setsar=1[msk];"
                    f"[fg][msk]alphamerge[fg_alpha];"
                    f"[bg][fg_alpha]overlay=0:0:shortest=1{srt_filter}[outv]"
                )
        else:
            # Chế độ thông thường khi không có thumb card trên
            crop_prefix = ""
            if auto_crop_black_bars:
                crop_box = detect_letterbox_crop(str(v_input_path), padding=crop_padding)
                if crop_box:
                    crop_prefix = f"crop={crop_box['w']}:{crop_box['h']}:{crop_box['x']}:{crop_box['y']},"

            # Chế độ co dãn: Khi video_scale >= 0.99 (mặc định), giữ nguyên 100% chiều ngang, 2 bên sát mép (x=0)
            # Background chỉ che 2 phần đen trên và dưới
            if video_scale >= 0.99:
                scale_fg = f"{crop_prefix}{flip_str}scale={tw}:-2,setsar=1"
                overlay_coord = "0:(H-h)/2"
            else:
                scale_fg = f"{crop_prefix}{flip_str}scale={fw}:{fh}:force_original_aspect_ratio=decrease,setsar=1"
                overlay_coord = "(W-w)/2:(H-h)/2"

            if bg_type == "video_motion" and real_motion_path and os.path.isfile(real_motion_path):
                extra_inputs = ["-stream_loop", "-1", "-i", real_motion_path]
                fc = (
                    f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                    f"[0:v]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )
            elif bg_type == "image":
                real_image_path = resolve_image_bg_path(bg_image_file)
                if real_image_path and os.path.isfile(real_image_path):
                    extra_inputs = ["-loop", "1", "-i", real_image_path]
                    fc = (
                        f"[1:v]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},setsar=1[bg];"
                        f"[0:v]{scale_fg}[fg];"
                        f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                    )
                else:
                    safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                    fc = (
                        f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                        f"[0:v]{scale_fg}[fg];"
                        f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                    )
            elif bg_type == "blur":
                fc = (
                    f"[0:v]split=2[bg_in][fg_in];"
                    f"[bg_in]scale={tw}:{th}:force_original_aspect_ratio=increase,crop={tw}:{th},boxblur=25:5,setsar=1,colorlevels=rimax=0.65:gimax=0.65:bimax=0.65[bg];"
                    f"[fg_in]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )
            else:
                safe_color = cfg.get("bg_color", "#f59e0b").replace("#", "0x")
                fc = (
                    f"color=c={safe_color}:s={tw}x{th}:r=30[bg];"
                    f"[0:v]{scale_fg}[fg];"
                    f"[bg][fg]overlay={overlay_coord}{srt_filter}[outv]"
                )
    else:
        # Không chống re-up: Xuất kích thước chuẩn
        crop_prefix = ""
        if auto_crop_black_bars:
            crop_box = detect_letterbox_crop(str(v_input_path), padding=crop_padding)
            if crop_box:
                crop_prefix = f"crop={crop_box['w']}:{crop_box['h']}:{crop_box['x']}:{crop_box['y']},"

        if final_video and not mirror:
            fc = f"[0:v]{crop_prefix}scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1[outv]"
            v_input_path = final_video
        else:
            fc = f"[0:v]{crop_prefix}{flip_str}scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1{srt_filter}[outv]"

    # Lồng Watermark mờ mờ cố định ở góc phải nếu được kích hoạt
    wm_filt = build_watermark_filter(cfg, tw, th)
    if wm_filt:
        fc = fc.replace("[outv]", f"{wm_filt}[outv]")

    # Lắp ráp lệnh FFmpeg
    cmd_inputs = ["ffmpeg", "-y", "-i", str(v_input_path)]
    if extra_inputs:
        cmd_inputs.extend(extra_inputs)

    # Thêm audio input nếu khác video input
    map_audio = "0:a?"
    if str(audio_input_path) != str(v_input_path):
        audio_idx = cmd_inputs.count("-i")
        cmd_inputs.extend(["-i", str(audio_input_path)])
        map_audio = f"{audio_idx}:a?"

    fps_args = ["-r", fps] if fps in ("24", "30", "60") else []

    # NVENC command
    cmd_nvenc = cmd_inputs + [
        "-filter_complex", fc,
        "-map", "[outv]",
        "-map", map_audio,
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-b:v", f"{bitrate_kbps}k",
        "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
        "-bufsize", f"{bitrate_kbps * 2}k"
    ] + fps_args + [
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
    ] + duration_args + [
        str(out_path)
    ]

    success = False
    try:
        res = subprocess.run(cmd_nvenc, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
        if res.returncode == 0 and _valid_final_video(out_path):
            success = True
    except Exception as e:
        logger.warning(f"NVENC custom export failed: {e}")

    if not success:
        # Fallback CPU x264
        cmd_cpu = cmd_inputs + [
            "-filter_complex", fc,
            "-map", "[outv]",
            "-map", map_audio,
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{int(bitrate_kbps * 1.5)}k",
            "-bufsize", f"{bitrate_kbps * 2}k"
        ] + fps_args + [
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            "-shortest",
        ] + duration_args + [
            str(out_path)
        ]
        res_cpu = subprocess.run(cmd_cpu, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        if res_cpu.returncode == 0 and _valid_final_video(out_path):
            success = True

    if not success:
        raise HTTPException(status_code=500, detail="Không thể xuất video bằng FFmpeg")

    # Gắn Thumbnail vào đầu video nếu người dùng yêu cầu
    prepend_thumb = bool(payload.get("prepend_thumbnail", False))
    if prepend_thumb:
        clean_stem = re.sub(r'[^\w\-_.]', '_', stem)
        thumb_f = THUMBNAILS_DIR / f"{clean_stem}.jpg"
        if not thumb_f.is_file() and job_dir.is_dir() and (job_dir / "thumbnail.jpg").is_file():
            thumb_f = job_dir / "thumbnail.jpg"
        if thumb_f.is_file():
            try:
                _prepend_thumbnail_file(out_path, thumb_f, float(payload.get("thumbnail_duration_s", 0.8)))
            except Exception as e_prep:
                logger.warning(f"Không thể ghép thumbnail vào video xuất {out_path}: {e_prep}")

    # Xuất kèm file SRT nếu người dùng yêu cầu
    exported_srt_path = ""
    if export_srt:
        srt_target = out_dir / f"{out_path.stem}.srt"
        orig_srt_f = job_dir / "original.srt"
        trans_srt_f = job_dir / "translated.srt"

        if srt_type == "chinese" and orig_srt_f.is_file():
            srt_target.write_text(orig_srt_f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8-sig")
            exported_srt_path = str(srt_target)
        elif srt_type == "bilingual" and (trans_srt_f.is_file() or orig_srt_f.is_file()):
            orig_s = parse_srt(orig_srt_f.read_text(encoding="utf-8", errors="replace")) if orig_srt_f.is_file() else []
            trans_s = parse_srt(trans_srt_f.read_text(encoding="utf-8", errors="replace")) if trans_srt_f.is_file() else []
            blines = []
            for idx in range(max(len(trans_s), len(orig_s))):
                ts = trans_s[idx] if idx < len(trans_s) else None
                os_s = orig_s[idx] if idx < len(orig_s) else None
                s_str = ts["start_str"] if ts else os_s["start_str"]
                e_str = ts["end_str"] if ts else os_s["end_str"]
                blines.append(str(idx + 1))
                blines.append(f"{s_str} --> {e_str}")
                if ts: blines.append(ts["text"])
                if os_s: blines.append(os_s["text"])
                blines.append("")
            srt_target.write_text("\n".join(blines), encoding="utf-8-sig")
            exported_srt_path = str(srt_target)
        elif trans_srt_f.is_file():
            srt_target.write_text(trans_srt_f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8-sig")
            exported_srt_path = str(srt_target)

    size_mb = round(out_path.stat().st_size / (1024 * 1024), 2)
    return {
        "status": "ok",
        "message": f"Xuất video thành công ({resolution}, {bitrate_kbps} kbps, {fps} fps)",
        "output_file": str(out_path).replace("\\", "/"),
        "filename": out_path.name,
        "size_mb": size_mb,
        "exported_srt": exported_srt_path.replace("\\", "/") if exported_srt_path else "",
        "stream_url": f"/api/workflow/stream-file?path={out_path}"
    }
