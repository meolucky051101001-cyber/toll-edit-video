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
from pathlib import Path
from typing import Optional, Dict, Any, List
import cv2
import numpy as np
import asyncio
from urllib.parse import unquote
from fastapi import APIRouter, HTTPException, Query, Body, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse

from canvas_settings import (
    get_canvas_settings,
    save_canvas_settings,
    get_available_motion_backgrounds,
    resolve_motion_bg_path,
    get_available_image_backgrounds,
    resolve_image_bg_path
)

logger = logging.getLogger("workflow_api")

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

        new_bg_style = f"Style: BgStyle,Arial,{font_size},{bg_color_ass},{bg_color_ass},{bg_color_ass},{bg_color_ass},0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1"
        new_txt_style = f"Style: TextStyle,{font_name},{font_size},{font_color_ass},&H000000FF,&H00FFFFFF,&H00000000,{bold_val},0,0,0,100,100,0,0,1,2,0,5,10,10,10,1"

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
    outline_val = max(1, padding_y // 2)

    primary = hex_to_ass(font_color_hex, default_hex="000000", alpha=1.0)
    back = hex_to_ass(bg_color_hex, default_hex="ffffff", alpha=bg_opacity)

    pos_y_pct = float(cfg.get("sub_pos_y_pct", 0.82))
    margin_v = max(15, int(1920 * max(0.02, 1.0 - pos_y_pct))) if default_font_size >= 18 else max(10, int(1080 * max(0.02, 1.0 - pos_y_pct)))

    return (
        f"FontName={font_name},"
        f"FontSize={font_size},"
        f"PrimaryColour={primary},"
        f"BackColour={back},"
        f"BorderStyle=3,"
        f"Outline={outline_val},"
        f"Shadow=0,"
        f"Alignment=2,"
        f"MarginV={margin_v},"
        f"Bold={bold_val}"
    )


ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
INPUT_DIR = Path(os.getenv("AUTODUB_INPUT_DIR", r"D:\video phôi"))
OUTPUT_DIR = Path(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve"))
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


def safe_check_job_artifacts(stem: str) -> Dict[str, bool]:
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
            res["step1"] = (job_dir / "original.wav").is_file()
            res["step2"] = (job_dir / "htdemucs").is_dir()
            res["step3"] = (job_dir / "original.srt").is_file()
            res["step3_5"] = res["step3"]
            res["step4"] = (job_dir / "translated.srt").is_file()
            res["step5"] = (job_dir / "mixed.wav").is_file()
            try:
                for item in job_dir.iterdir():
                    if item.name.startswith("final_") and item.suffix.lower() == ".mp4" and item.stat().st_size > 1000:
                        res["step6"] = True
                        break
            except OSError:
                pass
    except OSError:
        pass

    # Kiểm tra thêm trong banve
    try:
        banve_cand = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
        if banve_cand.is_file() and banve_cand.stat().st_size > 1000:
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
                "start_str": start_str[:8],
                "end_str": end_str[:8],
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
                    steps = safe_check_job_artifacts(stem)
                    size_mb = round(p.stat().st_size / (1024 * 1024), 2)
                    mtime = p.stat().st_mtime
                    items.append({
                        "stem": stem,
                        "filename": p.name,
                        "source": "phoi",
                        "size_mb": size_mb,
                        "created_at": mtime,
                        "steps": steps
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
                    steps = safe_check_job_artifacts(stem)
                    size_mb = round(dl.stat().st_size / (1024 * 1024), 2)
                    mtime = dl.stat().st_mtime
                    items.append({
                        "stem": stem,
                        "filename": dl.name,
                        "source": "downloads",
                        "size_mb": size_mb,
                        "created_at": mtime,
                        "steps": steps
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
                if item.name.startswith("final_") and item.suffix.lower() == ".mp4" and item.stat().st_size > 1000:
                    final_video_url = f"/api/workflow/stream-file?path={item}"
                    break
        if not final_video_url and OUTPUT_DIR.exists():
            banve_cand = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
            if banve_cand.is_file() and banve_cand.stat().st_size > 1000:
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


@router.get("/api/workflow/stream-file")
async def api_stream_workflow_file(path: str = Query(...)):
    """Stream file media an toàn cho trình phát web."""
    try:
        clean_p = Path(path).resolve()
        if not clean_p.is_file():
            raise HTTPException(status_code=404, detail="Không tìm thấy tệp")

        valid_exts = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".wav", ".mp3", ".aac", ".jpg", ".png", ".webp"}
        if clean_p.suffix.lower() not in valid_exts:
            raise HTTPException(status_code=403, detail="Định dạng tệp không được hỗ trợ")

        media_type = mimetypes.guess_type(clean_p.name)[0] or "application/octet-stream"
        return FileResponse(str(clean_p), media_type=media_type)
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
            if res.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000:
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
                if res2.returncode == 0 and out_path.exists() and out_path.stat().st_size > 1000:
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
# FEATURE: CAPCUT-STYLE CUSTOM EXPORT & SRT DOWNLOAD
# =========================================================================

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
        output_folder = r"D:\banve"
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
            if item.name.startswith("final_") and item.suffix.lower() == ".mp4" and item.stat().st_size > 1000:
                final_video = item
                break
    if not final_video and OUTPUT_DIR.exists():
        cand_banve = OUTPUT_DIR / f"Dubbed_{stem}.mp4"
        if cand_banve.is_file() and cand_banve.stat().st_size > 1000:
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
        if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 1000:
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
        if res_cpu.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 1000:
            success = True

    if not success:
        raise HTTPException(status_code=500, detail="Không thể xuất video bằng FFmpeg")

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
