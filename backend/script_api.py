# -*- coding: utf-8 -*-
"""
FastAPI Router cho Studio Kịch Bản AI & Lồng Tiếng CapCut TTS (/kich-ban).
Tích hợp Google Gemini Vision (video-to-script), Hook Generator, CapCut TTS 8 giọng và FFmpeg burning.
"""

from __future__ import annotations

import os
import sys
import time
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field

try:
    from backend.config.paths import AppPaths
except ImportError:
    from config.paths import AppPaths

from ai.scriptwriting import (
    analyze_video_and_generate_script,
    generate_video_script,
    generate_viral_hooks,
)
from ai.script_tts_service import (
    CAPCUT_VOICES,
    generate_single_tts,
    render_full_script_tts,
    burn_script_to_video,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Script Studio"])

# Lock đồng bộ để tránh tràn VRAM / quá tải API cùng lúc
SCRIPT_PROCESS_LOCK = threading.Lock()

# Bộ nhớ lưu tiến độ thời gian thực của các tác vụ Auto-Pipeline (in-memory task progress tracker)
PIPELINE_TASKS: Dict[str, Dict[str, Any]] = {}

def _update_task_progress(
    task_id: Optional[str],
    percent: int,
    step_name: str,
    status: str = "processing",
    error: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None
):
    """Cập nhật tiến độ xử lý của task cho frontend theo dõi thời gian thực."""
    if not task_id:
        return
    # Dọn dẹp task cũ khi danh sách quá lớn
    if len(PIPELINE_TASKS) > 100:
        now = time.time()
        for k in list(PIPELINE_TASKS.keys()):
            if now - PIPELINE_TASKS[k].get("updated_at", 0) > 7200:
                PIPELINE_TASKS.pop(k, None)

    PIPELINE_TASKS[task_id] = {
        "task_id": task_id,
        "percent": max(0, min(100, int(percent))),
        "step_name": step_name,
        "status": status,
        "error": error,
        "result": result,
        "updated_at": time.time()
    }

PATHS = AppPaths.from_environment(Path(__file__).resolve().parents[1])
SCRIPT_WORKSPACE = PATHS.workspace / "script_workspace"
SCRIPT_WORKSPACE.mkdir(parents=True, exist_ok=True)
PREVIEWS_DIR = SCRIPT_WORKSPACE / "previews"
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".ts", ".m4v"}

def _validate_safe_video_path(raw_path: str) -> Path:
    """Xác thực đường dẫn video an toàn, chống path traversal và probing hệ thống."""
    if not raw_path or not raw_path.strip():
        raise HTTPException(status_code=400, detail="Đường dẫn video không được để trống")
    cleaned = raw_path.replace("local:///", "").strip()
    p = Path(cleaned).resolve()
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"Không tìm thấy file video: {p.name}")
    if p.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Định dạng video không được hỗ trợ ({p.suffix}). Chỉ hỗ trợ: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}")
    return p


# =========================================================================
# ===== PYDANTIC SCHEMAS ===================================================
# =========================================================================

class HookRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)
    num_hooks: int = Field(default=6, ge=1, le=20)
    hook_duration: int = Field(default=7, ge=3, le=30)
    hook_type: str = Field(default="anti_copyright", max_length=50)

class ScriptGenerateRequest(BaseModel):
    topic: str = Field(..., min_length=1, max_length=1000)
    platform: str = Field(default="tiktok", max_length=50)
    duration_target: int = Field(default=45, ge=10, le=300)
    style: str = Field(default="Chuyên gia cuốn hút & thực chiến", max_length=200)
    hook_text: str = Field(default="", max_length=500)
    custom_instruction: str = Field(default="", max_length=2000)
    hook_duration: int = Field(default=7, ge=3, le=30)
    persona_gender: str = Field(default="neutral", max_length=50)

class PreviewTTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)
    voice: str = Field(default="BV562_streaming", max_length=100)

class RenderScriptRequest(BaseModel):
    scenes: List[Dict[str, Any]] = Field(..., min_items=1)
    voice: str = Field(default="BV562_streaming", max_length=100)

