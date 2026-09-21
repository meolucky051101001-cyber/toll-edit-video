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
from pydantic import BaseModel

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

# Thư mục lưu trữ workspace kịch bản & file âm thanh, phụ đề (tuyệt đối không lưu vào D:\banve hoặc D:\video phôi)
def _get_script_workspace() -> Path:
    candidates = [
        Path(__file__).resolve().parent.parent / "workspace" / "script_workspace",
        Path(r"C:\tool v1\workspace\script_workspace"),
        Path(__file__).resolve().parent / "workspace" / "script_workspace",
    ]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            return c
        except Exception:
            continue
    fallback = Path(__file__).resolve().parent.parent / "workspace" / "script_workspace"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback

SCRIPT_WORKSPACE = _get_script_workspace()
PREVIEWS_DIR = SCRIPT_WORKSPACE / "previews"
PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================================
# ===== PYDANTIC SCHEMAS ===================================================
# =========================================================================

class HookRequest(BaseModel):
    topic: str
    num_hooks: int = 6
    hook_duration: int = 7
    hook_type: str = "anti_copyright"

class ScriptGenerateRequest(BaseModel):
    topic: str
    platform: str = "tiktok"
    duration_target: int = 45
    style: str = "Chuyên gia cuốn hút & thực chiến"
    hook_text: str = ""
    custom_instruction: str = ""
    hook_duration: int = 7
    persona_gender: str = "neutral"

class PreviewTTSRequest(BaseModel):
    text: str
    voice: str = "BV562_streaming"

class RenderScriptRequest(BaseModel):
    scenes: list
    voice: str = "BV562_streaming"

class BurnVideoRequest(BaseModel):
    video_path: str
    audio_filename: str
    srt_filename: str
    output_filename: str = ""
    hook_card_text: Optional[str] = None
    hook_card_bg_color: Optional[str] = "#A52A3A"
    hook_card_show_badge: bool = False
    hook_card_badge_text: Optional[str] = ""
    hook_card_duration: Optional[float] = 4.5

class AutoPipelineRequest(BaseModel):
    video_path: str
    genre: str = "review"
    style: str = "Chuyên gia cuốn hút & thực chiến"
    custom_instruction: str = ""
    voice: str = "BV562_streaming"
    persona: str = "auto"
    hook_duration: int = 7
    anti_copyright: bool = True
    hook_card_text: Optional[str] = None
    hook_card_bg_color: str = "#A52A3A"
    hook_card_show_badge: bool = False
    hook_card_badge_text: str = ""

