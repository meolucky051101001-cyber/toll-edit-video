from fastapi import FastAPI, Form, Body, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
import os
import asyncio
import datetime
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

# The API in this worktree is Tool V1 only, regardless of inherited variables.
os.environ["PIPELINE_MODE"] = "legacy"

from ai.transcription import extract_subtitles_whisper, save_srt
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video
from pipeline_v2.config import PipelineMode, PipelineSettings
import shared_state
import job_tracker

logger = logging.getLogger("main_api")

app = FastAPI()

ALLOWED_CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "AUTODUB_CORS_ORIGINS",
        "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088,null",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace"))
OUTPUT_DIR = os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
os.makedirs(WORKSPACE, exist_ok=True)

def get_input_dir() -> Path:
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
API_PROCESS_LOCK = asyncio.Lock()
BATCH_TASK_LOCK = asyncio.Lock()
BATCH_TASK: Optional[asyncio.Task] = None


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


def _read_log_tail(max_lines: int = 100) -> str:
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
        return "".join(handle.readlines()[-max_lines:])


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
async def api_generate_subtitles(video_path: str = Form(...), target_lang: str = Form("vi")):
    """Nhận đường dẫn file video trên máy, tạo phụ đề gốc và dịch."""
    try:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")

        # 1. Extract audio
        extract_audio_from_video(video_path, original_audio)

        # 2. Transcribe with Whisper
        srt_segments = extract_subtitles_whisper(original_audio, srt_original)

        # 3. Translate
        translated_segments = translate_subtitles(
            srt_segments,
            target_lang,
            api_key=GEMINI_API_KEY,
            video_path=video_path,
        )
        save_srt(translated_segments, srt_translated)

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


# ===== API: Xử lý full (Lồng tiếng + Xuất video) =====
@app.post("/api/process_video")
async def api_process_video(
    video_path: str = Form(...),
    target_lang: str = Form("vi"),
    voice_source: str = Form("edge"),
    voice_param: str = Form("vi-VN-HoaiMyNeural"),
    api_key: str = Form(""),
    font_name: str = Form("Arial"),
    font_color: str = Form("&H00FFFFFF"),
    font_weight: int = Form(1)
):
    """Xử lý full: Transcribe → Dịch → TTS → Mix Audio → Blur + Sub → Xuất video."""
    await API_PROCESS_LOCK.acquire()
    try:
        base_name = os.path.splitext(os.path.basename(video_path))[0]
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

        # 1. Extract audio
        extract_audio_from_video(video_path, original_audio)

        # 2. Transcribe
        srt_segments = extract_subtitles_whisper(original_audio, srt_original)
        vid_w, vid_h, main_y = await locate_v1_subtitles(video_path, srt_segments)

        # 3. Translate
        translated_segments = translate_subtitles(
            srt_segments,
            target_lang,
            api_key=(
                GEMINI_API_KEY
                if voice_source == "fpt"
                else (api_key or GEMINI_API_KEY)
            ),
            video_path=video_path,
        )
        save_srt(translated_segments, srt_translated)

        # 4. Generate TTS dubbing
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir,
            voice_source=voice_source,
            voice_param=voice_param,
            api_key=api_key
        )

        # 5. Mix audio (original + dubbing)
        mix_audio_pydub(original_audio, dubbing_audio_files, mixed_audio)

        # 6. Final render: Blur + Subtitles + Audio → Output video
        from ass_utils import generate_ass_file
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(
            generate_ass_file, translated_segments, [], ass_path,
            play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y,
            font_name=font_name, font_color=font_color, font_weight=font_weight,
        )
        rendered = process_video(
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
    # Search in workspace subdirectories
    for root, dirs, files in os.walk(WORKSPACE):
        if filename in files:
            return FileResponse(os.path.join(root, filename), media_type="video/mp4", filename=filename)
    return {"status": "error", "message": "File not found"}


# ===== API: Tải video từ URL + Xử lý tự động =====
@app.post("/api/process_url")
async def api_process_url(
    url: str = Form(...),
    target_lang: str = Form("vi"),
    voice_source: str = Form("edge"),
    voice_param: str = Form("vi-VN-HoaiMyNeural"),
    api_key: str = Form(""),
    font_name: str = Form("Arial"),
    font_color: str = Form("&H00FFFFFF"),
    font_weight: int = Form(1)
):
    """Tải video từ URL (Xiaohongshu, TikTok, YouTube...) rồi xử lý toàn bộ."""
    await API_PROCESS_LOCK.acquire()
    try:
        import time

        download_dir = os.path.join(WORKSPACE, "downloads")
        os.makedirs(download_dir, exist_ok=True)

        # Nanosecond prefix prevents stale-file collisions between fast retries.
        timestamp = str(time.time_ns())
        # Use the shared no-watermark downloader, including 206 + ffprobe checks.
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

        # Extract audio
        extract_audio_from_video(video_path, original_audio)

        # Transcribe
        srt_segments = extract_subtitles_whisper(original_audio, srt_original)
        vid_w, vid_h, main_y = await locate_v1_subtitles(video_path, srt_segments)

        # Translate
        translated_segments = translate_subtitles(
            srt_segments,
            target_lang,
            api_key=(
                GEMINI_API_KEY
                if voice_source == "fpt"
                else (api_key or GEMINI_API_KEY)
            ),
            video_path=video_path,
        )
        save_srt(translated_segments, srt_translated)

        # Subtitles for response
        subtitles = []
        for seg in translated_segments:
            subtitles.append({
                "index": seg.index,
                "start": seg.start.total_seconds(),
                "end": seg.end.total_seconds(),
                "content": seg.content
            })

        # TTS
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir,
            voice_source=voice_source,
            voice_param=voice_param,
            api_key=api_key
        )

        # Mix audio
        mix_audio_pydub(original_audio, dubbing_audio_files, mixed_audio)

        # Final render
        from ass_utils import generate_ass_file
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(
            generate_ass_file, translated_segments, [], ass_path,
            play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y,
            font_name=font_name, font_color=font_color, font_weight=font_weight,
        )
        rendered = process_video(
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
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h2>Dashboard template not found.</h2>", status_code=404)


@app.get("/api/status")
async def api_get_status():
    """Trả về trạng thái tiến độ thời gian thực của tác vụ hiện tại."""
    status = job_tracker.get_status()
    status["stop_requested"] = bool(
        status.get("stop_requested")
        or getattr(shared_state, "stop_requested", False)
    )
    return status


@app.get("/api/queue")
async def api_get_queue():
    import json
    path = Path(WORKSPACE) / "telegram_queue.json"
    if not path.is_file():
        return {"items": [], "available": False, "message": "Chưa kết nối hàng đợi Telegram. Cần khởi động lại bot để bật tính năng."}
    try:
        data = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
        alive = job_tracker._pid_is_running(int(data.get("pid") or 0))
        return {**data, "available": alive, "message": "" if alive else "Bot đã dừng; danh sách là bản ghi cuối cùng."}
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "Không đọc được hàng đợi Telegram")