class BurnVideoRequest(BaseModel):
    video_path: str = Field(..., min_length=1)
    audio_filename: str = Field(..., min_length=1, max_length=255)
    srt_filename: str = Field(..., min_length=1, max_length=255)
    output_filename: str = Field(default="", max_length=255)
    hook_card_text: Optional[str] = Field(default=None, max_length=300)
    hook_card_bg_color: Optional[str] = Field(default="#A52A3A", max_length=20)
    hook_card_show_badge: bool = False
    hook_card_badge_text: Optional[str] = Field(default="", max_length=50)
    hook_card_duration: Optional[float] = Field(default=4.5, ge=1.0, le=30.0)

class AutoPipelineRequest(BaseModel):
    video_path: str = Field(..., min_length=1)
    genre: str = Field(default="review", max_length=50)
    style: str = Field(default="Chuyên gia cuốn hút & thực chiến", max_length=200)
    custom_instruction: str = Field(default="", max_length=2000)
    voice: str = Field(default="BV562_streaming", max_length=100)
    persona: str = Field(default="auto", max_length=50)
    persona_gender: Optional[str] = Field(default=None, max_length=50)
    hook_duration: int = Field(default=7, ge=3, le=30)
    anti_copyright: bool = True
    hook_card_text: Optional[str] = Field(default=None, max_length=300)
    hook_card_bg_color: str = Field(default="#A52A3A", max_length=20)
    hook_card_show_badge: bool = False
    hook_card_badge_text: str = Field(default="", max_length=50)
    hook_card_duration: float = Field(default=4.5, ge=1.0, le=30.0)
    task_id: Optional[str] = Field(default=None, max_length=100)

class VideoAnalyzeRequest(BaseModel):
    video_path: str = Field(..., min_length=1)
    genre: str = Field(default="review", max_length=50)
    style: str = Field(default="Chuyên gia cuốn hút & thực chiến", max_length=200)
    custom_instruction: str = Field(default="", max_length=2000)
    hook_duration: int = Field(default=7, ge=3, le=30)
    persona_gender: str = Field(default="neutral", max_length=50)
    persona: Optional[str] = Field(default=None, max_length=50)



# =========================================================================
# ===== ROUTES ============================================================
# =========================================================================

def get_current_control_token() -> str:
    token_path = Path(__file__).resolve().parent.parent / "workspace" / ".dashboard_control_token"
    if token_path.is_file():
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""

@router.get("/kich-ban", response_class=HTMLResponse)
def page_script_studio(request: Request):
    """Phục vụ trang giao diện Studio Kịch Bản AI & Lồng Sub."""
    template_path = Path(__file__).resolve().parent / "templates" / "script_studio.html"
    if not template_path.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy file template script_studio.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    token = get_current_control_token()
    html_content = html_content.replace("__REPLACE_TOKEN__", token)
    return HTMLResponse(content=html_content)


@router.get("/api/script/voices")
def api_get_script_voices():
    """Lấy danh mục 8 giọng đọc CapCut Việt Nam và Edge TTS."""
    return {"status": "success", "voices": CAPCUT_VOICES}


@router.get("/api/script/available-videos")
def api_get_available_videos():
    """Quét thư mục video nguồn và thành phẩm đang được cấu hình."""
    video_dirs = [PATHS.input_dir, PATHS.output_dir]
    extensions = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
    results: List[Dict[str, Any]] = []

    for d in video_dirs:
        if not d.exists():
            continue
        try:
            for item in d.iterdir():
                if item.is_file() and item.suffix.lower() in extensions:
                    if item.name.startswith("~") or item.name.startswith("."):
                        continue
                    try:
                        size_mb = round(item.stat().st_size / (1024 * 1024), 2)
                        mtime = item.stat().st_mtime
                    except Exception:
                        size_mb = 0
                        mtime = 0
                    results.append({
                        "path": str(item),
                        "name": item.name,
                        "folder": str(d),
                        "size_mb": size_mb,
                        "mtime": mtime,
                    })
        except Exception as e:
            logger.warning(f"Lỗi quét thư mục {d}: {e}")

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return {"status": "success", "videos": results[:50]}


