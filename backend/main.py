from fastapi import FastAPI, Form, Body, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import os
import sys
import asyncio
import datetime
import logging
import mimetypes
import uuid
import json
from urllib.parse import unquote
from pathlib import Path
from dashboard_media import media_status, paginate_listing, paginate_output
from typing import Optional

# Load .env local file
env_file = Path(__file__).resolve().parent / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

# The API in this worktree is Tool V1 only, regardless of inherited variables.
os.environ["PIPELINE_MODE"] = "legacy"

from environment import load_environment

BASE_DIR = Path(__file__).resolve().parent
load_environment(BASE_DIR)
from config.paths import AppPaths

PATHS = AppPaths.from_environment(BASE_DIR.parent)

from ai.transcription import extract_subtitles_whisper, save_srt
from ai.v1_asr_isolated import extract_subtitles_isolated
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio
from ai.v1_voice_isolated import generate_dubbing_audio_isolated
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video, separate_vocals_demucs
from pipeline_v2.config import PipelineMode, PipelineSettings
import shared_state
import job_tracker

logger = logging.getLogger("main_api")

app = FastAPI()
app.state.pipeline_runtime = sys.modules[__name__]

ALLOWED_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "AUTODUB_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088",
    ).split(",")
    if origin.strip() and origin.strip().lower() != "null"
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

import secrets

def _load_or_create_local_token() -> str:
    env_token = os.getenv("AUTODUB_LOCAL_TOKEN", "").strip()
    if env_token:
        return env_token
    candidate_paths = [
        Path(__file__).resolve().parent.parent / "workspace" / "bot_system" / ".dashboard_control_token",
        Path(__file__).resolve().parent.parent / "workspace" / ".dashboard_control_token",
        Path(__file__).resolve().parent.parent / "workspace_backup_safety" / ".dashboard_control_token",
        Path(__file__).resolve().parent / ".dashboard_control_token",
    ]
    for p in candidate_paths:
        try:
            if p.is_file():
                tok = p.read_text(encoding="utf-8").strip()
                if len(tok) >= 16:
                    return tok
        except Exception:
            pass
    new_token = secrets.token_urlsafe(32)
    for p in candidate_paths[:2]:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(new_token, encoding="utf-8")
        except Exception:
            pass
    return new_token

LOCAL_TOKEN = _load_or_create_local_token()
DASHBOARD_CONTROL_TOKEN = LOCAL_TOKEN


def _is_valid_host(host_header: str) -> bool:
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("[") and "]" in h:
        host_part = h[1:h.index("]")]
    else:
        host_part = h.split(":")[0]
    return host_part in ("127.0.0.1", "localhost", "::1", "testserver", "testclient")

@app.middleware("http")
async def security_middleware(request: Request, call_next):
    # 1. Host header validation (DNS Rebinding protection)
    host_header = request.headers.get("host", "")
    if not _is_valid_host(host_header):
        return JSONResponse(status_code=403, content={"detail": f"Host không hợp lệ: '{host_header}'"})

    # 2. Client socket IP validation (Defense in depth)
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        return JSONResponse(status_code=403, content={"detail": "Chỉ hỗ trợ truy cập nội bộ (localhost)"})

    # 3. State-changing token validation
    path = request.url.path
    method = request.method.upper()
    if method in ("POST", "PUT", "DELETE", "PATCH") and (path.startswith("/api/") or path.startswith("/a2ui/")):
        if not path.startswith("/api/script/") and not path.startswith("/api/workflow/thumbnail/") and path != "/api/voice-preview":
            token = request.headers.get("X-Local-Control-Token", "")
            if not token or not secrets.compare_digest(str(token), str(LOCAL_TOKEN)):
                # Dự phòng cho các nút điều khiển nội bộ từ dashboard UI hợp lệ
                is_dashboard_internal = (
                    request.headers.get("X-Dashboard-Input") == "1"
                    and request.headers.get("origin") in (None, "http://127.0.0.1:8088", "http://localhost:8088", "http://127.0.0.1:5173", "http://localhost:5173")
                    and path in ("/api/stop-batch", "/api/pause-video", "/api/resume-video", "/api/audio-settings", "/api/worker-settings")
                )
                if not is_dashboard_internal:
                    return JSONResponse(status_code=403, content={"detail": "Yêu cầu thiếu hoặc sai X-Local-Control-Token"})


    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent

def ensure_tool_control():
    try:
        import socket, subprocess, sys
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(('127.0.0.1', 8090)) == 0:
                return
        pythonw = BASE_DIR / "venv" / "Scripts" / "pythonw.exe"
        script = BASE_DIR / "tool_control.py"
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        subprocess.Popen(
            [str(pythonw), str(script)],
            cwd=str(BASE_DIR),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        logger.info("Launched tool_control.py on port 8090")
    except Exception as e:
        logger.warning("Failed to auto-start tool_control: %s", e)

def _install_asyncio_quiet_disconnect_handler() -> None:
    """Suppress noisy Windows ProactorEventLoop ConnectionResetError (WinError 10054) when browser video tags close range streams."""
    try:
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def _custom_exception_handler(loop_obj, context):
            exc = context.get("exception")
            if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                winerror = getattr(exc, "winerror", None)
                if winerror in (10054, 10053, None):
                    return
            msg = str(context.get("message") or "")
            if "_ProactorBasePipeTransport._call_connection_lost" in msg:
                return
            if previous_handler is not None:
                previous_handler(loop_obj, context)
            else:
                loop_obj.default_exception_handler(context)

        loop.set_exception_handler(_custom_exception_handler)
    except Exception:
        pass


@app.on_event("startup")
async def on_startup():
    _install_asyncio_quiet_disconnect_handler()
    ensure_tool_control()
    try:
        asyncio.create_task(_night_batch_watcher())
    except Exception as e:
        logger.warning("Không thể khởi động bộ theo dõi lịch chạy đêm: %s", e)
    try:
        from workspace_cleaner import run_full_retention_maintenance
        asyncio.create_task(asyncio.to_thread(run_full_retention_maintenance))
    except Exception as e:
        logger.warning("Không thể chạy bảo trì dọn dẹp workspace: %s", e)
WORKSPACE = os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace"))
OUTPUT_DIR = os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
os.makedirs(WORKSPACE, exist_ok=True)

def _get_system_path(rel_path: str) -> Path:
    bot_system = Path(WORKSPACE) / "bot_system"
    target = bot_system / rel_path
    if target.exists() or bot_system.is_dir():
        return target
    return Path(WORKSPACE) / rel_path

# Write runtime token file for tool_control graceful stop
TOKEN_FILE = _get_system_path(".dashboard_control_token")
try:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_t = TOKEN_FILE.with_suffix(".tmp")
    tmp_t.write_text(LOCAL_TOKEN, encoding="utf-8")
    tmp_t.replace(TOKEN_FILE)
except Exception as _te:
    logger.warning("Failed to write runtime control token: %s", _te)

def get_input_dir() -> Path:
    saved = _get_system_path("dashboard_input.json")
    if saved.exists():
        return Path(json.loads(saved.read_text(encoding="utf-8"))["path"])
    env_dir = os.getenv("AUTODUB_INPUT_DIR")
    if env_dir and os.path.exists(env_dir):
        return Path(env_dir)
    for cand in [Path(r"D:\video phôi"), Path(r"D:\video phoi"), Path(r"D:\video_input")]:
        if cand.exists():
            return cand
    return Path(r"D:\video phôi")

def get_output_dir() -> Path:
    env_dir = os.getenv("AUTODUB_OUTPUT_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(OUTPUT_DIR)

# The desktop/API surface can receive concurrent requests, while the media
# pipeline is intentionally sized for one long GPU/FFmpeg job at a time.  Keep
# requests queued instead of letting two jobs compete for shared output names,
# VRAM and large temporary files.
UNIFIED_PIPELINE_LOCK = asyncio.Lock()
API_PROCESS_LOCK = UNIFIED_PIPELINE_LOCK
BATCH_TASK_LOCK = UNIFIED_PIPELINE_LOCK
BATCH_INPUT_LOCK = asyncio.Lock()
BATCH_TASK: Optional[asyncio.Task] = None



def _validate_input_path(video_path: str) -> Path:
    from fastapi import HTTPException
    import os
    from pathlib import Path
    
    clean_path = video_path.strip()
    if not clean_path:
        raise HTTPException(status_code=400, detail="Invalid path")
    
    candidate = Path(clean_path).resolve()
    allowed_root = get_input_dir().resolve()
    
    try:
        candidate.relative_to(allowed_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path must be within allowed input directory")
        
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File not found")
        
    return candidate

def _folder_by_key(folder: str) -> Path:
    folders = {
        "phoi": get_input_dir(),
        "banve": get_output_dir(),
    }
    try:
        return folders[folder]
    except KeyError as exc:
        raise HTTPException(status_code=400, detail="Thư mục không hợp lệ") from exc


def _safe_media_path(folder: str, filename: str) -> Path:
    from batch_processor import SUPPORTED_EXTENSIONS

    root = _folder_by_key(folder).resolve()
    clean_name = filename.strip()
    if not clean_name or Path(clean_name).name != clean_name:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    candidate = (root / clean_name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Đường dẫn không hợp lệ") from exc
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Định dạng video không được hỗ trợ")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="File không tồn tại")
    return candidate


def _safe_preview_name(resource_id: str) -> str:
    """Ép resource_id (voice_id) thành tên file trần — chặn path traversal qua %2F, '..', ký tự ổ đĩa."""
    clean = (resource_id or "").strip()
    if not clean or clean in (".", "..") or clean != Path(clean).name or any(ch in '<>:"|?*' for ch in clean):
        raise HTTPException(status_code=400, detail="Tên tài nguyên không hợp lệ")
    return clean


def _read_log_tail(max_lines: int = 500) -> str:
    configured = os.getenv("AUTODUB_LOG_FILE")
    candidates = [
        Path(configured) if configured else None,
        BASE_DIR / "app.log",
        BASE_DIR.parent / "app.log",
    ]
    log_file = next((p for p in candidates if p and p.is_file()), None)
    if log_file is None:
        return "Chưa có log nào."
    with open(log_file, "r", encoding="utf-8", errors="replace") as handle:
        raw_tail = "".join(handle.readlines()[-max_lines:])
    try:
        from ai.v1_gemini_dispatcher import mask_secret
        return mask_secret(raw_tail)
    except Exception:
        return raw_tail



async def locate_v1_subtitles(video_path, segments):
    from ocr_utils import perform_video_ocr, release_ocr_reader
    try:
        _, width, height, main_y = await asyncio.to_thread(
            perform_video_ocr, video_path, srt_segments=segments
        )
        return width, height, main_y
    finally:
        release_ocr_reader()


async def run_api_pipeline_v2(
    video_path,
    out_dir,
    final_video,
    published_video,
    target_lang,
    voice_source,
    voice_param,
    api_key,
    font_name="Arial",
    font_color="&H00FFFFFF",
    font_weight=1,
):
    """Run the shared v2 runner for API routes when rollout mode is v2."""

    settings = PipelineSettings.from_env()
    if settings.mode is not PipelineMode.V2:
        return None

    from pipeline_v2.stage_validation import is_real_rvc_model
    from pipeline_v2.video_pipeline import (
        VideoPipelineRequest,
        VideoPipelineRunner,
        discover_rvc_model,
    )

    rvc_model = None
    selected_voice_param = voice_param
    if voice_source == "rvc":
        candidate = Path(voice_param)
        if is_real_rvc_model(candidate):
            rvc_model = candidate
        else:
            rvc_model = discover_rvc_model(Path(WORKSPACE))
        if rvc_model is None:
            raise RuntimeError(
                "RVC was requested but no real .pth model is available"
            )
        selected_voice_param = str(rvc_model)

    request = VideoPipelineRequest(
        video_path=Path(video_path),
        job_directory=Path(out_dir),
        output_path=Path(final_video),
        delivery_copy_path=Path(published_video),
        settings=settings,
        api_key=(GEMINI_API_KEY if voice_source == "fpt" else (api_key or GEMINI_API_KEY)),
        tts_api_key=(api_key if voice_source == "fpt" else ""),
        target_lang=target_lang,
        voice_source=voice_source,
        voice_param=selected_voice_param,
        rvc_model_path=rvc_model,
        font_name=font_name,
        font_color=font_color,
        font_weight=font_weight,
    )
    return await VideoPipelineRunner(request).run()

@app.get("/api/logs")
async def api_get_logs():
    """Trả về 100 dòng log mới nhất của bot."""
    try:
        return {"logs": await asyncio.to_thread(_read_log_tail)}
    except Exception as e:
        logger.warning("Lỗi khi đọc log: %s", e)
        return JSONResponse(
            status_code=500,
            content={"logs": "Không thể đọc nhật ký hệ thống."},
        )

# ===== API: Tạo phụ đề (Transcribe + Translate) =====
@app.post("/api/generate_subtitles")
@app.post("/api/generate-subtitles")
async def api_generate_subtitles(video_path: str = Form(...), target_lang: str = Form("vi")):
    """Nhận đường dẫn file video trên máy, tạo phụ đề gốc và dịch."""
    if UNIFIED_PIPELINE_LOCK.locked() or job_tracker.get_status().get("active"):
        raise HTTPException(status_code=409, detail="Pipeline đang bận xử lý video khác. Vui lòng đợi hoàn thành!")
    await UNIFIED_PIPELINE_LOCK.acquire()
    try:
        valid_path = _validate_input_path(video_path)
        video_path = str(valid_path)
        base_name = valid_path.stem
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")

        # 1. Extract audio
        await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)

        # 2. Transcribe with Whisper
        srt_segments = await asyncio.to_thread(extract_subtitles_isolated, original_audio, srt_original)

        # 3. Translate
        translated_segments = await asyncio.to_thread(translate_subtitles, 
            srt_segments,
            target_lang,
            api_key=GEMINI_API_KEY,
            video_path=video_path,
        )
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # Prepare response data
        subtitles = []
        for seg in translated_segments:
            subtitles.append({
                "index": seg.index,
                "start": seg.start.total_seconds(),
                "end": seg.end.total_seconds(),
                "content": seg.content
            })

        return {
            "status": "success",
            "original_srt": srt_original,
            "translated_srt": srt_translated,
            "subtitles": subtitles,
            "total": len(subtitles)
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        UNIFIED_PIPELINE_LOCK.release()


# ===== API: Xử lý full (Lồng tiếng + Xuất video) =====
@app.post("/api/process_video")
@app.post("/api/process-video")
async def api_process_video(
    video_path: str = Form(...),
    target_lang: str = Form("vi"),
    voice_source: str = Form("edge"),
    voice_param: str = Form("vi-VN-HoaiMyNeural"),
    api_key: str = Form(""),
    font_name: str = Form("Arial"),
    font_color: str = Form("&H00000000"),
    font_weight: int = Form(2),
    video_mode: str = Form("auto")
):
    """Xử lý full: Transcribe → Dịch → TTS → Mix Audio → Blur + Sub → Xuất video."""
    if UNIFIED_PIPELINE_LOCK.locked() or job_tracker.get_status().get("active"):
        raise HTTPException(status_code=409, detail="Đang có tiến trình Batch xử lý video. Vui lòng đợi hoàn thành!")
    await API_PROCESS_LOCK.acquire()
    try:
        import time
        valid_path = _validate_input_path(video_path)
        video_path = str(valid_path)
        base_name = valid_path.stem
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")
        dubbing_dir = os.path.join(out_dir, "dubbing")
        mixed_audio = os.path.join(out_dir, "mixed.wav")
        final_video = os.path.join(out_dir, f"final_{base_name}.mp4")
        published_video = os.path.join(OUTPUT_DIR, f"Dubbed_{base_name}.mp4")

        v2_result = await run_api_pipeline_v2(
            video_path,
            out_dir,
            final_video,
            published_video,
            target_lang,
            voice_source,
            voice_param,
            api_key,
            font_name,
            font_color,
            font_weight,
        )
        if v2_result is not None:
            return {
                "status": "success",
                "pipeline": "v2",
                "final_video": published_video,
                "manifest": str(v2_result.manifest_path),
                "qc_report": str(v2_result.qc_report_path),
                "qc_allowed": v2_result.qc_allowed,
                "message": f"Xuất video thành công: {published_video}",
            }

        # Kiểm tra cờ V1_USE_ORCHESTRATOR để kích hoạt luồng điều phối V1 nâng cấp
        try:
            from v1_feature_flags import get_feature_flags
            flags = get_feature_flags(WORKSPACE)
        except Exception:
            flags = {}

        if flags.get("V1_USE_ORCHESTRATOR", True):
            try:
                from v1_orchestrator import V1Orchestrator
                orch = V1Orchestrator(WORKSPACE)
                job_id = f"proc_{base_name}_{int(time.time())}"
                orch_res = await orch.execute_job(
                    video_path=video_path,
                    job_id=job_id,
                    output_dir=out_dir,
                    delivery_path=published_video,
                    user_mode=video_mode or "auto",
                    overrides={
                        "target_lang": target_lang,
                        "voice_source": voice_source,
                        "voice_param": voice_param,
                        "api_key": api_key,
                        "font_name": font_name,
                        "font_color": font_color,
                        "font_weight": font_weight,
                    },
                    progress_callback=lambda st, step, tot, pct, msg, d: logger.info(f"[{job_id}] ({pct:.1f}%) {msg}"),
                    stop_checker=lambda: getattr(shared_state, 'stop_requested', False),
                )
                return {
                    "status": "success",
                    "pipeline": "v1_orchestrator",
                    "video_mode": orch_res.get("video_mode"),
                    "final_video": orch_res.get("final_video", published_video),
                    "manifest": orch_res.get("manifest_file"),
                    "qc_status": orch_res.get("qc_status"),
                    "qc_report": orch_res.get("qc_report"),
                    "message": f"Xuất video thành công: {orch_res.get('final_video', published_video)}",
                }
            except asyncio.CancelledError:
                raise
            except Exception as orch_err:
                logger.exception("V1 job failed; checkpoint retained, no legacy restart")
                raise

        # 0. Snapshot/freeze job configuration (Legacy fallback)
        from job_config_service import get_frozen_config, freeze_job_config
        from audio_settings import get_audio_settings
        cur_audio_settings = get_audio_settings()
        video_name = os.path.basename(video_path)
        frozen_job = get_frozen_config(video_name, workspace_path=WORKSPACE) or freeze_job_config(
            video_name=video_name,
            workspace_path=WORKSPACE,
            overrides={
                "video_mode": video_mode or "auto",
                "bgm_volume_db": cur_audio_settings.get("bgm_volume_db", -2.0),
                "dubbing_volume_db": cur_audio_settings.get("dubbing_volume_db", 1.0),
                "separation_mode": cur_audio_settings.get("separation_mode", "roformer"),
                "ducking_mode": cur_audio_settings.get("ducking_mode", "soft"),
                "script_mode": cur_audio_settings.get("script_mode", "default"),
            }
        )
        frozen_eff = frozen_job.get("effective_config", {}) if frozen_job else {}
        job_bgm_vol = frozen_eff.get("bgm_volume_db", cur_audio_settings.get("bgm_volume_db", -2.0))
        job_dub_vol = frozen_eff.get("dubbing_volume_db", cur_audio_settings.get("dubbing_volume_db", 1.0))
        job_sep_mode = frozen_eff.get("separation_mode", cur_audio_settings.get("separation_mode", "roformer"))
        job_duck_mode = frozen_eff.get("ducking_mode", cur_audio_settings.get("ducking_mode", "soft"))
        job_script_mode = frozen_eff.get("script_mode", cur_audio_settings.get("script_mode", "default"))

        # 1. Extract audio
        await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)

        # 1.5. Separate vocals & background music (BS-RoFormer GPU / Demucs / Bypass)
        vocals_audio, no_vocals_audio = await asyncio.to_thread(
            separate_vocals_demucs,
            original_audio,
            out_dir,
            separation_mode=job_sep_mode,
        )

        # 2. Transcribe
        srt_segments = await asyncio.to_thread(extract_subtitles_isolated, vocals_audio, srt_original)
        vid_w, vid_h, main_y = await locate_v1_subtitles(video_path, srt_segments)

        # 3. Translate
        translated_segments = await asyncio.to_thread(translate_subtitles, 
            srt_segments,
            target_lang,
            api_key=GEMINI_API_KEY,
            video_path=video_path,
            script_mode=job_script_mode,
        )
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # 4. Quyết định giọng theo chế độ Auto/Manual (Codex Plan) & Tạo TTS dubbing
        from ai.v1_auto_voice import decide_video_voice, get_auto_voice_mode
        api_voice_mode = get_auto_voice_mode(WORKSPACE)
        voice_lock_info = await asyncio.to_thread(
            decide_video_voice,
            out_dir=out_dir,
            srt_segments=srt_segments,
            vocals_path=vocals_audio,
            original_audio_path=original_audio,
            video_path=video_path,
            voice_mode=api_voice_mode,
            workspace=WORKSPACE,
        )
        effective_source = voice_lock_info["voice_source"]
        effective_param = voice_lock_info["voice_param"]
        vid_duration = None
        try:
            from pydub import AudioSegment
            vid_duration = len(AudioSegment.from_file(original_audio)) / 1000.0
        except Exception:
            pass

        dubbing_audio_files = await generate_dubbing_audio_isolated(
            translated_segments, dubbing_dir,
            voice_source=effective_source,
            voice_param=effective_param,
            api_key=api_key,
            video_duration=vid_duration,
            segment_voices=voice_lock_info.get("segment_voices"),
            script_mode=job_script_mode,
        )

        # 4.5. Synchronize subtitle timing to audio duration & anti-overlap
        from ass_utils import sync_and_clamp_subtitles
        translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

        # 5. Mix audio (clean instrumental BGM + dubbing)
        await asyncio.to_thread(
            mix_audio_pydub,
            no_vocals_audio,
            dubbing_audio_files,
            mixed_audio,
            original_volume_db=job_bgm_vol,
            dubbing_volume_db=job_dub_vol,
            ducking_mode=job_duck_mode,
            explicit=True,
        )

        # 6. Final render: Blur + Subtitles + Audio → Output video
        from ass_utils import generate_ass_file
        ass_path = os.path.join(out_dir, "final.ass")
        
        await asyncio.to_thread(
            generate_ass_file, translated_segments, [], ass_path,
            play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y,
            font_name=font_name, font_color=font_color, font_weight=font_weight,
        )
        rendered = await asyncio.to_thread(process_video, 
            video_path,
            ass_path,
            mixed_audio,
            final_video,
            font_name=font_name,
            font_color=font_color,
            font_weight=font_weight,
        )
        if not rendered:
            raise RuntimeError("Final video render failed")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        from pipeline_v2.atomic_io import atomic_copy_file

        atomic_copy_file(final_video, published_video)

        return {
            "status": "success",
            "final_video": published_video,
            "message": f"Xuất video thành công: {published_video}"
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}
    finally:
        API_PROCESS_LOCK.release()


# ===== API: Tải video đã xuất =====
@app.get("/api/download/{filename}")
async def download_video(filename: str):
    """Tải file video đã render xong."""
    from batch_processor import SUPPORTED_EXTENSIONS

    clean = filename.strip()
    if not clean or clean in (".", "..") or clean != Path(clean).name or clean.startswith("."):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Tên file không hợp lệ"})
    if Path(clean).suffix.lower() not in SUPPORTED_EXTENSIONS:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Chỉ cho phép tải file video"})
    # Search in workspace subdirectories
    for root, dirs, files in os.walk(WORKSPACE):
        if clean in files:
            return FileResponse(os.path.join(root, clean), media_type="video/mp4", filename=clean)
    return {"status": "error", "message": "File not found"}


# ===== API: Tải video từ URL + Xử lý tự động =====
@app.post("/api/process_url")
@app.post("/api/process-url")
async def api_process_url(
    url: str = Form(...),
    target_lang: str = Form("vi"),
    voice_source: str = Form("edge"),
    voice_param: str = Form("vi-VN-HoaiMyNeural"),
    api_key: str = Form(""),
    font_name: str = Form("Arial"),
    font_color: str = Form("&H00000000"),
    font_weight: int = Form(2),
    video_mode: str = Form("auto")
):
    """Tải video từ URL (Xiaohongshu, TikTok, YouTube...) rồi xử lý toàn bộ."""
    if UNIFIED_PIPELINE_LOCK.locked() or job_tracker.get_status().get("active"):
        raise HTTPException(status_code=409, detail="Pipeline đang bận xử lý video khác. Vui lòng đợi hoàn thành!")
    await API_PROCESS_LOCK.acquire()
    try:
        import time

        download_dir = os.path.join(WORKSPACE, "downloads")
        os.makedirs(download_dir, exist_ok=True)

        # Nanosecond prefix prevents stale-file collisions between fast retries.
        timestamp = str(time.time_ns())
        # Use the shared no-watermark downloader, including 206 + ffprobe checks.
        import importlib
        import social_downloader
        importlib.reload(social_downloader)
        from social_downloader import download_social_video

        success, video_path, _title, download_error = await asyncio.to_thread(
            download_social_video, url, download_dir, timestamp
        )
        if not success:
            return {
                "status": "error",
                "step": "download",
                "message": download_error or "Không thể tải video.",
            }
        video_filename = os.path.basename(video_path)
        base_name = os.path.splitext(video_filename)[0]

        # 2. Xử lý pipeline (giống process_video)
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")
        dubbing_dir = os.path.join(out_dir, "dubbing")
        mixed_audio = os.path.join(out_dir, "mixed.wav")
        final_video = os.path.join(out_dir, f"final_{base_name}.mp4")
        published_video = os.path.join(OUTPUT_DIR, f"Dubbed_{base_name}.mp4")

        v2_result = await run_api_pipeline_v2(
            video_path,
            out_dir,
            final_video,
            published_video,
            target_lang,
            voice_source,
            voice_param,
            api_key,
            font_name,
            font_color,
            font_weight,
        )
        if v2_result is not None:
            return {
                "status": "success",
                "pipeline": "v2",
                "downloaded_video": video_path,
                "final_video": published_video,
                "manifest": str(v2_result.manifest_path),
                "qc_report": str(v2_result.qc_report_path),
                "qc_allowed": v2_result.qc_allowed,
                "message": f"Hoàn tất! Video đã xuất tại: {published_video}",
            }

        # Kiểm tra cờ V1_USE_ORCHESTRATOR để kích hoạt luồng điều phối V1 nâng cấp
        try:
            from v1_feature_flags import get_feature_flags
            flags = get_feature_flags(WORKSPACE)
        except Exception:
            flags = {}

        if flags.get("V1_USE_ORCHESTRATOR", True):
            try:
                from v1_orchestrator import V1Orchestrator
                orch = V1Orchestrator(WORKSPACE)
                job_id = f"url_{base_name}_{int(time.time())}"
                orch_res = await orch.execute_job(
                    video_path=video_path,
                    job_id=job_id,
                    output_dir=out_dir,
                    delivery_path=published_video,
                    user_mode=video_mode or "auto",
                    overrides={
                        "target_lang": target_lang,
                        "voice_source": voice_source,
                        "voice_param": voice_param,
                        "api_key": api_key,
                        "font_name": font_name,
                        "font_color": font_color,
                        "font_weight": font_weight,
                    },
                    progress_callback=lambda st, step, tot, pct, msg, d: logger.info(f"[{job_id}] ({pct:.1f}%) {msg}"),
                    stop_checker=lambda: getattr(shared_state, 'stop_requested', False),
                )
                return {
                    "status": "success",
                    "pipeline": "v1_orchestrator",
                    "video_mode": orch_res.get("video_mode"),
                    "downloaded_video": video_path,
                    "final_video": orch_res.get("final_video", published_video),
                    "manifest": orch_res.get("manifest_file"),
                    "qc_status": orch_res.get("qc_status"),
                    "qc_report": orch_res.get("qc_report"),
                    "message": f"Hoàn tất! Video đã xuất tại: {orch_res.get('final_video', published_video)}",
                }
            except asyncio.CancelledError:
                raise
            except Exception as orch_err:
                logger.exception("V1 job failed; checkpoint retained, no legacy restart")
                raise

        # 0. Snapshot/freeze job configuration (Legacy fallback)
        from job_config_service import get_frozen_config, freeze_job_config
        from audio_settings import get_audio_settings
        cur_audio_settings = get_audio_settings()
        video_name = os.path.basename(video_path)
        frozen_job = get_frozen_config(video_name, workspace_path=WORKSPACE) or freeze_job_config(
            video_name=video_name,
            workspace_path=WORKSPACE,
            overrides={
                "video_mode": video_mode or "auto",
                "bgm_volume_db": cur_audio_settings.get("bgm_volume_db", -2.0),
                "dubbing_volume_db": cur_audio_settings.get("dubbing_volume_db", 1.0),
                "separation_mode": cur_audio_settings.get("separation_mode", "roformer"),
                "ducking_mode": cur_audio_settings.get("ducking_mode", "soft"),
                "script_mode": cur_audio_settings.get("script_mode", "default"),
            }
        )
        frozen_eff = frozen_job.get("effective_config", {}) if frozen_job else {}
        job_bgm_vol = frozen_eff.get("bgm_volume_db", cur_audio_settings.get("bgm_volume_db", -2.0))
        job_dub_vol = frozen_eff.get("dubbing_volume_db", cur_audio_settings.get("dubbing_volume_db", 1.0))
        job_sep_mode = frozen_eff.get("separation_mode", cur_audio_settings.get("separation_mode", "roformer"))
        job_duck_mode = frozen_eff.get("ducking_mode", cur_audio_settings.get("ducking_mode", "soft"))
        job_script_mode = frozen_eff.get("script_mode", cur_audio_settings.get("script_mode", "default"))

        # Extract audio
        await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)

        # Separate vocals & background music (BS-RoFormer GPU / Demucs / Bypass)
        vocals_audio, no_vocals_audio = await asyncio.to_thread(
            separate_vocals_demucs,
            original_audio,
            out_dir,
            separation_mode=job_sep_mode,
        )

        # Transcribe
        srt_segments = await asyncio.to_thread(extract_subtitles_isolated, vocals_audio, srt_original)
        vid_w, vid_h, main_y = await locate_v1_subtitles(video_path, srt_segments)

        # Translate
        translated_segments = await asyncio.to_thread(translate_subtitles, 
            srt_segments,
            target_lang,
            api_key=GEMINI_API_KEY,
            video_path=video_path,
            script_mode=job_script_mode,
        )
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # Subtitles for response
        subtitles = []
        for seg in translated_segments:
            subtitles.append({
                "index": seg.index,
                "start": seg.start.total_seconds(),
                "end": seg.end.total_seconds(),
                "content": seg.content
            })

        # TTS: Quyết định giọng theo chế độ Auto/Manual (Codex Plan)
        from ai.v1_auto_voice import decide_video_voice, get_auto_voice_mode
        api_voice_mode = get_auto_voice_mode(WORKSPACE)
        voice_lock_info = await asyncio.to_thread(
            decide_video_voice,
            out_dir=out_dir,
            srt_segments=srt_segments,
            vocals_path=vocals_audio,
            original_audio_path=original_audio,
            video_path=None,
            voice_mode=api_voice_mode,
            workspace=WORKSPACE,
        )
        effective_source = voice_lock_info["voice_source"]
        effective_param = voice_lock_info["voice_param"]
        vid_duration = None
        try:
            from pydub import AudioSegment
            vid_duration = len(AudioSegment.from_file(original_audio)) / 1000.0
        except Exception:
            pass

        dubbing_audio_files = await generate_dubbing_audio_isolated(
            translated_segments, dubbing_dir,
            voice_source=effective_source,
            voice_param=effective_param,
            api_key=api_key,
            video_duration=vid_duration,
            segment_voices=voice_lock_info.get("segment_voices"),
            script_mode=job_script_mode,
        )

        # Synchronize subtitle timing to audio duration & anti-overlap
        from ass_utils import sync_and_clamp_subtitles
        translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

        # Mix audio (clean instrumental BGM + dubbing)
        await asyncio.to_thread(
            mix_audio_pydub,
            no_vocals_audio,
            dubbing_audio_files,
            mixed_audio,
            original_volume_db=job_bgm_vol,
            dubbing_volume_db=job_dub_vol,
            ducking_mode=job_duck_mode,
            explicit=True,
        )

        # Final render
        from ass_utils import generate_ass_file
        ass_path = os.path.join(out_dir, "final.ass")
        
        await asyncio.to_thread(
            generate_ass_file, translated_segments, [], ass_path,
            play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y,
            font_name=font_name, font_color=font_color, font_weight=font_weight,
        )
        rendered = await asyncio.to_thread(process_video, 
            video_path,
            ass_path,
            mixed_audio,
            final_video,
            font_name=font_name,
            font_color=font_color,
            font_weight=font_weight,
        )
        if not rendered:
            raise RuntimeError("Final video render failed")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        from pipeline_v2.atomic_io import atomic_copy_file

        atomic_copy_file(final_video, published_video)

        return {
            "status": "success",
            "downloaded_video": video_path,
            "final_video": published_video,
            "subtitles": subtitles,
            "message": f"Hoàn tất! Video đã xuất tại: {published_video}"
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"status": "error", "step": "process", "message": str(e)}
    finally:
        API_PROCESS_LOCK.release()


# ===== DASHBOARD & MONITORING ENDPOINTS =====

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Phục vụ giao diện Dashboard Web Tool V1."""
    template_path = BASE_DIR / "templates" / "dashboard.html"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read().replace("__REPLACE_TOKEN__", LOCAL_TOKEN)
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h2>Dashboard template not found.</h2>", status_code=404)


@app.get("/api/auth-token")
async def api_get_auth_token():
    """Trả về control token hợp lệ cho giao diện nội bộ localhost."""
    return {"token": LOCAL_TOKEN}


@app.get("/api/status")
async def api_get_status():
    """Trả về trạng thái tiến độ thời gian thực của tác vụ hiện tại."""
    # get_status có thể chạy ffprobe xác minh thành phẩm — đẩy sang thread để không block event loop
    status = await asyncio.to_thread(job_tracker.get_status)
    import video_pause
    status["pause_state"] = video_pause.state()
    status["stop_requested"] = bool(
        status.get("stop_requested")
        or getattr(shared_state, "stop_requested", False)
    )
    if status.get("active") and not status.get("router_info"):
        vname = status.get("video_name", "")
        if vname:
            try:
                from v1_video_router import route_video
                candidates = [
                    Path(WORKSPACE) / "downloads" / vname,
                    Path(r"D:\workspace\downloads") / vname,
                    Path(WORKSPACE) / vname,
                ]
                vpath = next((p for p in candidates if p.is_file()), None)
                if vpath:
                    dec = await asyncio.to_thread(route_video, vpath, requested_mode=status.get("video_mode") or "AUTO")
                    req_m = str(dec.requested_mode).upper()
                    res_m = dec.resolved_mode.value
                    lbl = f"AUTO · {res_m}" if req_m == "AUTO" else f"Thủ công · {res_m}"
                    if res_m == "SHORT": lbl += " (≤ 7m)"
                    elif res_m == "LONG": lbl += " (> 7m)"
                    elif res_m == "MEDIUM": lbl += " (Chuyển tiếp)"

                    status["router_info"] = {
                        "resolved_mode": res_m,
                        "requested_mode": req_m,
                        "is_escalated": dec.is_escalated,
                        "escalation_reasons": dec.escalation_reasons,
                        "duration_s": dec.metadata.duration_s,
                        "router_label": lbl,
                        "separation_model": dec.planned_pipeline.get("separation_model", "roformer"),
                        "asr_model": dec.planned_pipeline.get("asr_model", "whisper_turbo"),
                    }
                    status["video_mode"] = res_m
            except Exception:
                pass
    try:
        from v1_vram_monitor import get_vram_stats
        status["vram"] = get_vram_stats()
    except Exception:
        pass
    try:
        from worker_settings import get_worker_settings
        status["worker_settings"] = get_worker_settings()
    except Exception:
        pass
    return status



@app.get("/api/queue")
async def api_get_queue():
    import json
    path = _get_system_path("telegram_queue.json")
    if not path.is_file():
        return {"items": [], "available": False, "message": "Chưa kết nối hàng đợi Telegram. Cần khởi động lại bot để bật tính năng."}
    try:
        data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8-sig"))
        alive = job_tracker._pid_is_running(int(data.get("pid") or 0))
        # Bổ sung thông tin router cho từng video trong hàng chờ
        try:
            from v1_video_router import route_video
            for item in data.get("items", []):
                rs_vp = item.get("resume_state", {}).get("video_path")
                if rs_vp and Path(rs_vp).is_file():
                    dec = await asyncio.to_thread(route_video, rs_vp, requested_mode=item.get("video_mode") or "AUTO")
                    req_m = str(dec.requested_mode).upper()
                    res_m = dec.resolved_mode.value
                    lbl = f"AUTO · {res_m}" if req_m == "AUTO" else f"Thủ công · {res_m}"
                    if res_m == "SHORT": lbl += " (≤ 7m)"
                    elif res_m == "LONG": lbl += " (> 7m)"
                    item["router_info"] = {
                        "resolved_mode": res_m,
                        "requested_mode": req_m,
                        "router_label": lbl,
                        "duration_s": dec.metadata.duration_s,
                    }
        except Exception:
            pass
        return {**data, "available": alive, "message": "" if alive else "Bot đã dừng; danh sách là bản ghi cuối cùng."}
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "Không đọc được hàng đợi Telegram")


@app.post("/api/queue/process")
async def api_process_queue(request: Request):
    """Kích hoạt xử lý lại danh sách video đang đợi trong hàng chờ Telegram."""
    queue_file = _get_system_path("telegram_queue.json")
    if not queue_file.is_file():
        return {"status": "empty", "message": "Không tìm thấy hàng chờ video."}

    try:
        import json, time
        data = json.loads(await asyncio.to_thread(queue_file.read_text, encoding="utf-8-sig"))
        items = data.get("items", [])
        if not items:
            return {"status": "empty", "message": "Hàng chờ hiện đang trống."}

        # 1. Gỡ cờ pause nếu Tool V1 đang bị tạm dừng
        pause_flag = _get_system_path("control") / "v1.pause"
        try:
            pause_flag.unlink(missing_ok=True)
        except Exception:
            pass

        # 2. Ghi cờ kích hoạt để bot đang chạy nhặt việc ngay lập tức
        trigger_flag = _get_system_path("telegram_queue_trigger.flag")
        trigger_flag.write_text(str(time.time()), encoding="utf-8")

        # 3. Kiểm tra xem telegram_bot.py có đang chạy không. Nếu chưa chạy thì khởi động
        alive = False
        pid = data.get("pid")
        if pid and job_tracker._pid_is_running(int(pid)):
            alive = True
        else:
            import psutil
            for p in psutil.process_iter(['pid', 'cmdline']):
                try:
                    cmdline = p.info.get('cmdline') or []
                    if any('telegram_bot.py' in str(arg) for arg in cmdline):
                        alive = True
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        if not alive:
            python = BASE_DIR / "venv" / "Scripts" / "python.exe"
            if not python.exists():
                python = Path(sys.executable)
            import subprocess
            subprocess.Popen(
                [str(python), str(BASE_DIR / "background_service.py"), "--service", "telegram"],
                cwd=str(BASE_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return {
                "status": "started",
                "count": len(items),
                "message": f"Đã khởi động Bot và kích hoạt xử lý {len(items)} video trong hàng chờ!"
            }

        return {
            "status": "processing",
            "count": len(items),
            "message": f"Đã gửi lệnh xử lý {len(items)} video trong hàng chờ!"
        }
    except Exception as e:
        logger.exception("Lỗi api_process_queue")
        raise HTTPException(500, f"Lỗi kích hoạt hàng chờ: {str(e)}")


@app.post("/api/queue/clear")
async def api_clear_queue(request: Request):
    """Xóa sạch các video đang đợi trong hàng chờ Telegram."""
    queue_file = _get_system_path("telegram_queue.json")
    clear_flag = _get_system_path("telegram_queue_clear.flag")
    try:
        import json, time
        # Ghi cờ xóa cho bot đang chạy
        clear_flag.write_text(str(time.time()), encoding="utf-8")
        # Đồng thời cập nhật file snapshot json ngay lập tức
        if queue_file.is_file():
            payload = {"items": [], "updated_at": time.time(), "pid": 0}
            queue_file.write_text(json.dumps(payload), encoding="utf-8")
        return {"status": "cleared", "message": "Đã xóa toàn bộ video trong hàng chờ."}
    except Exception as e:
        logger.exception("Lỗi api_clear_queue")
        raise HTTPException(500, f"Lỗi xóa hàng chờ: {str(e)}")


@app.post("/api/queue/move")
async def api_move_queue_item(request: Request):
    """Thay đổi thứ tự ưu tiên của video trong hàng chờ Telegram (đẩy lên / xuống / lên đầu)."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Dữ liệu JSON không hợp lệ")

    index = payload.get("index")
    action = payload.get("action", "up")  # 'up', 'down', 'top', 'position' (kèm 'to')

    if index is None or not isinstance(index, int):
        raise HTTPException(400, "Vị trí index không hợp lệ")

    queue_file = _get_system_path("telegram_queue.json")
    if not queue_file.is_file():
        raise HTTPException(404, "Hàng chờ video không tồn tại")

    try:
        import json, time
        data = json.loads(await asyncio.to_thread(queue_file.read_text, encoding="utf-8-sig"))
        items = data.get("items", [])
        n = len(items)
        if n <= 1 or index < 0 or index >= n:
            return {"status": "unchanged", "items": items, "message": "Không thể thay đổi vị trí"}

        moved_name = items[index].get("name", "video")
        if len(moved_name) > 35:
            moved_name = moved_name[:35] + "..."

        if action == "up" and index > 0:
            items[index - 1], items[index] = items[index], items[index - 1]
            msg = f"Đã đẩy video #{index + 1} lên vị trí #{index}!"
        elif action == "top" and index > 0:
            target = items.pop(index)
            items.insert(0, target)
            msg = f"Đã đưa video lên đầu hàng chờ (#1)!"
        elif action == "position" and isinstance(payload.get("to"), int):
            to = max(0, min(int(payload["to"]), n - 1))
            if to == index:
                return {"status": "unchanged", "items": items, "message": "Vị trí không đổi"}
            target = items.pop(index)
            items.insert(to, target)
            msg = f"Đã chuyển video #{index + 1} đến vị trí #{to + 1}!"
        elif action == "down" and index < n - 1:
            items[index], items[index + 1] = items[index + 1], items[index]
            msg = f"Đã chuyển video #{index + 1} xuống vị trí #{index + 2}!"
        else:
            return {"status": "unchanged", "items": items, "message": "Vị trí đã ở giới hạn"}

        # Cập nhật lại chỉ số position cho từng video
        for i, item in enumerate(items, 1):
            item["position"] = i

        data["items"] = items
        data["updated_at"] = time.time()

        # Lưu lại file json snapshot
        await asyncio.to_thread(queue_file.write_text, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Ghi cờ tín hiệu để telegram_bot.py đang chạy đồng bộ lại bộ nhớ ngay lập tức
        reorder_flag = _get_system_path("telegram_queue_reorder.flag")
        reorder_flag.write_text(str(time.time()), encoding="utf-8")

        return {"status": "success", "items": items, "message": msg}
    except Exception as e:
        logger.exception("Lỗi api_move_queue_item")
        raise HTTPException(500, f"Lỗi di chuyển video: {str(e)}")


@app.post("/api/queue/delete")
async def api_delete_queue_item(request: Request):
    """Xóa một video khỏi hàng chờ Telegram theo index."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Dữ liệu JSON không hợp lệ")

    index = payload.get("index")
    if index is None or not isinstance(index, int):
        raise HTTPException(400, "Vị trí index không hợp lệ")

    queue_file = _get_system_path("telegram_queue.json")
    if not queue_file.is_file():
        raise HTTPException(404, "Hàng chờ video không tồn tại")

    try:
        import json, time
        data = json.loads(await asyncio.to_thread(queue_file.read_text, encoding="utf-8-sig"))
        items = data.get("items", [])
        n = len(items)
        if index < 0 or index >= n:
            return {"status": "unchanged", "items": items, "message": "Không tìm thấy video cần xóa"}

        removed = items.pop(index)
        removed_name = removed.get("name", "video")
        if len(removed_name) > 35:
            removed_name = removed_name[:35] + "..."

        # Cập nhật lại chỉ số position cho từng video
        for i, item in enumerate(items, 1):
            item["position"] = i

        data["items"] = items
        data["updated_at"] = time.time()

        # Thêm key của item bị xóa vào danh sách deleted_keys để bot loại bỏ khỏi bộ nhớ
        del_key = str(removed.get("url") or removed.get("filename") or removed.get("file_id") or removed.get("name") or "").strip()
        deleted_list = data.get("deleted_keys", [])
        if del_key and del_key not in deleted_list:
            deleted_list.append(del_key)
        data["deleted_keys"] = deleted_list

        await asyncio.to_thread(queue_file.write_text, json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # Ghi cờ tín hiệu để telegram_bot.py đang chạy đồng bộ lại bộ nhớ ngay lập tức
        reorder_flag = _get_system_path("telegram_queue_reorder.flag")
        reorder_flag.write_text(str(time.time()), encoding="utf-8")

        return {"status": "success", "items": items, "message": f'Đã xóa video "{removed_name}" khỏi hàng chờ!'}
    except Exception as e:
        logger.exception("Lỗi api_delete_queue_item")
        raise HTTPException(500, f"Lỗi xóa video: {str(e)}")

def _check_input_request(request):
    if request.headers.get("X-Dashboard-Input") != "1" or request.headers.get("origin") not in (None, "http://127.0.0.1:8088", "http://localhost:8088"):
        raise HTTPException(403, "Yêu cầu không hợp lệ")
    if (BATCH_TASK is not None and not BATCH_TASK.done()) or job_tracker.get_status().get("active"):
        raise HTTPException(409, "Chờ video đang xử lý hoàn tất trước khi đổi nguồn hoặc thêm video.")

FOLDER_PICKER_LOCK = asyncio.Lock()

@app.post("/api/choose-input-folder")
async def api_choose_input_folder(request: Request):
    _check_input_request(request)
    if FOLDER_PICKER_LOCK.locked():
        raise HTTPException(409, "Hộp chọn thư mục đang mở. Hãy chọn hoặc bấm Hủy trong cửa sổ đó.")
    async with FOLDER_PICKER_LOCK:
        import sys, subprocess
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(BASE_DIR / "choose_video_folder.py"), str(get_input_dir()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode:
                raise HTTPException(503, "Không mở được hộp chọn thư mục Windows. Hãy kiểm tra phiên desktop đang đăng nhập.")
            return json.loads(stdout.decode("utf-8"))
        except asyncio.TimeoutError:
            raise HTTPException(408, "Hết thời gian chọn thư mục. Bấm Chọn thư mục để thử lại.")
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

@app.post("/api/input-folder")
async def api_input_folder(request: Request):
    async with BATCH_INPUT_LOCK:
        _check_input_request(request)
        data = await request.json()
        value = data.get("path", "") if isinstance(data, dict) else ""
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(400, "Nhập đường dẫn thư mục video.")
        path = Path(value.strip().strip('"'))
        if not path.is_absolute() or not path.is_dir():
            raise HTTPException(400, "Thư mục không tồn tại. Hãy nhập đường dẫn đầy đủ.")
        path = path.resolve()
        if path == get_output_dir().resolve():
            raise HTTPException(400, "Hãy chọn thư mục nguồn khác thư mục video hoàn thành.")
        dest = Path(WORKSPACE) / "dashboard_input.json"
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps({"path": str(path)}), encoding="utf-8")
        os.replace(tmp, dest)
        return {"path": str(path)}

@app.post("/api/input-video")
async def api_input_video(request: Request):
    from batch_processor import SUPPORTED_EXTENSIONS
    async with BATCH_INPUT_LOCK:
        _check_input_request(request)
        name = unquote(request.headers.get("X-Video-Name", ""))
        if not name or Path(name).name != name or any(c in name for c in '/\\:') or Path(name).suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise HTTPException(400, "Chỉ nhận file video MP4, MKV, MOV, AVI, WEBM, FLV, M4V.")
        if name.startswith("Dubbed_"):
            name = "Source_" + name
        root = get_input_dir().resolve()
        if not root.is_dir():
            raise HTTPException(400, "Thư mục nguồn không còn tồn tại. Hãy chọn lại.")
        # Stage completely before exposing the video to the batch scanner.
        tmp = root / (".upload-" + uuid.uuid4().hex + ".part")
        target = root / name
        total = 0
        try:
            with tmp.open("xb") as out:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 4 * 1024**3:
                        raise HTTPException(413, "Mỗi video tối đa 4 GB.")
                    await asyncio.to_thread(out.write, chunk)
            if not total:
                raise HTTPException(400, "File video rỗng.")
            if target.exists():
                target = root / (Path(name).stem + "_" + uuid.uuid4().hex[:8] + Path(name).suffix)
            tmp.rename(target)
            # F10 — cảnh báo trùng nội dung ngay khi tải lên + đăng ký fingerprint
            dup_info = None
            try:
                from duplicate_guard import check_path as _dup_check, register as _dup_register
                dup_info = await asyncio.to_thread(_dup_check, target)
                await asyncio.to_thread(_dup_register, "upload", target.name, target)
            except Exception:
                pass
            resp = {"name": target.name, "size": total}
            if dup_info:
                resp["duplicate_warning"] = (
                    f"Nội dung trùng với '{dup_info.get('name')}' "
                    f"({'thành phẩm đã xuất' if dup_info.get('kind') == 'output' else 'file đã tải lên trước đây'})!"
                )
            return resp
        finally:
            tmp.unlink(missing_ok=True)

@app.get("/api/phoi")
async def api_get_phoi(limit: Optional[int] = None, offset: int = 0, search: str = "", status: str = "all"):
    return await asyncio.to_thread(_list_phoi, limit, offset, search, status)


def _list_phoi(limit=None, offset=0, search="", status="all"):
    """Quét và trả về danh sách các video trong thư mục video phôi (D:\\video phôi)."""
    input_dir = get_input_dir()
    if not input_dir.exists():
        return paginate_listing({
            "exists": False,
            "path": str(input_dir),
            "files": [],
            "total_count": 0,
            "total_size_mb": 0,
        }, limit, offset, search, status)

    current_status = job_tracker.get_status()
    active_video = current_status.get("video_name", "")

    output_dir = get_output_dir()
    output_files = set(os.listdir(output_dir)) if output_dir.exists() else set()

    from batch_processor import SUPPORTED_EXTENSIONS
    files_data = []
    total_size = 0
    try:
        for f in sorted(os.listdir(input_dir)):
            full_path = input_dir / f
            if full_path.is_file() and f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_"):
                size_b = full_path.stat().st_size
                total_size += size_b
                mtime_str = datetime.datetime.fromtimestamp(full_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                stem = full_path.stem

                if f == active_video and current_status.get("active"):
                    v_status = "running"
                    v_status_label = f"Đang chạy ({current_status.get('percent', 0)}%)"
                elif f == active_video and current_status.get("status") in ("error", "stopped", "interrupted"):
                    v_status = "error" if current_status.get("status") == "error" else "waiting"
                    v_status_label = "Xử lý lỗi" if v_status == "error" else "Đã dừng; có thể chạy lại"
                elif f"Dubbed_{stem}.mp4" in output_files:
                    candidate = output_dir / f"Dubbed_{stem}.mp4"
                    check = media_status(candidate, defer=True)
                    v_status = "invalid" if check["status"] == "invalid" else "unverified"
                    v_status_label = "Đầu ra không hợp lệ" if v_status == "invalid" else "Có đầu ra; chưa đối chiếu nguồn"
                else:
                    v_status = "waiting"
                    v_status_label = "Chờ xử lý"

                files_data.append({
                    "name": f,
                    "size_mb": round(size_b / (1024 * 1024), 2),
                    "modified": mtime_str,
                    "status": v_status,
                    "status_label": v_status_label,
                })
    except Exception as e:
        logger.error(f"Lỗi đọc thư mục video phôi: {e}")

    return paginate_listing({
        "exists": True,
        "path": str(input_dir),
        "files": files_data,
        "total_count": len(files_data),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }, limit, offset, search, status)


@app.get("/api/banve")
async def api_get_banve(limit: Optional[int] = None, offset: int = 0, search: str = "", status: str = "all"):
    return await asyncio.to_thread(_list_banve, limit, offset, search, status)


def _list_banve(limit=None, offset=0, search="", status="all"):
    """Quét và trả về danh sách các video thành phẩm trong D:\\banve."""
    output_dir = get_output_dir()
    if not output_dir.exists():
        return paginate_listing({
            "exists": False,
            "path": str(output_dir),
            "files": [],
            "total_count": 0,
            "total_size_mb": 0,
        }, limit, offset, search, status)

    from batch_processor import SUPPORTED_EXTENSIONS
    from render_history import get_all_render_durations, format_duration
    durations = get_all_render_durations(output_dir)

    files_data = []
    total_size = 0
    total_render_sec = 0
    try:
        v2_workspace_history = Path(r"C:\tool v2\workspace\.render_history_v2.json")
        v2_names = set()
        if v2_workspace_history.exists():
            try:
                v2_names = set(json.loads(v2_workspace_history.read_text(encoding="utf-8")).keys())
            except Exception:
                pass

        for f in sorted(os.listdir(output_dir), reverse=True):
            if f.startswith("Dubbed_queue_") or f in v2_names:
                continue
            full_path = output_dir / f
            if full_path.is_file() and f.lower().endswith(SUPPORTED_EXTENSIONS):
                size_b = full_path.stat().st_size
                total_size += size_b
                mtime_str = datetime.datetime.fromtimestamp(full_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                dur_sec = durations.get(f) or durations.get(f.replace("Dubbed_", "")) or 0
                if dur_sec:
                    total_render_sec += dur_sec
                files_data.append({
                    "name": f,
                    "size_mb": round(size_b / (1024 * 1024), 2),
                    "created": mtime_str,
                    "duration_seconds": dur_sec,
                    "duration_formatted": format_duration(dur_sec) if dur_sec else "--",
                })
    except Exception as e:
        logger.error(f"Lỗi đọc thư mục bản vẽ: {e}")

    files_data.sort(key=lambda item: item["created"], reverse=True)
    result = paginate_listing({
        "exists": True,
        "path": str(output_dir),
        "files": files_data,
        "total_count": len(files_data),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "total_duration_seconds": total_render_sec,
        "total_duration_formatted": format_duration(total_render_sec) if total_render_sec else "--",
    }, None, 0, "", "all")
    return paginate_output(result, output_dir, limit, offset, search, status)


async def batch_runner(input_dir: str = "", output_dir: str = "", job_id: str = "", video_mode: str = "auto"):
    if not input_dir:
        input_dir = str(get_input_dir())
    if not output_dir:
        output_dir = str(get_output_dir())
    if not job_id:
        job_id = uuid.uuid4().hex
    from batch_processor import process_batch_folder
    async with UNIFIED_PIPELINE_LOCK:
        try:
            await process_batch_folder(
                input_dir, output_dir, job_id=job_id, video_mode=video_mode
            )
        except job_tracker.JobAlreadyRunningError as e:
            logger.warning("Từ chối batch trùng: %s", e)
        except Exception as e:
            logger.error("Lỗi chạy batch qua API: %s", e, exc_info=True)
            job_tracker.fail_batch(str(e), job_id=job_id)


@app.post("/api/run-batch")
async def api_run_batch(request: Request = None, video_mode: str = "auto"):
    """Kích hoạt chạy batch toàn bộ video phôi ngầm."""
    global BATCH_TASK
    selected_mode = str(video_mode or "auto").strip() or "auto"
    if request is not None:
        try:
            header_mode = request.headers.get("X-Video-Mode")
            if header_mode:
                selected_mode = header_mode.strip()
            elif request.headers.get("content-type", "").startswith("application/json"):
                body = await request.json()
                if isinstance(body, dict) and body.get("video_mode"):
                    selected_mode = str(body["video_mode"]).strip()
        except Exception:
            pass

    status = job_tracker.get_status()
    if UNIFIED_PIPELINE_LOCK.locked():
        return JSONResponse(
            status_code=409,
            content={
                "status": "busy",
                "job_id": status.get("job_id"),
                "message": "Pipeline đang bận xử lý video khác.",
            },
        )
    if Path(r"C:\tool v1\workspace\control\v1.pause").exists():
        raise HTTPException(409, "Tool đang tắt, không nhận batch mới.")
    status = job_tracker.get_status()
    if (BATCH_TASK is not None and not BATCH_TASK.done()) or status.get("active"):
        return JSONResponse(
            status_code=409,
            content={
                "status": "busy",
                "job_id": status.get("job_id"),
                "message": "Hiện đang có tiến trình video đang xử lý, vui lòng đợi!",
            },
        )

    shared_state.stop_requested = False
    input_dir = str(get_input_dir())
    output_dir = str(get_output_dir())
    job_id = uuid.uuid4().hex
    try:
        job_tracker.start_batch(0, input_dir, output_dir, job_id=job_id)
    except job_tracker.JobAlreadyRunningError:
        return JSONResponse(status_code=409, content={
            "status": "busy", "message": "Một batch khác đang chạy."})

    BATCH_TASK = asyncio.create_task(
        batch_runner(input_dir, output_dir, job_id, video_mode=selected_mode), name=f"autodub-batch-{job_id}"
    )

    return JSONResponse(
        status_code=202,
        content={
            "status": "started",
            "job_id": job_id,
            "message": "Đã bắt đầu xử lý video trong thư mục đã chọn!",
        },
    )


@app.post("/api/pause-video")
async def api_pause_video(request: Request):
    if request.headers.get("X-Dashboard-Input") != "1" or request.headers.get("origin") not in (None,"http://127.0.0.1:8088","http://localhost:8088"):
        raise HTTPException(403, "Yêu cầu không hợp lệ")
    import video_pause
    if not job_tracker.get_status().get("active"):
        raise HTTPException(409, "Không có video đang xử lý.")
    video_pause.request_pause()
    return {"message": "Sẽ tạm dừng sau bước đang chạy."}

@app.post("/api/resume-video")
async def api_resume_video(request: Request):
    if request.headers.get("X-Dashboard-Input") != "1" or request.headers.get("origin") not in (None,"http://127.0.0.1:8088","http://localhost:8088"):
        raise HTTPException(403, "Yêu cầu không hợp lệ")
    import video_pause
    video_pause.resume()
    return {"message": "Đã tiếp tục xử lý video."}

@app.post("/api/stop-batch")
async def api_stop_batch():
    """Yêu cầu dừng tiến trình batch đang chạy (Áp dụng tức thì cho cả Dashboard và Telegram)."""
    import shared_state
    shared_state.stop_requested = True

    # 1. Ghi nhận dừng vào job_tracker & tạo file cờ stop
    job_tracker.request_stop()

    # 2. Hủy asyncio task trong main.py nếu có
    global BATCH_TASK
    if BATCH_TASK and not BATCH_TASK.done():
        BATCH_TASK.cancel()

    # 3. Tiêu diệt tất cả subprocess nặng (ffmpeg, demucs, separator_worker)
    killed_count = 0
    try:
        from process_killer import terminate_worker_processes
        killed = terminate_worker_processes()
        killed_count = len(killed)
        logger.info(f"Đã dừng batch và tiêu diệt các tiến trình con: {killed}")
    except Exception as e:
        logger.warning(f"Lỗi khi dọn tiến trình con: {e}")

    # 4. Đánh dấu dừng hoàn tất trong job_tracker
    job_tracker.mark_stopped("Đã dừng tiến trình theo yêu cầu từ Dashboard!")

    return {
        "status": "stopped",
        "message": "Đã dừng tiến trình thành công! Đã dọn dẹp các tác vụ ngầm.",
        "killed_processes": killed_count,
    }


@app.get("/api/preflight")
async def api_preflight():
    from preflight_checker import run_full_preflight
    return await asyncio.to_thread(
        run_full_preflight,
        tool="v1",
        input_dir=get_input_dir(),
        output_dir=get_output_dir(),
        workspace_dir=Path(WORKSPACE),
        backend_dir=BASE_DIR,
    )


@app.get("/api/task-history")
async def api_task_history():
    from history_service import get_task_history
    return await asyncio.to_thread(
        get_task_history,
        tool_name="v1",
        output_dir=get_output_dir(),
        workspace_dir=Path(WORKSPACE),
    )


async def single_video_runner(video_path: str, output_dir: str, job_id: str, *,
                              recovery_mode: str = "recover", config_overrides=None,
                              work_dir=None, pipeline_lock_held: bool = False):
    from batch_processor import process_single_local_video
    async def execute():
        try:
            job_tracker.start_batch(1, str(Path(video_path).parent), output_dir, job_id=job_id)
            ok = await process_single_local_video(
                video_path, output_dir, job_id=job_id, overrides=config_overrides,
                resume_if_possible=recovery_mode != "restart", work_dir=work_dir,
            )
            if ok:
                job_tracker.finish_batch()
            else:
                job_tracker.fail_batch("Video processing failed", job_id=job_id)
        except Exception as e:
            logger.error(f"Lỗi chạy retry video qua API: {e}", exc_info=True)
            job_tracker.fail_batch(str(e), job_id=job_id)
        finally:
            job_tracker.release_batch(job_id)
    if pipeline_lock_held:
        try:
            await execute()
        finally:
            UNIFIED_PIPELINE_LOCK.release()
    else:
        async with UNIFIED_PIPELINE_LOCK:
            await execute()


@app.post("/api/retry-video")
async def api_retry_video(request: Request):
    global BATCH_TASK
    status = job_tracker.get_status()
    if UNIFIED_PIPELINE_LOCK.locked() or (BATCH_TASK is not None and not BATCH_TASK.done()) or status.get("active"):
        return JSONResponse(
            status_code=409,
            content={
                "status": "busy",
                "job_id": status.get("job_id"),
                "message": "Hiện đang có tiến trình video đang xử lý, vui lòng đợi!",
            },
        )
    if Path(r"C:\tool v1\workspace\control\v1.pause").exists():
        raise HTTPException(409, "Tool đang tắt, không nhận xử lý video mới.")

    filename = ""
    try:
        data = await request.json()
        filename = data.get("filename", "").strip()
    except Exception:
        pass
    if not filename:
        try:
            form = await request.form()
            filename = form.get("filename", "").strip()
        except Exception:
            pass

    if not filename:
        raise HTTPException(status_code=400, detail="Thiếu tên video cần chạy lại (filename).")

    input_dir = get_input_dir()
    video_path = input_dir / filename

    if not video_path.is_file():
        processed_file = input_dir / "processed" / filename
        if processed_file.is_file():
            try:
                import shutil
                shutil.copy2(processed_file, video_path)
            except Exception:
                video_path = processed_file
        else:
            raise HTTPException(status_code=404, detail=f"Không tìm thấy file video {filename} trong thư mục phôi.")

    shared_state.stop_requested = False
    output_dir = str(get_output_dir())
    job_id = uuid.uuid4().hex

    BATCH_TASK = asyncio.create_task(
        single_video_runner(str(video_path), output_dir, job_id),
        name=f"autodub-retry-{job_id}"
    )

    return JSONResponse(
        status_code=200,
        content={
            "status": "started",
            "job_id": job_id,
            "video": filename,
            "message": f"Đã bắt đầu xử lý lại video '{filename}'!",
        },
    )


@app.post("/api/open-folder")
async def api_open_folder(folder: str = Form(...)):
    """Mở thư mục Video Phôi hoặc Bản Vẽ trong File Explorer của Windows."""
    target = _folder_by_key(folder)
    try:
        target = target.resolve(strict=True)
        if not target.is_dir():
            raise NotADirectoryError(str(target))
        # ShellExecute uses the interactive Windows shell, not a hidden child console.
        await asyncio.to_thread(os.startfile, str(target), "open", "", None, 1)
        return {"status": "ok", "path": str(target)}
    except Exception as e:
        logger.exception("Cannot open folder %s", target)
        return JSONResponse(status_code=500, content={
            "status": "error", "path": str(target),
            "message": f"Không mở được thư mục {target}: {e}",
        })


@app.get("/api/stream/{folder}/{filename}")
async def api_stream_video(folder: str, filename: str):
    """Stream video để xem trước trực tiếp trên Dashboard."""
    from urllib.parse import unquote
    file_path = _safe_media_path(folder, unquote(filename))
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return FileResponse(str(file_path), media_type=media_type)


# ===== AUDIO SETTINGS (MIXER GAIN CONTROLS) =====
from audio_settings import get_audio_settings, save_audio_settings

@app.get("/api/audio-settings")
async def api_get_audio_settings():
    """Lấy cấu hình âm lượng BGM và Dubbing Voice hiện tại."""
    return get_audio_settings()


@app.post("/api/audio-settings")
async def api_save_audio_settings(payload: dict = Body(...)):
    """Lưu cấu hình âm lượng BGM, Dubbing Voice, Separation Mode, Ducking Mode và Script Mode mới."""
    bgm = payload.get("bgm_volume_db", -2.0)
    dub = payload.get("dubbing_volume_db", 1.0)
    sep = payload.get("separation_mode")
    duck = payload.get("ducking_mode")
    script = payload.get("script_mode")
    return save_audio_settings(bgm, dub, separation_mode=sep, ducking_mode=duck, script_mode=script)


# ===== VRAM & WORKER CONCURRENCY CONTROLS =====
from v1_vram_monitor import get_vram_stats
from worker_settings import get_worker_settings, save_worker_settings

@app.get("/api/vram")
async def api_get_vram():
    """Lấy thông tin GPU VRAM thời gian thực."""
    return get_vram_stats()


@app.get("/api/worker-settings")
async def api_get_worker_settings():
    """Lấy cấu hình số luồng xử lý và bộ bảo vệ VRAM."""
    return get_worker_settings()


@app.post("/api/worker-settings")
async def api_save_worker_settings(payload: dict = Body(...)):
    """Lưu cấu hình số luồng (1 hoặc 2) và ngưỡng bảo vệ VRAM."""
    # Merge với cấu hình hiện tại: payload một phần (vd chỉ {concurrency}) không được
    # ghi đè ngầm các ngưỡng VRAM người dùng đã chỉnh (lỗi trượt mặc định 1500 -> 1800).
    current = get_worker_settings()
    concurrency = int(payload.get("concurrency", current.get("concurrency", 1)))
    auto_guard = bool(payload.get("auto_vram_guard", current.get("auto_vram_guard", True)))
    max_thresh = float(payload.get("max_vram_percent_threshold", current.get("max_vram_percent_threshold", 85.0)))
    min_free = float(payload.get("min_free_vram_mb", current.get("min_free_vram_mb", 1500.0)))
    return save_worker_settings(concurrency, auto_guard, max_thresh, min_free)


# ===== F13: TRUNG TÂM THÔNG BÁO · F1: THỐNG KÊ HIỆU SUẤT · F9: LỊCH CHẠY ĐÊM =====


@app.get("/api/notifications")
async def api_get_notifications(unread: bool = False, limit: int = 50):
    """Danh sách thông báo + số chưa đọc (poll nhẹ, đọc file JSON nhỏ)."""
    from notification_service import list_notifications, unread_count

    items = await asyncio.to_thread(list_notifications, unread, limit)
    return {"items": items, "unread_count": await asyncio.to_thread(unread_count)}


@app.post("/api/notifications/mark-read")
async def api_mark_notifications_read(payload: dict = Body(...)):
    from notification_service import mark_read

    ids = payload.get("ids")
    changed = await asyncio.to_thread(
        mark_read, ids if isinstance(ids, list) else None, bool(payload.get("all"))
    )
    return {"marked": changed}


@app.post("/api/notifications/test")
async def api_test_notification():
    from notification_service import push

    return await asyncio.to_thread(
        push, "info", "Thông báo thử nghiệm",
        "Nếu bạn thấy mục này, Trung tâm Thông báo hoạt động bình thường.", "test",
    )


@app.get("/api/stats/summary")
async def api_get_stats_summary(days: int = 14):
    """F1 — tổng hợp hiệu suất pipeline từ video_stats.json."""
    from stats_service import get_summary

    return await asyncio.to_thread(get_summary, days)


def _schedule_file() -> Path:
    return Path(WORKSPACE) / "control" / "schedule.json"


@app.get("/api/schedule")
async def api_get_schedule():
    """F9 — đọc lịch chạy đêm hiện tại."""
    f = _schedule_file()
    if not f.is_file():
        return {"active": False}
    try:
        data = json.loads(await asyncio.to_thread(f.read_text, encoding="utf-8"))
        data["active"] = True
        return data
    except Exception:
        return {"active": False}


@app.post("/api/schedule")
async def api_schedule_night_batch(payload: dict = Body(...)):
    """F9 — lên lịch tự động chạy hàng đợi vào giờ HH:MM (một lần)."""
    import re
    import time

    t = str(payload.get("time") or "02:00").strip()
    if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", t):
        raise HTTPException(status_code=400, detail="Giờ lên lịch không hợp lệ (định dạng HH:MM, ví dụ 02:00).")
    mode = str(payload.get("video_mode") or "AUTO").upper()
    data = {"time": t, "video_mode": mode, "created_at": time.time(), "last_fired_date": ""}
    f = _schedule_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(f.write_text, json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"active": True, **data}


@app.delete("/api/schedule")
async def api_cancel_schedule():
    f = _schedule_file()
    try:
        await asyncio.to_thread(f.unlink)
    except FileNotFoundError:
        pass
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Không hủy được lịch: {e}")
    return {"active": False}


async def _night_batch_watcher():
    """F9 — theo dõi schedule.json; đến giờ thì kích hoạt batch (một lần, tự hủy nếu trễ > 30 phút)."""
    from datetime import datetime

    while True:
        try:
            await asyncio.sleep(30)
            f = _schedule_file()
            if not f.is_file():
                continue
            data = json.loads(await asyncio.to_thread(f.read_text, encoding="utf-8"))
            hh, mm = str(data.get("time") or "02:00").split(":")[:2]
            now = datetime.now()
            target = now.replace(hour=int(hh) % 24, minute=int(mm) % 60, second=0, microsecond=0)
            today = now.strftime("%Y-%m-%d")
            if data.get("last_fired_date") == today:
                continue
            if now < target:
                continue
            from notification_service import push

            if (now - target).total_seconds() > 1800:
                # Máy ngủ/tắt quá 30 phút — hủy lịch thay vì chạy đột ngột giữa ngày
                await asyncio.to_thread(f.unlink)
                await asyncio.to_thread(
                    push, "warning", "Đã lỡ lịch chạy đêm",
                    f"Quá giờ {data.get('time')} hơn 30 phút, lịch đã tự hủy để chạy an toàn.", "schedule",
                )
                continue
            if not job_tracker.get_status().get("active"):
                await asyncio.to_thread(
                    push, "info", "Kích hoạt lịch chạy đêm",
                    f"Đến giờ {data.get('time')} — bắt đầu xử lý hàng đợi.", "schedule",
                )
                await api_run_batch(None, video_mode=data.get("video_mode") or "AUTO")
                data["last_fired_date"] = today
                await asyncio.to_thread(f.write_text, json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Lỗi trong night batch watcher: %s", exc)
            await asyncio.sleep(10)


# ===== GĐ3: F7 GLOSSARY LEARNING · F10 DUPLICATE GUARD · F12 WORKSPACE · F14 DISK =====


@app.get("/api/glossary/suggestions")
async def api_get_glossary_suggestions(min_count: int = 3):
    """F7 — các cặp sửa tay lặp lại >= min_count lần, chờ người dùng duyệt vào Glossary."""
    from glossary_learning_service import get_suggestions

    return {"items": await asyncio.to_thread(get_suggestions, min_count)}


@app.post("/api/glossary/suggestions/accept")
async def api_accept_glossary_suggestion(payload: dict = Body(...)):
    from glossary_learning_service import accept_suggestion

    try:
        res = await asyncio.to_thread(
            accept_suggestion, payload.get("before", ""), payload.get("after", "")
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return res


@app.post("/api/glossary/suggestions/dismiss")
async def api_dismiss_glossary_suggestion(payload: dict = Body(...)):
    from glossary_learning_service import dismiss_suggestion

    return await asyncio.to_thread(
        dismiss_suggestion, payload.get("before", ""), payload.get("after", "")
    )


@app.get("/api/phoi/duplicate-check")
async def api_phoi_duplicate_check(filename: str):
    """F10 — kiểm tra video phôi có trùng nội dung với video đã xử lý/tải lên trước đây."""
    from duplicate_guard import check_path

    clean = filename.strip()
    if not clean or clean in (".", "..") or clean != Path(clean).name:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    target = get_input_dir() / clean
    if not target.is_file():
        return {"duplicate": False}
    hit = await asyncio.to_thread(check_path, target)
    return {"duplicate": bool(hit), "info": hit}


@app.get("/api/workspace/usage")
async def api_workspace_usage():
    """F12 — dung lượng từng job trong workspace (chạy quét nặng trong thread)."""
    from workspace_usage_service import list_job_usage

    return await asyncio.to_thread(list_job_usage)


@app.post("/api/workspace/cleanup")
async def api_workspace_cleanup(payload: dict = Body(...)):
    """F12 — dọn stems (giữ SRT/dubbing để còn sửa câu) hoặc xóa cả job dir."""
    from workspace_usage_service import cleanup_jobs

    names = payload.get("jobs")
    mode = str(payload.get("mode") or "stems")
    if mode not in ("stems", "full"):
        raise HTTPException(status_code=400, detail="Chế độ dọn không hợp lệ (stems | full)")
    if not isinstance(names, list) or not names:
        raise HTTPException(status_code=400, detail="Danh sách job cần dọn đang rỗng")
    active: list = []
    try:
        st = job_tracker.get_status()
        active.append(st.get("video_name") or "")
        for w in (st.get("workers") or {}).values():
            if isinstance(w, dict):
                active.append(w.get("video_name") or "")
    except Exception:
        pass
    return await asyncio.to_thread(cleanup_jobs, names, mode, active)


@app.get("/api/disk-usage")
async def api_disk_usage():
    """F14 — dung lượng còn trống của các ổ chứa input/workspace/output."""
    import shutil as _shutil

    def _probe(label: str, p) -> dict:
        try:
            u = _shutil.disk_usage(str(p))
            return {"label": label, "path": str(p), "free_gb": round(u.free / 2**30, 1), "total_gb": round(u.total / 2**30, 1)}
        except Exception:
            return {"label": label, "path": str(p), "free_gb": None, "total_gb": None}

    def _all():
        return [
            _probe("input", get_input_dir()),
            _probe("workspace", Path(WORKSPACE)),
            _probe("output", get_output_dir()),
        ]

    return {"drives": await asyncio.to_thread(_all)}


# ===== GĐ4: F16 TÌM KIẾM TOÀN CỤC · F18 XUẤT/NHẬP CẤU HÌNH =====


@app.get("/api/search")
async def api_global_search(q: str = ""):
    """F16 — tìm kiếm toàn cục: video phôi, thành phẩm, preset, glossary, lịch sử (fold tiếng Việt)."""
    import unicodedata

    query = q.strip()
    if len(query) < 2:
        return {"groups": []}

    def _fold(s) -> str:
        return "".join(
            c for c in unicodedata.normalize("NFD", str(s).lower().replace("đ", "d"))
            if unicodedata.category(c) != "Mn"
        )

    needle = _fold(query)

    def _match(name) -> bool:
        return needle in _fold(name)

    def _run() -> list:
        groups = []
        try:
            root = get_input_dir()
            files = [e.name for e in root.iterdir() if e.is_file()] if root.is_dir() else []
            hits = [n for n in files if _match(n)][:5]
            if hits:
                groups.append({"id": "phoi", "label": "Video Phôi", "items": [{"title": n} for n in hits]})
        except Exception:
            pass
        try:
            root = get_output_dir()
            files = [e.name for e in root.iterdir() if e.is_file()] if root.is_dir() else []
            hits = [n for n in files if _match(n)][:5]
            if hits:
                groups.append({"id": "banve", "label": "Thành phẩm", "items": [{"title": n} for n in hits]})
        except Exception:
            pass
        try:
            from preset_service import list_presets

            hits = [p.get("name", "?") for p in list_presets(tool="v1") if _match(p.get("name", ""))][:5]
            if hits:
                groups.append({"id": "preset", "label": "Preset cấu hình", "items": [{"title": n} for n in hits]})
        except Exception:
            pass
        try:
            from ai.translation import load_custom_glossary

            gl = load_custom_glossary(WORKSPACE)
            hits = [f"{k} → {v}" for k, v in gl.items() if _match(k) or _match(v)][:5]
            if hits:
                groups.append({"id": "glossary", "label": "Glossary", "items": [{"title": n} for n in hits]})
        except Exception:
            pass
        try:
            hist = job_tracker.get_status().get("history", [])
            hits = [h.get("video_name", "?") for h in hist if _match(h.get("video_name", ""))][:5]
            if hits:
                groups.append({"id": "history", "label": "Lịch sử đã xử lý", "items": [{"title": n} for n in hits]})
        except Exception:
            pass
        return groups

    return {"groups": await asyncio.to_thread(_run)}


@app.get("/api/config/export")
async def api_export_dashboard_config():
    """F18 — xuất toàn bộ cấu hình Dashboard ra 1 file JSON (presets, drafts, glossary, mixer, worker, voice, flags)."""
    import time as _time

    def _collect() -> dict:
        out = {"format": "toolv1-dashboard-config", "version": 1, "exported_at": _time.time()}
        sections = {
            "presets": lambda: __import__("preset_service").list_presets(tool="v1"),
            "video_drafts": lambda: __import__("job_config_service").list_all_drafts(),
            "glossary": lambda: __import__("ai.translation", fromlist=["x"]).load_custom_glossary(WORKSPACE),
            "audio_settings": get_audio_settings,
            "worker_settings": get_worker_settings,
            "voice_auto": lambda: {
                "config": __import__("ai.v1_auto_voice", fromlist=["x"]).get_auto_voice_config(WORKSPACE),
                "enabled": __import__("ai.v1_auto_voice", fromlist=["x"]).get_auto_voice_enabled(WORKSPACE),
            },
            "feature_flags": lambda: __import__("v1_feature_flags").get_feature_flags(WORKSPACE),
        }
        for key, fn in sections.items():
            try:
                out[key] = fn()
            except Exception:
                continue
        return out

    return await asyncio.to_thread(_collect)


@app.post("/api/config/import")
async def api_import_dashboard_config(payload: dict = Body(...)):
    """F18 — phục hồi cấu hình từ file JSON đã xuất. Chỉ nhận file đúng định dạng, áp dụng qua các hàm ghi chuẩn."""
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("format") != "toolv1-dashboard-config":
        raise HTTPException(status_code=400, detail="File cấu hình không hợp lệ (không phải bản xuất của Tool V1 Dashboard).")

    def _apply() -> dict:
        applied, skipped = [], []

        presets = data.get("presets")
        if isinstance(presets, list) and presets:
            try:
                from preset_service import create_preset

                cnt = 0
                for p in presets:
                    if isinstance(p, dict):
                        try:
                            create_preset(p)
                            cnt += 1
                        except Exception:
                            continue
                applied.append(f"presets ({cnt} mục)")
            except Exception:
                skipped.append("presets")

        drafts = data.get("video_drafts")
        if isinstance(drafts, dict) and drafts:
            try:
                import job_config_service as jcs

                cnt = 0
                for name, d in drafts.items():
                    if isinstance(d, dict):
                        try:
                            jcs.save_video_draft(str(name), d)
                            cnt += 1
                        except Exception:
                            continue
                applied.append(f"bản nháp video ({cnt} mục)")
            except Exception:
                skipped.append("video_drafts")

        gl = data.get("glossary")
        if isinstance(gl, dict) and gl:
            try:
                from ai.translation import save_custom_glossary

                save_custom_glossary(entries=gl, merge=True, workspace=WORKSPACE)
                applied.append(f"glossary ({len(gl)} mục)")
            except Exception:
                skipped.append("glossary")

        aud = data.get("audio_settings")
        if isinstance(aud, dict) and aud:
            try:
                save_audio_settings(
                    aud.get("bgm_volume_db", -2.0),
                    aud.get("dubbing_volume_db", 1.0),
                    separation_mode=aud.get("separation_mode"),
                    ducking_mode=aud.get("ducking_mode"),
                    script_mode=aud.get("script_mode"),
                )
                applied.append("mixer âm thanh")
            except Exception:
                skipped.append("audio_settings")

        ws = data.get("worker_settings")
        if isinstance(ws, dict) and ws:
            try:
                save_worker_settings(
                    int(ws.get("concurrency", 1)),
                    bool(ws.get("auto_vram_guard", True)),
                    float(ws.get("max_vram_percent_threshold", 85.0)),
                    float(ws.get("min_free_vram_mb", 1500.0)),
                )
                applied.append("worker/VRAM")
            except Exception:
                skipped.append("worker_settings")

        va = data.get("voice_auto")
        if isinstance(va, dict) and isinstance(va.get("config"), dict):
            try:
                from ai.v1_auto_voice import set_auto_voice_config

                cfg = va["config"]
                set_auto_voice_config(
                    enabled=va.get("enabled") if va.get("enabled") is not None else cfg.get("enabled"),
                    female_voice_id=cfg.get("female_voice_id"),
                    male_voice_id=cfg.get("male_voice_id"),
                    dual_voice=cfg.get("dual_voice"),
                    updated_by="config-import",
                    workspace=WORKSPACE,
                )
                applied.append("auto voice")
            except Exception:
                skipped.append("voice_auto")

        flags = data.get("feature_flags")
        if isinstance(flags, dict) and flags:
            try:
                from v1_feature_flags import set_feature_flags

                set_feature_flags(flags, workspace_path=WORKSPACE)
                applied.append("feature flags")
            except Exception:
                skipped.append("feature_flags")

        return {"status": "ok", "applied": applied, "skipped": skipped}

    return await asyncio.to_thread(_apply)



# ===== AUTO VOICE MODE (CODEX PLAN - SINGLE VOICE PER VIDEO) =====
from ai.v1_auto_voice import (
    get_auto_voice_config,
    set_auto_voice_config,
    get_auto_voice_enabled,
    set_auto_voice_enabled,
    get_auto_voice_mode,
    get_manual_voice_info,
    VOICE_FEMALE_ID,
    VOICE_MALE_ID,
)
import voice_selection

@app.get("/api/voice-auto")
async def api_get_voice_auto():
    """Lấy trạng thái và cấu hình chế độ tự động nhận diện giọng nói đầu video (Tool V1)."""
    cfg = get_auto_voice_config(WORKSPACE)
    manual_info = get_manual_voice_info(WORKSPACE)
    catalog = voice_selection.catalog()

    female_voice = next((v for v in catalog if v.get("id") == cfg["female_voice_id"]), None)
    male_voice = next((v for v in catalog if v.get("id") == cfg["male_voice_id"]), None)

    effective_mode = "dual" if (cfg["enabled"] and cfg.get("dual_voice", False)) else ("auto_single" if cfg["enabled"] else "manual")
    return {
        "enabled": cfg["enabled"],
        "mode": "auto" if cfg["enabled"] else "manual",
        "effective_mode": effective_mode,
        "dual_voice": bool(cfg.get("dual_voice", False)),
        "female_voice_id": cfg["female_voice_id"],
        "male_voice_id": cfg["male_voice_id"],
        "female_voice_label": female_voice["label"] if female_voice else cfg["female_voice_id"],
        "male_voice_label": male_voice["label"] if male_voice else cfg["male_voice_id"],
        "manual_voice": manual_info,
        "voices": catalog,
    }


@app.post("/api/voice-auto")
async def api_set_voice_auto(payload: dict = Body(...)):
    current_cfg = get_auto_voice_config(WORKSPACE)
    catalog = voice_selection.catalog()
    valid_voice_ids = {v["id"] for v in catalog}

    def _parse_bool_val(val, default):
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        s = str(val).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        return default

    target_enabled = _parse_bool_val(payload.get("enabled"), current_cfg["enabled"]) if "enabled" in payload else current_cfg["enabled"]
    target_dual = _parse_bool_val(payload.get("dual_voice"), current_cfg.get("dual_voice", False)) if "dual_voice" in payload else current_cfg.get("dual_voice", False)

    if "mode" in payload:
        mode_val = str(payload["mode"]).strip().lower()
        if mode_val in ("manual", "single_manual"):
            target_enabled = False
            target_dual = False
        elif mode_val in ("auto", "auto_single", "single_auto"):
            target_enabled = True
            target_dual = False
        elif mode_val in ("dual", "dual_voice"):
            target_enabled = True
            target_dual = True

    raw_female = payload.get("female_voice_id") if "female_voice_id" in payload else payload.get("female_voice")
    raw_male = payload.get("male_voice_id") if "male_voice_id" in payload else payload.get("male_voice")

    target_female_id = current_cfg["female_voice_id"]
    if raw_female is not None:
        raw_f_str = str(raw_female).strip()
        matched_f = next((v for v in catalog if v.get("id") == raw_f_str or v.get("param") == raw_f_str), None)
        if matched_f:
            target_female_id = matched_f["id"]
        elif raw_f_str in valid_voice_ids:
            target_female_id = raw_f_str
        else:
            return {
                "status": "error",
                "message": f"Giọng nữ '{raw_female}' không hợp lệ hoặc không tồn tại trong danh mục.",
                "enabled": current_cfg["enabled"],
                "dual_voice": bool(current_cfg.get("dual_voice", False)),
                "female_voice_id": current_cfg["female_voice_id"],
                "male_voice_id": current_cfg["male_voice_id"],
            }

    target_male_id = current_cfg["male_voice_id"]
    if raw_male is not None:
        raw_m_str = str(raw_male).strip()
        matched_m = next((v for v in catalog if v.get("id") == raw_m_str or v.get("param") == raw_m_str), None)
        if matched_m:
            target_male_id = matched_m["id"]
        elif raw_m_str in valid_voice_ids:
            target_male_id = raw_m_str
        else:
            return {
                "status": "error",
                "message": f"Giọng nam '{raw_male}' không hợp lệ hoặc không tồn tại trong danh mục.",
                "enabled": current_cfg["enabled"],
                "dual_voice": bool(current_cfg.get("dual_voice", False)),
                "female_voice_id": current_cfg["female_voice_id"],
                "male_voice_id": current_cfg["male_voice_id"],
            }

    if target_dual and target_female_id == target_male_id:
        logger.warning(
            "api_set_voice_auto: Dual voice bật nhưng cả vai nam và nữ cùng chọn giọng %s. Tự động điều chỉnh để tách vai.",
            target_female_id,
        )
        if target_male_id == VOICE_FEMALE_ID or "female" in target_male_id.lower() or "mai" in target_male_id.lower():
            target_male_id = VOICE_MALE_ID
        else:
            target_female_id = VOICE_FEMALE_ID

    success = set_auto_voice_config(
        enabled=target_enabled,
        female_voice_id=target_female_id,
        male_voice_id=target_male_id,
        dual_voice=target_dual,
        updated_by="dashboard",
        workspace=WORKSPACE
    )
    if not success:
        logger.error("Không thể lưu cấu hình auto voice vào thư mục control")
        raise HTTPException(status_code=500, detail="Lỗi hệ thống: Không thể lưu file cấu hình Auto Voice.")

    cfg = get_auto_voice_config(WORKSPACE)
    manual_info = get_manual_voice_info(WORKSPACE)
    catalog = voice_selection.catalog()
    female_voice = next((v for v in catalog if v.get("id") == cfg["female_voice_id"]), None)
    male_voice = next((v for v in catalog if v.get("id") == cfg["male_voice_id"]), None)

    msg = "Đã cập nhật cấu hình giọng"
    if "mode" in payload:
        if mode_val == "manual":
            msg = "Đã chuyển sang chế độ 1 giọng thủ công"
        elif mode_val in ("auto", "auto_single", "single_auto"):
            msg = "Đã chuyển sang chế độ Tự chọn 1 giọng (Auto Single)"
        elif mode_val in ("dual", "dual_voice"):
            msg = "Đã chuyển sang chế độ Phân vai Nam & Nữ (Dual Voice)"
    elif "dual_voice" in payload and len(payload) == 1:
        msg = "Đã BẬT phân vai Nam & Nữ trong cùng video" if target_dual else "Đã TẮT phân vai Nam/Nữ (Mặc định 1 giọng cả video)"
    elif "enabled" in payload and len(payload) == 1:
        msg = "Đã BẬT tự động nhận diện giọng đầu video" if target_enabled else "Đã TẮT tự động nhận diện (Dùng giọng thủ công)"

    effective_mode = "dual" if (cfg["enabled"] and cfg.get("dual_voice", False)) else ("auto_single" if cfg["enabled"] else "manual")
    return {
        "status": "ok",
        "enabled": cfg["enabled"],
        "mode": "auto" if cfg["enabled"] else "manual",
        "effective_mode": effective_mode,
        "dual_voice": bool(cfg.get("dual_voice", False)),
        "female_voice_id": cfg["female_voice_id"],
        "male_voice_id": cfg["male_voice_id"],
        "female_voice_label": female_voice["label"] if female_voice else cfg["female_voice_id"],
        "male_voice_label": male_voice["label"] if male_voice else cfg["male_voice_id"],
        "manual_voice": manual_info,
        "voices": catalog,
        "message": msg,
    }

# ===== V1 UPGRADE SML: ROUTER MODES, FEATURE FLAGS, MANIFEST & QC =====
@app.get("/api/v1/router-modes")
async def api_get_router_modes():
    """Lấy danh sách các VideoMode và ngưỡng định tuyến."""
    try:
        from v1_video_router import get_router_thresholds, VideoMode
        return {
            "modes": [m.value for m in VideoMode],
            "thresholds": get_router_thresholds(),
            "default_mode": "AUTO",
        }
    except Exception as e:
        return {"modes": ["AUTO", "SHORT", "MEDIUM", "LONG", "CUSTOM"], "error": str(e)}


@app.get("/api/v1/feature-flags")
async def api_get_feature_flags():
    """Lấy trạng thái các cờ tính năng nâng cấp."""
    try:
        from v1_feature_flags import get_feature_flags
        flags = get_feature_flags(WORKSPACE)
        try:
            from ai.v1_qwen_asr_adapter import check_qwen_readiness
            ready, reason = check_qwen_readiness(ignore_flag=True)
            flags["QWEN_ASR_READY"] = bool(ready)
            flags["QWEN_ASR_READINESS_REASON"] = reason
        except Exception as readiness_error:
            flags["QWEN_ASR_READY"] = False
            flags["QWEN_ASR_READINESS_REASON"] = str(readiness_error)
        try:
            from ai.v1_sensevoice_adapter import check_sensevoice_readiness
            sv_ready, sv_reason = check_sensevoice_readiness(ignore_flag=True)
            flags["SENSEVOICE_ASR_READY"] = bool(sv_ready)
            flags["SENSEVOICE_ASR_READINESS_REASON"] = sv_reason
        except Exception as sv_err:
            flags["SENSEVOICE_ASR_READY"] = False
            flags["SENSEVOICE_ASR_READINESS_REASON"] = str(sv_err)
        return flags
    except Exception:
        from v1_speech_guard import natural_speed_limit
        return {
            "ENABLE_QWEN_ASR": os.getenv("ENABLE_QWEN_ASR", "false").lower() in ("true", "1", "yes"),
            "V1_ASR_CHUNKING": os.getenv("V1_ASR_CHUNKING", "true").lower() in ("true", "1", "yes"),
            "V1_SMART_SKIP_OCR": os.getenv("V1_SMART_SKIP_OCR", "true").lower() in ("true", "1", "yes"),
            "V1_QC_POLICY": os.getenv("V1_QC_POLICY", "REPORT_ONLY"),
            "V1_MAX_NATURAL_SPEED": natural_speed_limit(),
            "V1_TTS_WORKERS": int(os.getenv("V1_TTS_WORKERS", "6")),
            "V1_USE_ORCHESTRATOR": True,
        }


@app.post("/api/v1/feature-flags")
async def api_set_feature_flags(payload: dict = Body(...)):
    """Cập nhật trạng thái các cờ tính năng nâng cấp (lưu vào biến môi trường & file cấu hình)."""
    try:
        requested_model = str(payload.get("V1_ASR_MODEL", "")).strip().lower()
        wants_qwen = (payload.get("ENABLE_QWEN_ASR") is True
                      or requested_model in ("qwen3_asr", "qwen", "qwen3")
                      or payload.get("V1_SHORT_ASR_MODEL") == "qwen3_asr"
                      or payload.get("V1_LONG_ASR_MODEL") == "qwen3_asr")
        if wants_qwen:
            from ai.v1_qwen_asr_adapter import check_qwen_readiness
            ready, reason = check_qwen_readiness(ignore_flag=True)
            if not ready:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "error",
                        "code": "QWEN_NOT_READY",
                        "message": reason,
                    },
                )
        wants_sensevoice = (
            requested_model in ("whisper_sensevoice", "whisper_sensevoice_hybrid", "hybrid", "sensevoice_small", "sensevoice")
            or payload.get("V1_SHORT_ASR_MODEL") in ("whisper_sensevoice", "sensevoice_small")
            or payload.get("V1_LONG_ASR_MODEL") in ("whisper_sensevoice", "sensevoice_small")
        )
        if wants_sensevoice:
            from ai.v1_sensevoice_adapter import check_sensevoice_readiness
            sv_ready, sv_reason = check_sensevoice_readiness(ignore_flag=True)
            if not sv_ready:
                return JSONResponse(
                    status_code=409,
                    content={
                        "status": "error",
                        "code": "SENSEVOICE_NOT_READY",
                        "message": sv_reason,
                    },
                )
        from v1_feature_flags import set_feature_flags
        updated = set_feature_flags(payload, WORKSPACE)
        return {"status": "ok", "flags": updated}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("Lỗi cập nhật feature flags: %s", exc)
        raise HTTPException(status_code=500, detail="Không lưu được cấu hình") from exc


@app.get("/api/v1/job-manifest/{job_id}")
async def api_get_job_manifest(job_id: str):
    """Lấy manifest và khả năng resume của job."""
    try:
        from v1_checkpoint import ManifestManager
        mgr = ManifestManager(WORKSPACE)
        manifest = mgr.load_manifest(job_id)
        if not manifest:
            return {"found": False, "job_id": job_id}
        can_resume, resume_stage, plan = mgr.verify_manifest_resumability(job_id)
        return {
            "found": True,
            "manifest": manifest.to_dict(),
            "resumability": {
                "can_resume": can_resume,
                "resume_from_stage": resume_stage,
                "plan": plan,
            }
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


@app.get("/api/v1/qc-report/{job_id}")
async def api_get_qc_report(job_id: str):
    """Lấy báo cáo kiểm định Quality Gate của job."""
    qc_file = Path(WORKSPACE) / "bot_system" / "qc_reports" / f"{job_id}.qc.json"
    if not qc_file.is_file():
        return {"found": False, "job_id": job_id}
    try:
        data = json.loads(qc_file.read_text(encoding="utf-8"))
        return {"found": True, "report": data}
    except Exception as e:
        return {"found": False, "error": str(e)}


@app.get("/api/voice-preview-video/{voice_id}")
async def api_voice_preview_video(voice_id: str):
    filename = f"{_safe_preview_name(voice_id)}.mp4"
    candidates = [
        Path(WORKSPACE) / "bot_system" / "control" / "voice_previews" / filename,
        Path(WORKSPACE) / "control" / "voice_previews" / filename,
        Path(r"C:\tool v1\workspace\control\voice_previews") / filename,
        Path(r"C:\tool v1\workspace\bot_system\control\voice_previews") / filename,
    ]
    for p in candidates:
        if p.is_file():
            return FileResponse(p, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Không tìm thấy video mẫu cho giọng này.")


@app.get("/api/voice-preview-audio/{voice_id}")
async def api_voice_preview_audio(voice_id: str):
    clean_voice_id = _safe_preview_name(voice_id)
    for ext, media in ((".mp3", "audio/mpeg"), (".wav", "audio/wav")):
        filename = f"{clean_voice_id}{ext}"
        candidates = [
            Path(WORKSPACE) / "bot_system" / "control" / "voice_checks" / filename,
            Path(WORKSPACE) / "control" / "voice_checks" / filename,
            Path(r"C:\tool v1\workspace\control\voice_checks") / filename,
            Path(r"C:\tool v1\workspace\bot_system\control\voice_checks") / filename,
        ]
        for p in candidates:
            if p.is_file():
                return FileResponse(p, media_type=media)
    raise HTTPException(status_code=404, detail="Không tìm thấy audio mẫu cho giọng này.")



# ===== A2UI (AGENT-TO-USER INTERFACE) ISOLATED EXTENSION ENDPOINTS =====

@app.get("/a2ui", response_class=HTMLResponse)
async def serve_a2ui_studio():
    """Phục vụ giao diện A2UI Studio Playground (Thử nghiệm giao diện động AI)."""
    template_path = BASE_DIR / "templates" / "a2ui_studio.html"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read().replace("__REPLACE_TOKEN__", LOCAL_TOKEN)
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h2>A2UI Studio template not found.</h2>", status_code=404)


@app.get("/api/a2ui/catalog")
async def api_get_a2ui_catalog():
    """Trả về Component Catalog của A2UI."""
    from a2ui.catalog import MEDIA_COMPONENT_CATALOG
    return MEDIA_COMPONENT_CATALOG


@app.get("/api/a2ui/scenario/{name}")
async def api_get_a2ui_scenario(name: str):
    """Lấy luồng thông điệp A2UI theo kịch bản mẫu (multivoice, subtitles, rater)."""
    from a2ui.mock_agent import get_preset_scenario
    return {"messages": get_preset_scenario(name)}


@app.post("/api/a2ui/generate")
async def api_generate_a2ui(video_name: str = Form(...)):
    """Agent tự sinh cấu trúc A2UI cho 1 video cụ thể."""
    from a2ui.agent_generator import generate_a2ui_for_video
    video_path = str(_safe_media_path("phoi", video_name))
    messages = await asyncio.to_thread(generate_a2ui_for_video, video_path)
    return {"messages": messages}


@app.post("/api/a2ui/action")
async def api_handle_a2ui_action(payload: dict = Body(...)):
    """Xử lý callAgentFunction tương tác 2 chiều từ client."""
    function_name = payload.get("name", "")
    params = payload.get("parameters", {})
    call_id = payload.get("functionCallId", "")

    logger.info(f"[A2UI Action] Function: {function_name}, Params: {params}")

    # Xử lý các action mẫu
    result = {
        "status": "success",
        "message": f"Agent đã tiếp nhận và thực thi hàm '{function_name}' thành công!",
        "executed_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "received_params": params
    }

    return {
        "agentFunctionResponse": {
            "functionCallId": call_id,
            "result": result
        }
    }


try:
    from workflow_api import router as workflow_router
    app.include_router(workflow_router)
    logger.info("Mounted Studio Quy Trình workflow_router successfully.")
except Exception as _we:
    logger.warning("Failed to mount workflow_router: %s", _we)

try:
    from script_api import router as script_router
    app.include_router(script_router)
    logger.info("Mounted Studio Kịch Bản script_router successfully.")
except Exception as _se:
    logger.warning("Failed to mount script_router: %s", _se)

try:
    from phase_a_routes import router as phase_a_router
    app.include_router(phase_a_router)
    logger.info("Mounted Phase A Presets & Job Config router successfully.")
except Exception as _pe:
    logger.warning("Failed to mount phase_a_router: %s", _pe)

try:
    from phase_b_routes import router as phase_b_router
    app.include_router(phase_b_router)
    logger.info("Mounted Phase B Voice Preview router successfully.")
except Exception as _pbe:
    logger.warning("Failed to mount phase_b_router: %s", _pbe)

try:
    from phase_c_routes import router as phase_c_router
    app.include_router(phase_c_router)
    logger.info("Mounted Phase C Segment Editor router successfully.")
except Exception as _pce:
    logger.warning("Failed to mount phase_c_router: %s", _pce)

try:
    from phase_d_routes import router as phase_d_router
    app.include_router(phase_d_router)
    logger.info("Mounted Phase D Crash Recovery router successfully.")
except Exception as _pde:
    logger.warning("Failed to mount phase_d_router: %s", _pde)


@app.get("/api/v1/gemini-health")
async def api_v1_gemini_health():
    """Trả về trạng thái thời gian thực của Multi-Key Gemini Pool và Circuit Breaker."""
    from ai.v1_gemini_dispatcher import get_gemini_pool_health
    return get_gemini_pool_health()


@app.get("/api/v1/glossary")
async def api_v1_get_glossary():
    """Trả về từ điển thuật ngữ tùy chỉnh (Custom Glossary) và danh sách preset chuyên ngành."""
    from ai.translation import BUILTIN_GLOSSARY_PRESETS, load_custom_glossary
    return {
        "status": "ok",
        "entries": load_custom_glossary(WORKSPACE),
        "presets": BUILTIN_GLOSSARY_PRESETS,
    }


@app.post("/api/v1/glossary")
async def api_v1_save_glossary(payload: dict = Body(...)):
    """Lưu hoặc cập nhật từ điển thuật ngữ tùy chỉnh (Custom Glossary) theo chủ đề."""
    from ai.translation import BUILTIN_GLOSSARY_PRESETS, save_custom_glossary
    entries = payload.get("entries") if isinstance(payload.get("entries"), dict) else {}
    preset = payload.get("preset")
    merge = bool(payload.get("merge", True))
    if preset and preset not in BUILTIN_GLOSSARY_PRESETS:
        raise HTTPException(status_code=400, detail=f"Preset '{preset}' không tồn tại.")
    updated = save_custom_glossary(entries=entries, preset=preset, merge=merge, workspace=WORKSPACE)
    return {
        "status": "ok",
        "count": len(updated),
        "entries": updated,
    }




if __name__ == "__main__":
    import uvicorn, time
    port = int(os.getenv("AUTODUB_PORT", "8088"))
    for attempt in range(15):
        try:
            uvicorn.run(app, host="127.0.0.1", port=port)
            break
        except OSError as oe:
            if getattr(oe, "winerror", None) == 10048 or oe.errno == 10048:
                time.sleep(2)
                continue
            raise