@app.get("/api/phoi")
async def api_get_phoi():
    """Quét và trả về danh sách các video trong thư mục video phôi (D:\\video phôi)."""
    input_dir = get_input_dir()
    if not input_dir.exists():
        return {
            "exists": False,
            "path": str(input_dir),
            "files": [],
            "total_count": 0,
            "total_size_mb": 0,
        }

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
            if full_path.is_file() and f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_") and " (1)" not in f:
                size_b = full_path.stat().st_size
                total_size += size_b
                mtime_str = datetime.datetime.fromtimestamp(full_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                stem = full_path.stem

                if f == active_video and current_status.get("active"):
                    v_status = "running"
                    v_status_label = f"Đang chạy ({current_status.get('percent', 0)}%)"
                elif f"Dubbed_{stem}.mp4" in output_files:
                    v_status = "completed"
                    v_status_label = "Đã có bản vẽ"
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

    return {
        "exists": True,
        "path": str(input_dir),
        "files": files_data,
        "total_count": len(files_data),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


@app.get("/api/banve")
async def api_get_banve():
    """Quét và trả về danh sách các video thành phẩm trong D:\\banve."""
    output_dir = get_output_dir()
    if not output_dir.exists():
        return {
            "exists": False,
            "path": str(output_dir),
            "files": [],
            "total_count": 0,
            "total_size_mb": 0,
        }

    from batch_processor import SUPPORTED_EXTENSIONS
    files_data = []
    total_size = 0
    try:
        for f in sorted(os.listdir(output_dir), reverse=True):
            full_path = output_dir / f
            if full_path.is_file() and f.lower().endswith(SUPPORTED_EXTENSIONS):
                size_b = full_path.stat().st_size
                total_size += size_b
                mtime_str = datetime.datetime.fromtimestamp(full_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                files_data.append({
                    "name": f,
                    "size_mb": round(size_b / (1024 * 1024), 2),
                    "created": mtime_str,
                })
    except Exception as e:
        logger.error(f"Lỗi đọc thư mục bản vẽ: {e}")

    return {
        "exists": True,
        "path": str(output_dir),
        "files": files_data,
        "total_count": len(files_data),
        "total_size_mb": round(total_size / (1024 * 1024), 2),
    }


@app.post("/api/run-batch")
async def api_run_batch():
    """Kích hoạt chạy batch toàn bộ video phôi ngầm."""
    global BATCH_TASK
    async with BATCH_TASK_LOCK:
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

        from batch_processor import process_batch_folder

        async def batch_runner():
            try:
                await process_batch_folder(
                    input_dir, output_dir, job_id=job_id
                )
            except job_tracker.JobAlreadyRunningError as e:
                logger.warning("Từ chối batch trùng: %s", e)
            except Exception as e:
                logger.error("Lỗi chạy batch qua API: %s", e, exc_info=True)
                job_tracker.fail_batch(str(e), job_id=job_id)

        BATCH_TASK = asyncio.create_task(
            batch_runner(), name=f"autodub-batch-{job_id}"
        )

    return JSONResponse(
        status_code=202,
        content={
            "status": "started",
            "job_id": job_id,
            "message": "Đã bắt đầu xử lý toàn bộ video trong thư mục video phôi!",
        },
    )


@app.post("/api/stop-batch")
async def api_stop_batch():
    """Yêu cầu dừng tiến trình batch đang chạy."""
    status = job_tracker.get_status()
    if not status.get("active"):
        return JSONResponse(
            status_code=409,
            content={"status": "idle", "message": "Không có batch nào đang chạy."},
        )
    shared_state.stop_requested = True
    job_tracker.request_stop()
    return {"status": "stopping", "message": "Đã gửi lệnh dừng tiến trình xử lý!"}


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


# ===== A2UI (AGENT-TO-USER INTERFACE) ISOLATED EXTENSION ENDPOINTS =====

@app.get("/a2ui", response_class=HTMLResponse)
async def serve_a2ui_studio():
    """Phục vụ giao diện A2UI Studio Playground (Thử nghiệm giao diện động AI)."""
    template_path = BASE_DIR / "templates" / "a2ui_studio.html"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("AUTODUB_PORT", "8088"))
    uvicorn.run(app, host="127.0.0.1", port=port)