@router.post("/api/script/analyze-video")
def api_analyze_video(req: VideoAnalyzeRequest):
    """AI xem video qua Gemini Vision, hiểu nội dung và dựng kịch bản khớp chính xác thời lượng video."""
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return JSONResponse(status_code=423, content={"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"})
    try:
        clean_video_path = _validate_safe_video_path(req.video_path)
        persona_val = req.persona or req.persona_gender or "auto"

        script = analyze_video_and_generate_script(
            video_path=str(clean_video_path),
            genre=req.genre,
            style=req.style,
            custom_instruction=req.custom_instruction,
            hook_duration=float(req.hook_duration or 7),
            persona=persona_val
        )
        return {
            "status": "success",
            "script": script,
            "video_path": str(clean_video_path),
            "message": f"Đã dựng kịch bản thành công từ video ({script.get('video_duration', 0)}s)!"
        }
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"status": "error", "message": he.detail})
    except Exception as e:
        logger.error(f"Lỗi api_analyze_video: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi phân tích video: {e}"})
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.post("/api/script/hooks")
def api_generate_hooks(req: HookRequest):
    """Sinh 6 biến thể Hook giật tít cho chủ đề theo phong cách hook-generator."""
    try:
        hooks = generate_viral_hooks(
            topic=req.topic,
            num_hooks=req.num_hooks,
            hook_duration=int(req.hook_duration or 7),
            mode=req.hook_type or "anti_copyright"
        )
        return {"status": "success", "hooks": hooks}
    except Exception as e:
        logger.error(f"Lỗi api_generate_hooks: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi tạo hook: {e}"})


@router.post("/api/script/generate")
def api_generate_script(req: ScriptGenerateRequest):
    """Sinh kịch bản video ngắn hoàn chỉnh theo chuẩn reels-scripting."""
    try:
        script = generate_video_script(
            topic=req.topic,
            platform=req.platform,
            duration_target=req.duration_target,
            style=req.style,
            hook_text=req.hook_text or None,
            custom_instruction=req.custom_instruction,
            hook_duration=float(req.hook_duration or 7),
            persona=req.persona_gender or "auto"
        )
        return {"status": "success", "script": script}
    except Exception as e:
        logger.error(f"Lỗi api_generate_script: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi sinh kịch bản: {e}"})


@router.post("/api/script/preview-tts")
def api_preview_tts(req: PreviewTTSRequest):
    """Sinh audio nhanh một câu thoại bằng CapCut TTS để nghe thử."""
    try:
        temp_file = PREVIEWS_DIR / f"preview_{int(time.time()*1000)}.mp3"
        duration = generate_single_tts(req.text, temp_file, voice=req.voice)
        return {
            "status": "success",
            "duration": duration,
            "stream_url": f"/api/script/stream/previews/{temp_file.name}"
        }
    except Exception as e:
        logger.error(f"Lỗi api_preview_tts: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi đọc thử âm thanh: {e}"})