class VideoAnalyzeRequest(BaseModel):
    video_path: str
    genre: str = "review"
    style: str = "Chuyên gia cuốn hút & thực chiến"
    custom_instruction: str = ""
    hook_duration: int = 7
    persona_gender: str = "neutral"



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
    """Quét và trả về danh sách video có sẵn trong D:\\video phôi và D:\\banve để người dùng chọn nhanh."""
    video_dirs = [
        Path(r"D:\video phôi"),
        Path(r"D:\banve"),
    ]
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
        return {"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"}
    try:
        clean_path = req.video_path.replace("local:///", "").replace("/", "\\").strip()
        if not os.path.exists(clean_path):
            return {"status": "error", "message": f"Không tìm thấy file video: {clean_path}"}

        script = analyze_video_and_generate_script(
            video_path=clean_path,
            genre=req.genre,
            style=req.style,
            custom_instruction=req.custom_instruction,
            hook_duration=float(getattr(req, "hook_duration", 7) or 7),
            persona=getattr(req, "persona_gender", "auto") or "auto"
        )
        return {
            "status": "success",
            "script": script,
            "video_path": clean_path,
            "message": f"Đã dựng kịch bản thành công từ video ({script.get('video_duration', 0)}s)!"
        }
    except Exception as e:
        logger.error(f"Lỗi api_analyze_video: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.post("/api/script/hooks")
def api_generate_hooks(req: HookRequest):
    """Sinh 6 biến thể Hook giật tít cho chủ đề theo phong cách hook-generator."""
    try:
        hooks = generate_viral_hooks(
            topic=req.topic,
            num_hooks=req.num_hooks,
            hook_duration=int(getattr(req, "hook_duration", 7) or 7),
            mode=getattr(req, "hook_type", "anti_copyright") or "anti_copyright"
        )
        return {"status": "success", "hooks": hooks}
    except Exception as e:
        logger.error(f"Lỗi api_generate_hooks: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


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
            hook_duration=float(getattr(req, "hook_duration", 7) or 7),
            persona=getattr(req, "persona_gender", "auto") or "auto"
        )
        return {"status": "success", "script": script}
    except Exception as e:
        logger.error(f"Lỗi api_generate_script: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


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
        return {"status": "error", "message": str(e)}


@router.post("/api/script/render-all")
def api_render_full_script(req: RenderScriptRequest):
    """Sinh audio toàn bộ kịch bản bằng CapCut TTS, đo đạc và tạo file SRT chuẩn xác."""
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return {"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"}
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
        return {"status": "error", "message": str(e)}
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.post("/api/script/burn-video")
def api_burn_video(req: BurnVideoRequest):
    """Lồng audio TTS và phụ đề vào video nền bằng FFmpeg."""
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return {"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"}
    try:
        workspace_resolved = SCRIPT_WORKSPACE.resolve()
        audio_file = (SCRIPT_WORKSPACE / req.audio_filename).resolve()
        srt_file = (SCRIPT_WORKSPACE / req.srt_filename).resolve()

        if not str(audio_file).startswith(str(workspace_resolved)) or not str(srt_file).startswith(str(workspace_resolved)):
            return {"status": "error", "message": "Đường dẫn file đầu vào không hợp lệ."}

        clean_video_path = req.video_path.replace("local:///", "").replace("/", "\\").strip()
        if not os.path.exists(clean_video_path):
            return {"status": "error", "message": f"Không tìm thấy file video nền: {clean_video_path}"}

        out_name = req.output_filename.strip() or f"burned_script_{int(time.time())}.mp4"
        out_path = SCRIPT_WORKSPACE / out_name

        final_path = burn_script_to_video(
            video_path=clean_video_path,
            audio_path=str(audio_file),
            srt_path=str(srt_file),
            output_path=str(out_path),
            hook_card_text=req.hook_card_text,
            hook_card_bg_color=req.hook_card_bg_color or "#A52A3A",
            hook_card_show_badge=req.hook_card_show_badge,
            hook_card_badge_text=req.hook_card_badge_text or "",
            hook_card_duration=req.hook_card_duration or 4.5,
        )
        return {
            "status": "success",
            "final_video": final_path,
            "video_stream_url": f"/api/script/stream/{out_name}",
            "message": f"Đã xuất video hoàn tất: {final_path}"
        }
    except Exception as e:
        logger.error(f"Lỗi api_burn_video: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        SCRIPT_PROCESS_LOCK.release()


@router.post("/api/script/auto-pipeline")
def api_auto_pipeline(req: AutoPipelineRequest):
    """
    TỰ ĐỘNG HÓA TOÀN BỘ PIPELINE CHO 1 VIDEO (1-CLICK AUTO PIPELINE):
    1. AI Gemini Vision xem video, phân tích tình huống và lên kịch bản + Hook né bản quyền.
    2. CapCut TTS lồng tiếng toàn bộ phân cảnh, căn chỉnh timeline và xuất file SRT.
    3. FFmpeg dập audio giọng đọc, phụ đề SRT và Thẻ Text Hook Drama vào video thành phẩm.
    """
    if not SCRIPT_PROCESS_LOCK.acquire(blocking=False):
        return {"status": "error", "message": "Hệ thống đang xử lý tác vụ khác. Vui lòng đợi hoàn tất!"}
    try:
        clean_video_path = req.video_path.replace("local:///", "").replace("/", "\\").strip()
        if not os.path.exists(clean_video_path):
            return {"status": "error", "message": f"Không tìm thấy file video: {clean_video_path}"}

        logger.info(f"[AUTO-PIPELINE] Bắt đầu tự động hóa từ A-Z cho video: {clean_video_path}")

        # BƯỚC 1: Phân tích video & lên kịch bản + hook
        logger.info("[AUTO-PIPELINE] Bước 1: Gemini Vision phân tích video & bóc tách tình huống...")
        script = analyze_video_and_generate_script(
            video_path=clean_video_path,
            genre=req.genre,
            style=req.style,
            custom_instruction=req.custom_instruction,
            hook_duration=float(req.hook_duration or 7),
            persona=req.persona or "auto",
            voice=req.voice
        )
        scenes = script.get("scenes", [])
        if not scenes:
            raise RuntimeError("Không tạo được phân cảnh kịch bản từ video.")

        # Xác định câu Hook Card
        card_text = (req.hook_card_text or "").strip()
        if not card_text:
            sc1 = scenes[0]
            card_text = sc1.get("text_overlay") or script.get("title") or ""

        # BƯỚC 2: CapCut TTS thu âm & tạo phụ đề SRT
        logger.info(f"[AUTO-PIPELINE] Bước 2: CapCut TTS thu âm ({len(scenes)} cảnh) giọng {req.voice} & tạo file SRT...")
        render_res = render_full_script_tts(
            scenes=scenes,
            voice=req.voice,
            workspace_dir=str(SCRIPT_WORKSPACE),
            video_duration=script.get("video_duration")
        )
        audio_filename = render_res["audio_filename"]
        srt_filename = render_res["srt_filename"]

        audio_file = SCRIPT_WORKSPACE / audio_filename
        srt_file = SCRIPT_WORKSPACE / srt_filename

        # BƯỚC 3: Dập tiếng, phụ đề và Text Hook Drama vào video
        logger.info("[AUTO-PIPELINE] Bước 3: FFmpeg dập tiếng, sub và Text Hook Drama...")
        out_name = f"AutoDone_{Path(clean_video_path).stem}_{int(time.time())}.mp4"
        out_path = SCRIPT_WORKSPACE / out_name

        final_path = burn_script_to_video(
            video_path=clean_video_path,
            audio_path=str(audio_file),
            srt_path=str(srt_file),
            output_path=str(out_path),
            hook_card_text=card_text,
            hook_card_bg_color=req.hook_card_bg_color or "#A52A3A",
            hook_card_show_badge=req.hook_card_show_badge,
            hook_card_badge_text=req.hook_card_badge_text or "",
            hook_card_duration=float(req.hook_duration or 4.5)
        )

        logger.info(f"[AUTO-PIPELINE] Hoàn tất 100%! Xuất video: {final_path}")

        return {
            "status": "success",
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
            "message": f"🎉 Tự động hóa hoàn tất 100%! Video đã được lồng tiếng, dập sub và tạo Hook thành công."
        }
    except Exception as e:
        logger.error(f"[AUTO-PIPELINE] Lỗi tự động hóa: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
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