@router.post("/api/script/render-all")
def api_render_full_script(req: RenderScriptRequest):
    """Sinh audio toàn bộ kịch bản bằng CapCut TTS, đo đạc và tạo file SRT chuẩn xác."""
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return JSONResponse(status_code=423, content={"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"})
    try:
        result = render_full_script_tts(
            scenes=req.scenes,
            voice=req.voice,
            workspace_dir=str(SCRIPT_WORKSPACE)
        )
        result["status"] = "success"
        result["audio_stream_url"] = f"/api/script/stream/{result['audio_filename']}"
        result["srt_download_url"] = f"/api/script/stream/{result['srt_filename']}"
        return result
    except Exception as e:
        logger.error(f"Lỗi api_render_full_script: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi lồng tiếng kịch bản: {e}"})
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.post("/api/script/burn-video")
def api_burn_video(req: BurnVideoRequest):
    """Lồng audio TTS và phụ đề vào video nền bằng FFmpeg."""
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return JSONResponse(status_code=423, content={"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"})
    try:
        workspace_resolved = SCRIPT_WORKSPACE.resolve()
        audio_file = (SCRIPT_WORKSPACE / req.audio_filename).resolve()
        srt_file = (SCRIPT_WORKSPACE / req.srt_filename).resolve()

        if not str(audio_file).startswith(str(workspace_resolved)) or not str(srt_file).startswith(str(workspace_resolved)):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Đường dẫn file đầu vào không hợp lệ."})

        if not audio_file.exists() or not srt_file.exists():
            return JSONResponse(status_code=404, content={"status": "error", "message": "Không tìm thấy file audio hoặc phụ đề đã tạo."})

        clean_video_path = _validate_safe_video_path(req.video_path)

        raw_out_name = os.path.basename(req.output_filename.strip()) if req.output_filename else ""
        out_name = raw_out_name or f"burned_script_{int(time.time())}.mp4"
        if not out_name.lower().endswith(".mp4"):
            out_name += ".mp4"
        out_path = (SCRIPT_WORKSPACE / out_name).resolve()
        if not str(out_path).startswith(str(workspace_resolved)):
            return JSONResponse(status_code=403, content={"status": "error", "message": "Tên file đầu ra không hợp lệ."})

        final_path = burn_script_to_video(
            video_path=str(clean_video_path),
            audio_path=str(audio_file),
            srt_path=str(srt_file),
            output_path=str(out_path),
            hook_card_text=req.hook_card_text,
            hook_card_bg_color=req.hook_card_bg_color or "#A52A3A",
            hook_card_show_badge=req.hook_card_show_badge,
            hook_card_badge_text=req.hook_card_badge_text or "",
            hook_card_duration=float(req.hook_card_duration or 4.5),
        )
        return {
            "status": "success",
            "final_video": final_path,
            "video_stream_url": f"/api/script/stream/{out_name}",
            "message": f"Đã xuất video hoàn tất: {final_path}"
        }
    except HTTPException as he:
        return JSONResponse(status_code=he.status_code, content={"status": "error", "message": he.detail})
    except Exception as e:
        logger.error(f"Lỗi api_burn_video: {e}", exc_info=True)
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi xuất video: {e}"})
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.get("/api/script/pipeline/progress/{task_id}")
def api_get_pipeline_progress(task_id: str):
    """Lấy tiến độ xử lý thời gian thực của tác vụ Auto-Pipeline."""
    task = PIPELINE_TASKS.get(task_id)
    if not task:
        return {"status": "unknown", "percent": 0, "step_name": "Đang chuẩn bị tiến trình...", "task_id": task_id}
    return {"status": "success", **task}


@router.post("/api/script/auto-pipeline")
def api_auto_pipeline(req: AutoPipelineRequest):
    """
    TỰ ĐỘNG HÓA TOÀN BỘ PIPELINE CHO 1 VIDEO (1-CLICK AUTO PIPELINE):
    1. AI Gemini Vision xem video, phân tích tình huống và lên kịch bản + Hook né bản quyền.
    2. CapCut TTS lồng tiếng toàn bộ phân cảnh, căn chỉnh timeline và xuất file SRT.
    3. FFmpeg dập audio giọng đọc, phụ đề SRT và Thẻ Text Hook Drama vào video thành phẩm.
    """
    task_id = req.task_id or f"task_{int(time.time()*1000)}"
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        _update_task_progress(task_id, 0, "Hệ thống đang bận xử lý tác vụ khác", status="error", error="LOCKED")
        return JSONResponse(status_code=423, content={"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"})
    try:
        _update_task_progress(task_id, 5, "Khởi động quy trình & kiểm tra video...", status="processing")
        clean_video_path = _validate_safe_video_path(req.video_path)
        logger.info(f"[AUTO-PIPELINE] Bắt đầu tự động hóa từ A-Z cho video: {clean_video_path}")

        persona_val = req.persona or req.persona_gender or "auto"

        # BƯỚC 1: Phân tích video & lên kịch bản + hook
        _update_task_progress(task_id, 15, "AI Gemini Vision đang xem video & bóc tách tình huống...")
        logger.info("[AUTO-PIPELINE] Bước 1: Gemini Vision phân tích video & bóc tách tình huống...")
        script = analyze_video_and_generate_script(
            video_path=str(clean_video_path),
            genre=req.genre,
            style=req.style,
            custom_instruction=req.custom_instruction,
            hook_duration=float(req.hook_duration or 7),
            persona=persona_val,
            voice=req.voice,
            anti_copyright=req.anti_copyright
        )
        scenes = script.get("scenes", [])
        if not scenes:
            raise RuntimeError("Không tạo được phân cảnh kịch bản từ video.")

        _update_task_progress(task_id, 35, f"Đã dựng kịch bản ({len(scenes)} phân cảnh). Bắt đầu lồng tiếng CapCut...")

        # Xác định câu Hook Card
        card_text = (req.hook_card_text or "").strip()
        if not card_text:
            sc1 = scenes[0]
            card_text = sc1.get("text_overlay") or script.get("title") or ""

        # BƯỚC 2: CapCut TTS thu âm & tạo phụ đề SRT
        logger.info(f"[AUTO-PIPELINE] Bước 2: CapCut TTS thu âm ({len(scenes)} cảnh) giọng {req.voice} & tạo file SRT...")
        def on_tts_progress(curr, total, msg):
            pct = 35 + int((curr / max(1, total)) * 35) # 35% -> 70%
            _update_task_progress(task_id, pct, f"CapCut TTS: Cảnh #{curr}/{total}")

        render_res = render_full_script_tts(
            scenes=scenes,
            voice=req.voice,
            workspace_dir=str(SCRIPT_WORKSPACE),
            video_duration=script.get("video_duration"),
            progress_callback=on_tts_progress
        )
        audio_filename = render_res["audio_filename"]
        srt_filename = render_res["srt_filename"]

        audio_file = SCRIPT_WORKSPACE / audio_filename
        srt_file = SCRIPT_WORKSPACE / srt_filename

        # BƯỚC 3: Dập tiếng, phụ đề và Text Hook Drama vào video
        _update_task_progress(task_id, 75, "FFmpeg đang dập tiếng, sub và Text Hook Drama vào video...")
        logger.info("[AUTO-PIPELINE] Bước 3: FFmpeg dập tiếng, sub và Text Hook Drama...")
        out_name = f"AutoDone_{clean_video_path.stem}_{int(time.time())}.mp4"
        out_path = SCRIPT_WORKSPACE / out_name

        final_path = burn_script_to_video(
            video_path=str(clean_video_path),
            audio_path=str(audio_file),
            srt_path=str(srt_file),
            output_path=str(out_path),
            hook_card_text=card_text,
            hook_card_bg_color=req.hook_card_bg_color or "#A52A3A",
            hook_card_show_badge=req.hook_card_show_badge,
            hook_card_badge_text=req.hook_card_badge_text or "",
            hook_card_duration=float(req.hook_card_duration or 4.5)
        )

        logger.info(f"[AUTO-PIPELINE] Hoàn tất 100%! Xuất video: {final_path}")

        response_payload = {
            "status": "success",
            "task_id": task_id,
            "script": script,
            "scenes": scenes,
            "audio_filename": audio_filename,
            "srt_filename": srt_filename,
            "audio_stream_url": f"/api/script/stream/{audio_filename}",
            "srt_download_url": f"/api/script/stream/{srt_filename}",
            "final_video": final_path,
            "video_stream_url": f"/api/script/stream/{out_name}",
            "hook_text": card_text,
            "total_duration": render_res.get("total_duration", 0),
            "message": "🎉 Tự động hóa hoàn tất 100%! Video đã được lồng tiếng, dập sub và tạo Hook thành công."
        }
        _update_task_progress(task_id, 100, "🎉 Xuất xưởng video thành công!", status="success", result=response_payload)
        return response_payload
    except HTTPException as he:
        _update_task_progress(task_id, 100, "Lỗi kiểm tra dữ liệu", status="error", error=he.detail)
        return JSONResponse(status_code=he.status_code, content={"status": "error", "message": he.detail})
    except Exception as e:
        logger.error(f"[AUTO-PIPELINE] Lỗi tự động hóa: {e}", exc_info=True)
        _update_task_progress(task_id, 100, f"Lỗi hệ thống: {e}", status="error", error=str(e))
        return JSONResponse(status_code=500, content={"status": "error", "message": f"Lỗi tự động hóa: {e}"})
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.get("/api/script/stream/{file_path:path}")
def api_stream_script_file(file_path: str):
    """Stream audio, video hoặc tải phụ đề SRT trực tiếp từ SCRIPT_WORKSPACE."""
    target = (SCRIPT_WORKSPACE / file_path).resolve()
    workspace_resolved = SCRIPT_WORKSPACE.resolve()

    if not str(target).startswith(str(workspace_resolved)):
        raise HTTPException(status_code=403, detail="Truy cập bị từ chối")

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy tệp")

    suffix = target.suffix.lower()
    media_types = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".srt": "text/plain; charset=utf-8",
        ".json": "application/json",
    }
    media_type = media_types.get(suffix, "application/octet-stream")
    return FileResponse(path=str(target), media_type=media_type, filename=target.name)
