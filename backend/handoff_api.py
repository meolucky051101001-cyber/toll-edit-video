# -*- coding: utf-8 -*-
"""handoff_api — nhận bộ phụ đề đã OCR + dịch từ SubExtract (127.0.0.1:8787),
lồng tiếng Việt, che phụ đề cứng ngôn ngữ gốc và xuất video bằng chính các hàm
mà pipeline Tool V1 đang dùng (generate_dubbing_audio, sync_and_clamp_subtitles,
generate_ass_file, mix_audio_pydub, process_video).

Không đụng V1Orchestrator: batch 8 bước, Quality Gate và bot Telegram đi theo đường
code cũ. Handoff giữ chung quyền xử lý với batch và khóa render của Studio.
"""
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

logger = logging.getLogger("handoff_api")

router = APIRouter()

_VERSION = 1
_HANDOFF_JOBS: Dict[str, Dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 7200.0
_RENDER_QUEUE = deque()
_QUEUE_WORKER: Optional[threading.Thread] = None
_MAX_PENDING = 64
_HANDOFF_STEM = re.compile(r"subextract_[0-9a-f]{32}")
_ACTIVE = {"queued", "running", "cancelling"}
_MAX_CUES = 4000
_DEFAULT_DUCK_DB = -14.0
_DEFAULT_DUB_DB = 1.0
_FALLBACK_MAIN_Y = 0.88
_DEFAULT_SUBEXTRACT_JOBS = Path(
    r"C:\Users\admin\Documents\Qoder\2026-10-29\6b462a65\data\jobs"
)


def _wf():
    import workflow_api
    return workflow_api


class HandoffCancelled(RuntimeError):
    pass


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _allowed_roots() -> List[Path]:
    wf = _wf()
    candidates: List[Path] = [Path(os.getenv("SUBEXTRACT_JOBS_DIR", str(_DEFAULT_SUBEXTRACT_JOBS)))]
    for key in ("AUTODUB_WORKSPACE", "AUTODUB_INPUT_DIR", "AUTODUB_OUTPUT_DIR"):
        value = os.getenv(key)
        if value:
            candidates.append(Path(value))
    candidates += [wf.WORKSPACE, wf.INPUT_DIR, wf.DOWNLOADS_DIR, wf.OUTPUT_DIR]
    for extra in os.getenv("V1_HANDOFF_ALLOWED_DIRS", "").split(os.pathsep):
        if extra.strip():
            candidates.append(Path(extra.strip()))
    roots: List[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _resolve_source_video(raw: Any) -> Path:
    text = str(raw or "").strip().strip('"')
    if not text:
        raise HTTPException(status_code=400, detail="Thiếu đường dẫn video nguồn của SubExtract.")
    candidate = Path(text)
    if not candidate.is_absolute():
        raise HTTPException(status_code=400, detail="Đường dẫn video nguồn phải là đường dẫn tuyệt đối.")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Không đọc được đường dẫn video: {exc}") from exc
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"Không thấy video nguồn: {resolved.name}")
    if resolved.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv"):
        raise HTTPException(status_code=400, detail="Video nguồn không đúng định dạng hỗ trợ.")
    if not any(_is_inside(resolved, root) for root in _allowed_roots()):
        raise HTTPException(
            status_code=400,
            detail="Video nguồn nằm ngoài các thư mục được phép nhận bản giao phụ đề.",
        )
    return resolved


def _clamp_box(raw: Any) -> Optional[List[float]]:
    if raw is None:
        return None
    if isinstance(raw, dict):
        values = [raw.get("x_pct"), raw.get("y_pct"), raw.get("max_x_pct"), raw.get("max_y_pct")]
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        values = list(raw)
    else:
        return None
    if any(value is None for value in values):
        return None
    x1, y1, x2, y2 = (_num(v) for v in values)
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    x1 = min(max(x1, 0.0), 0.995)
    y1 = min(max(y1, 0.0), 0.995)
    x2 = min(max(x2, x1 + 0.005), 1.0)
    y2 = min(max(y2, y1 + 0.005), 1.0)
    return [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)]


def _normalize_cues(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="Danh sách phụ đề nhận được đang trống.")
    if len(raw) > _MAX_CUES:
        raise HTTPException(status_code=400, detail=f"Vượt quá giới hạn {_MAX_CUES} câu phụ đề.")
    cues: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = max(0.0, _num(item.get("start")))
        end = _num(item.get("end"), start + 2.0)
        if end <= start:
            end = start + 0.25
        text = str(item.get("text") or "").strip()
        trans = str(item.get("trans") or item.get("translation") or "").strip()
        if not text and not trans:
            continue
        cues.append({
            "index": len(cues) + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "trans": trans,
            "box": _clamp_box(item.get("box") or item.get("region_box")),
        })
    if not cues:
        raise HTTPException(status_code=400, detail="Không có câu phụ đề hợp lệ nào.")
    return cues


def _band_box(main_y_pct: float) -> List[float]:
    half = 0.045
    return _clamp_box([0.04, main_y_pct - half, 0.96, main_y_pct + half]) or [0.04, 0.8, 0.96, 0.96]


def _main_y_pct(cues: List[Dict[str, Any]], requested: Any) -> float:
    value = _num(requested, -1.0)
    if 0.05 <= value <= 0.99:
        return round(value, 4)
    centers = sorted((c["box"][1] + c["box"][3]) / 2.0 for c in cues if c["box"])
    if centers:
        return round(centers[len(centers) // 2], 4)
    return _FALLBACK_MAIN_Y


def _srt_text(entries: List[Dict[str, Any]], field: str) -> str:
    wf = _wf()
    blocks = []
    number = 1
    for cue in entries:
        text = str(cue.get(field) or "").strip()
        if not text:
            continue
        blocks.append(
            f"{number}\n{wf.format_srt_timestamp(cue['start'])} --> {wf.format_srt_timestamp(cue['end'])}\n{text}\n"
        )
        number += 1
    return "\n".join(blocks)


def _run(cmd: List[str], timeout: int, job_id: Optional[str] = None) -> subprocess.CompletedProcess:
    popen_kwargs: Dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(cmd, **popen_kwargs)
    with _JOBS_LOCK:
        job = _HANDOFF_JOBS.get(job_id) if job_id else None
        if job is not None:
            job["proc"] = process
            cancelled = job["cancel_requested"]
        else:
            cancelled = False
    try:
        if cancelled:
            process.kill()
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"FFmpeg quá thời gian {timeout}s") from None
    finally:
        with _JOBS_LOCK:
            if job is not None:
                job["proc"] = None
    if job_id:
        _check_cancel(job_id)
    if process.returncode != 0:
        raise RuntimeError((stderr or stdout or "FFmpeg lỗi")[-300:])
    return subprocess.CompletedProcess(cmd, 0, stdout, stderr)


def _probe_video(path: Path) -> tuple[int, int, float]:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
            creationflags=(subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
        )
        data = json.loads(result.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
        duration = _num((data.get("format") or {}).get("duration"))
        return width, height, duration
    except Exception as exc:
        logger.warning("handoff: không probe được %s (%s)", path, type(exc).__name__)
        return 0, 0, 0.0


def _register_locked(job_id: str, stem: str) -> Dict[str, Any]:
    now = time.time()
    for stale, record in list(_HANDOFF_JOBS.items()):
        if record.get("status") not in _ACTIVE and now - record.get("finished_at", now) > _JOB_TTL:
            del _HANDOFF_JOBS[stale]
    job = {"status": "queued", "progress": 0, "stage": "Trong hàng chờ…", "stem": stem,
           "created_at": now, "cancel_requested": False, "proc": None}
    _HANDOFF_JOBS[job_id] = job
    return job


def _register(job_id: str, stem: str) -> Dict[str, Any]:
    with _JOBS_LOCK:
        return _register_locked(job_id, stem)


def _update(job_id: str, **fields) -> None:
    with _JOBS_LOCK:
        job = _HANDOFF_JOBS.get(job_id)
        if job:
            if fields.get("status") in {"done", "error", "cancelled"}:
                fields["finished_at"] = time.time()
            job.update(fields)


def _stage(job_id: str, pct: int, message: str) -> None:
    _update(job_id, progress=int(pct), stage=message)


def _check_cancel(job_id: str) -> None:
    with _JOBS_LOCK:
        job = _HANDOFF_JOBS.get(job_id) or {}
    if job.get("cancel_requested"):
        raise HandoffCancelled("Đã huỷ bản giao theo yêu cầu.")


def _link_into_job_dir(source: Path, target: Path) -> str:
    """Hardlink khi cùng ổ đĩa để Studio tìm thấy video mà không nhân bản dung lượng."""
    if target.is_file() and target.stat().st_size > 0:
        return "existing"
    try:
        os.link(source, target)
        return "hardlink"
    except OSError:
        pass
    try:
        from disk_guard import ensure_free_space
        ensure_free_space(target.parent, purpose="nhận video từ SubExtract")
    except Exception as exc:
        logger.info("handoff: bỏ qua copy video vì kiểm tra dung lượng thất bại (%s)", type(exc).__name__)
        return "pointer"
    try:
        from pipeline_v2.atomic_io import atomic_copy_file
        atomic_copy_file(source, target)
        return "copy"
    except OSError:
        return "pointer"


@router.post("/api/handoff/import")
async def api_handoff_import(payload: Dict[str, Any] = Body(...)):
    """Ghi phụ đề SubExtract vào thư mục job của Studio; chưa render gì cả."""
    import asyncio
    return await asyncio.to_thread(_import_job, payload or {})


def _import_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    from pipeline_v2.atomic_io import atomic_write_text
    wf = _wf()

    source = _resolve_source_video(payload.get("source_video"))
    if payload.get("stem"):
        wf.safe_stem(payload["stem"])
    stem = "subextract_" + uuid.uuid4().hex
    cues = _normalize_cues(payload.get("cues"))

    width = int(_num(payload.get("video_width")))
    height = int(_num(payload.get("video_height")))
    duration = _num(payload.get("duration"))
    if width <= 0 or height <= 0 or duration <= 0:
        probed_w, probed_h, probed_d = _probe_video(source)
        width = width if width > 0 else probed_w
        height = height if height > 0 else probed_h
        duration = duration if duration > 0 else probed_d

    main_y = _main_y_pct(cues, payload.get("main_y_pct"))
    for cue in cues:
        if not cue["box"]:
            cue["box"] = _band_box(main_y)

    job_dir = wf.WORKSPACE.resolve() / stem
    job_dir.mkdir(exist_ok=False)
    link_mode = _link_into_job_dir(source, job_dir / f"{stem}.mp4")
    video_path = (job_dir / f"{stem}.mp4") if link_mode != "pointer" else source

    atomic_write_text(job_dir / "original.srt", _srt_text(cues, "text"))
    atomic_write_text(job_dir / "translated.srt", _srt_text(cues, "trans"))
    meta = {
        "version": _VERSION,
        "source": "subextract",
        "source_job_id": str(payload.get("source_job_id") or ""),
        "source_filename": str(payload.get("source_filename") or source.name),
        "stem": stem,
        "video": str(video_path),
        "video_original": str(source),
        "video_link": link_mode,
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "main_y_pct": main_y,
        "target_lang": str(payload.get("target_lang") or "vi"),
        "imported_at": _now_iso(),
        "cues": cues,
        "counts": {"cues": len(cues),
                   "translated": sum(1 for c in cues if c["trans"]),
                   "with_box": sum(1 for c in cues if c["box"])},
    }
    atomic_write_text(job_dir / "handoff.json", json.dumps(meta, ensure_ascii=False, indent=2))
    logger.info("handoff: nhận %s cue của '%s' từ SubExtract (%s)",
                len(cues), stem, meta["counts"]["translated"])
    return {
        "ok": True,
        "stem": stem,
        "job_dir": str(job_dir),
        "video": str(video_path),
        "video_link": link_mode,
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "main_y_pct": main_y,
        "cue_count": len(cues),
        "translated_count": meta["counts"]["translated"],
        "studio_page": "/quy-trinh",
    }


@router.get("/api/handoff/job")
async def api_handoff_job(stem: str = Query(...)):
    wf = _wf()
    meta_path = wf.resolve_job_dir(wf.safe_stem(stem)) / "handoff.json"
    if not meta_path.is_file():
        raise HTTPException(status_code=404, detail="Chưa có bản giao phụ đề cho video này.")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HTTPException(status_code=500, detail="handoff.json lỗi, cần gửi lại từ SubExtract.") from None
    return {"ok": True, "stem": meta.get("stem"), "imported_at": meta.get("imported_at"),
            "counts": meta.get("counts"), "video": meta.get("video"),
            "main_y_pct": meta.get("main_y_pct")}


def _resolve_voice(payload: Dict[str, Any]) -> tuple[str, str, str]:
    requested = str(payload.get("voice_id") or "").strip()
    try:
        import voice_selection
        catalog = voice_selection.catalog()
        if requested:
            entry = next((v for v in catalog if v["id"] == requested), None)
            if not entry:
                raise HTTPException(status_code=400, detail=f"Giọng '{requested}' không có trong catalog.")
        else:
            entry = voice_selection.selected() or {}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Không đọc được catalog giọng đọc: {exc}") from exc
    source = str(entry.get("source") or "edge")
    param = str(entry.get("param") or "vi-VN-HoaiMyNeural")
    return source, param, str(entry.get("label") or f"{source}/{param}")


def _project_dir(stem: str) -> Path:
    if not _HANDOFF_STEM.fullmatch(stem):
        raise HTTPException(409, "Bản giao cũ chưa có thư mục riêng. Hãy gửi lại từ SubExtract.")
    path = _wf().WORKSPACE.resolve() / stem
    if path.resolve() != path or not (path / "handoff.json").is_file():
        raise HTTPException(404, "Chưa gửi phụ đề sang Tool V1.")
    return path


@router.post("/api/handoff/render")
async def api_handoff_render(payload: Dict[str, Any] = Body(...)):
    """Nhận tác vụ vào hàng chờ; chỉ worker đầu hàng được xin quyền render."""
    global _QUEUE_WORKER
    stem = _wf().safe_stem(payload.get("stem"))
    _project_dir(stem)
    if payload.get("delivery_path"):
        raise HTTPException(400, "Bản giao phải dùng đường dẫn xuất riêng do Tool V1 cấp.")
    job_id = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        if any(job["stem"] == stem and job["status"] in _ACTIVE for job in _HANDOFF_JOBS.values()):
            raise HTTPException(409, "Bản giao này đã ở trong hàng chờ hoặc đang xuất video.")
        if len(_RENDER_QUEUE) >= _MAX_PENDING:
            raise HTTPException(429, "Hàng chờ Tool V1 đã đầy.")
        _register_locked(job_id, stem)
        _RENDER_QUEUE.append((job_id, stem, dict(payload)))
        position = len(_RENDER_QUEUE)
        if _QUEUE_WORKER is None:
            try:
                worker = threading.Thread(target=_drain_queue, daemon=True, name="subextract-handoff")
                worker.start()
            except Exception:
                _RENDER_QUEUE.pop()
                del _HANDOFF_JOBS[job_id]
                raise
            _QUEUE_WORKER = worker
    return {"status": "queued", "job_id": job_id, "stem": stem, "queue_position": position}


def _drain_queue() -> None:
    global _QUEUE_WORKER
    while True:
        with _JOBS_LOCK:
            if not _RENDER_QUEUE:
                _QUEUE_WORKER = None
                return
            item = _RENDER_QUEUE[0]
        _render_worker(*item)
        with _JOBS_LOCK:
            if _RENDER_QUEUE and _RENDER_QUEUE[0] is item:
                _RENDER_QUEUE.popleft()


@contextmanager
def _render_ownership(job_id: str):
    import job_tracker
    from v1_integrity_errors import JobStateReadError

    studio = _wf()._TIMELINE_RENDER_SEM
    _stage(job_id, 0, "Đang chờ batch hoặc lượt render Studio…")
    while True:
        _check_cancel(job_id)
        try:
            state = job_tracker.get_status(strict=True)
        except JobStateReadError:
            # Studio can hold the lease without publishing an active batch.
            time.sleep(0.25)
            continue
        active = state["active"] or any(w["active"] for w in state.get("workers", {}).values())
        if not active and studio.acquire(blocking=False):
            claimed = False
            try:
                # The idle snapshot alone cannot arbitrate against another process.
                try:
                    with job_tracker._LOCK:
                        job_tracker._claim_batch_lock_unlocked(job_id, exclusive=True)
                    claimed = True
                except job_tracker.JobAlreadyRunningError:
                    pass
                if claimed:
                    _check_cancel(job_id)
                    yield
                    return
            finally:
                try:
                    if claimed:
                        job_tracker.release_batch(job_id)
                finally:
                    studio.release()
        time.sleep(0.25)


def _render_worker(job_id: str, stem: str, payload: Dict[str, Any]) -> None:
    try:
        with _render_ownership(job_id):
            with _JOBS_LOCK:
                job = _HANDOFF_JOBS[job_id]
                if job["cancel_requested"]:
                    raise HandoffCancelled("Đã huỷ bản giao theo yêu cầu.")
                if _RENDER_QUEUE and _RENDER_QUEUE[0][0] == job_id:
                    _RENDER_QUEUE.popleft()
                job.update(status="running", stage="Đang chuẩn bị xuất video…")
            result = _execute_render(job_id, stem, payload)
        _update(job_id, status="done", progress=100, stage="Hoàn tất", result=result)
    except HandoffCancelled as exc:
        _update(job_id, status="cancelled", stage=str(exc)[:200])
    except HTTPException as exc:
        _update(job_id, status="error", stage=str(exc.detail)[:200], error=str(exc.detail))
    except Exception as exc:
        logger.exception("handoff render lỗi")
        _update(job_id, status="error", stage=str(exc)[:200], error=str(exc))


def _execute_render(job_id: str, stem: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    import asyncio
    import srt
    from ass_utils import generate_ass_file, sync_and_clamp_subtitles
    from video_utils import mix_audio_pydub, process_video
    from pipeline_v2.atomic_io import atomic_copy_file, atomic_write_text
    from ai.voice_cloning import generate_dubbing_audio

    from subtitle_blur import freeze_subtitle_settings
    import canvas_settings
    subtitle_settings = freeze_subtitle_settings(payload, canvas_settings.get_canvas_settings())
    wf = _wf()
    job_dir = _project_dir(stem)
    meta = json.loads((job_dir / "handoff.json").read_text(encoding="utf-8"))
    if meta.get("source") != "subextract" or meta.get("stem") != stem:
        raise RuntimeError("Thông tin sở hữu thư mục bản giao không hợp lệ.")
    video = Path(meta.get("video") or "")
    if not video.is_file():
        raise RuntimeError(f"Không tìm thấy video nguồn: {video.name}")
    cues = meta.get("cues") or []
    if not cues:
        raise RuntimeError("handoff.json không có câu phụ đề nào.")

    _check_cancel(job_id)

    duck_db = _num(payload.get("original_volume_db"), _DEFAULT_DUCK_DB)
    dub_db = _num(payload.get("dubbing_volume_db"), _DEFAULT_DUB_DB)
    main_y = _num(meta.get("main_y_pct"), _FALLBACK_MAIN_Y)
    width = int(_num(meta.get("width"))) or 1080
    height = int(_num(meta.get("height"))) or 1920
    duration = _num(meta.get("duration"))

    _stage(job_id, 8, "Tách âm thanh gốc để giữ làm nền...")
    wav = job_dir / "original.wav"
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn", "-ac", "2", "-ar", "44100", str(wav)],
         timeout=900, job_id=job_id)
    if duration <= 0:
        duration = _probe_video(video)[2]

    segments = []
    voiced = []
    for cue in cues:
        display = str(cue.get("trans") or cue.get("text") or "").strip()
        if not display:
            continue
        seg = srt.Subtitle(index=int(cue["index"]), start=timedelta(seconds=float(cue["start"])),
                           end=timedelta(seconds=float(cue["end"])), content=display)
        _attach_geometry(seg, cue.get("box"), main_y,
                         require_source_geometry=subtitle_settings["sub_cover_style"] == "localized_blur")
        segments.append(seg)
        if str(cue.get("trans") or "").strip():
            voiced.append(seg)

    import copy
    source_segments = copy.deepcopy(segments)

    voice_source, voice_param, voice_label = _resolve_voice(payload)
    failed_indices: List[int] = []
    dubbing: List[Dict[str, Any]] = []
    if voiced:
        _stage(job_id, 12, f"Đang tạo giọng đọc Việt cho {len(voiced)} câu bằng {voice_label}...")
        dubbing_dir = job_dir / "dubbing"
        dubbing_dir.mkdir(parents=True, exist_ok=True)
        for stale in dubbing_dir.glob("*.mp3"):
            stale.unlink(missing_ok=True)
        dubbing = asyncio.run(generate_dubbing_audio(
            voiced, str(dubbing_dir), voice_source=voice_source, voice_param=voice_param,
            api_key=os.getenv("GEMINI_API_KEY", ""), video_duration=duration or None,
        ))
        failed_indices = [int(d["index"]) for d in dubbing if d and d.get("error")]
        if not [d for d in dubbing if d and not d.get("error")]:
            raise RuntimeError("Không tạo được file giọng đọc nào — kiểm tra API TTS hoặc giọng đã chọn.")
    else:
        _stage(job_id, 12, "Chưa có bản dịch Việt — chỉ giữ phụ đề gốc, bỏ qua lồng tiếng.")

    _check_cancel(job_id)
    _stage(job_id, 70, "Căn giờ phụ đề theo độ dài giọng đọc...")
    if voiced:
        sync_and_clamp_subtitles(voiced, dubbing)

    _stage(job_id, 76, "Dựng phụ đề Việt và vùng xử lý chữ gốc...")
    ass_path = job_dir / "final.ass"
    generate_ass_file(
        segments, [], str(ass_path),
        play_res_x=width, play_res_y=height, main_y_pct=main_y,
        font_name=str(payload.get("font_name") or "Arial"),
        font_color=str(payload.get("font_color") or "&H00000000"),
        font_weight=int(_num(payload.get("font_weight"), 2)),
        cover_style=subtitle_settings["sub_cover_style"], blur_settings=subtitle_settings,
        source_segments=source_segments,
    )
    if not ass_path.is_file() or ass_path.stat().st_size < 64:
        raise RuntimeError("Không tạo được final.ass.")

    _stage(job_id, 80, f"Hòa âm: nền gốc {duck_db}dB + giọng Việt {dub_db}dB...")
    mixed = job_dir / "mixed.wav"
    mix_audio_pydub(str(wav), dubbing, str(mixed), original_volume_db=duck_db,
                    dubbing_volume_db=dub_db, ducking_mode="soft", explicit=True)
    if not mixed.is_file():
        raise RuntimeError("Hòa âm thất bại, không có mixed.wav.")

    _check_cancel(job_id)
    _stage(job_id, 86, "Render video bằng NVENC (che phụ đề gốc + burn phụ đề Việt)...")
    final_video = job_dir / f"final_{stem}.mp4"
    staged_render = job_dir / f"final_{stem}.handoff.mp4"
    rendered = process_video(str(video), str(ass_path), str(mixed), str(staged_render),
                             main_y_pct=main_y, delogo=False,
                             subtitle_cover_style=subtitle_settings["sub_cover_style"],
                             subtitle_blur_settings=subtitle_settings)
    _check_cancel(job_id)
    if not rendered or not staged_render.is_file() or staged_render.stat().st_size < 10240:
        raise RuntimeError("Render NVENC thất bại hoặc file xuất bị lỗi.")
    atomic_copy_file(staged_render, final_video)
    staged_render.unlink(missing_ok=True)

    _stage(job_id, 96, "Đóng gói ra thư mục video hoàn thành...")
    delivered = wf.OUTPUT_DIR / f"Dubbed_{stem}.mp4"
    try:
        atomic_copy_file(final_video, delivered)
    except OSError as exc:
        raise RuntimeError(f"Không sao lưu được video thành phẩm: {exc}") from exc

    atomic_write_text(job_dir / "handoff_result.json", json.dumps({
        "finished_at": _now_iso(), "job_id": job_id, "delivered": str(delivered),
        "final_video": str(final_video), "voice": voice_label,
        "cues": len(segments), "voiced": len(voiced), "failed_cues": failed_indices,
        "subtitle_settings": subtitle_settings,
        "seconds": round(time.time() - started, 1),
    }, ensure_ascii=False, indent=2))

    _stage(job_id, 100, "Hoàn tất")
    return {
        "stem": stem,
        "final_video": str(final_video),
        "delivered": str(delivered),
        "job_dir": str(job_dir),
        "ass": str(ass_path),
        "mixed_audio": str(mixed),
        "voice": voice_label,
        "cue_count": len(segments),
        "voiced_count": len(voiced),
        "failed_cues": failed_indices,
        "seconds": round(time.time() - started, 1),
    }


def _attach_geometry(seg, box: Any, main_y: float, *, require_source_geometry=False) -> None:
    """Gắn toạ độ hộp che cho từng câu — pipeline sinh từ OCR, còn đây là OCR của SubExtract."""
    resolved = _clamp_box(box)
    if resolved is None and require_source_geometry:
        raise ValueError("Thiếu tọa độ phụ đề nguồn trong handoff; hãy sửa vùng trước khi làm mờ")
    resolved = resolved or _band_box(main_y)
    block = {
        "text": seg.content,
        "start": seg.start.total_seconds(),
        "end": seg.end.total_seconds(),
        "x_pct": resolved[0], "max_x_pct": resolved[2],
        "y_pct": resolved[1], "max_y_pct": resolved[3],
        "prob": 0.9,
        "is_subtitle": True,
        "in_subtitle_band": True,
        "is_packaging": False,
        "is_static": False,
        "type": "subtitle",
        "geometry_verified": True,
    }
    seg.best_block = block
    seg.tracking_blocks = [block]
    seg.y_pct = resolved[1]
    seg.max_y_pct = resolved[3]
    seg.ocr_mask_status = "located"
    seg.geometry_verified = True


@router.get("/api/handoff/status")
async def api_handoff_status(job_id: str = Query(...)):
    with _JOBS_LOCK:
        job = _HANDOFF_JOBS.get(job_id)
        snapshot = dict(job) if job else None
        if snapshot and snapshot["status"] == "queued":
            for position, item in enumerate(_RENDER_QUEUE, 1):
                if item[0] == job_id:
                    snapshot["queue_position"] = position
                    break
    if not snapshot:
        raise HTTPException(status_code=404, detail="Không tìm thấy job handoff")
    snapshot.pop("proc", None)
    snapshot.pop("created_at", None)
    snapshot["ok"] = True
    return snapshot


@router.post("/api/handoff/cancel")
async def api_handoff_cancel(payload: Dict[str, Any] = Body(...)):
    job_id = str((payload or {}).get("job_id") or "")
    with _JOBS_LOCK:
        job = _HANDOFF_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Không tìm thấy job handoff")
        if job["status"] not in _ACTIVE:
            return {"status": job["status"], "job_id": job_id}
        job["cancel_requested"] = True
        if job["status"] == "queued":
            for item in _RENDER_QUEUE:
                if item[0] == job_id:
                    _RENDER_QUEUE.remove(item)
                    break
            job.update(status="cancelled", stage="Đã rút khỏi hàng chờ.", finished_at=time.time())
            return {"status": "cancelled", "job_id": job_id}
        job["status"] = "cancelling"
        process = job.get("proc")
    if process is not None:
        try:
            process.kill()
        except OSError:
            pass
    return {"status": "cancelling", "job_id": job_id}


@router.get("/api/handoff/info")
async def api_handoff_info():
    """Cho SubExtract biết Tool V1 nhận gì, đang chọn giọng nào, xuất ở đâu."""
    wf = _wf()
    try:
        voice_source, voice_param, voice_label = _resolve_voice({})
        voices = [voice_label]
    except HTTPException:
        voice_source = voice_param = voice_label = ""
    try:
        import voice_selection
        voices = [{"id": v["id"], "label": v.get("label", "")} for v in voice_selection.catalog()][:80]
    except Exception:
        voices = []
    return {
        "ok": True,
        "workspace": str(wf.WORKSPACE),
        "output_dir": str(wf.OUTPUT_DIR),
        "allowed_source_dirs": [str(p) for p in _allowed_roots()],
        "voice": {"source": voice_source, "param": voice_param, "label": voice_label},
        "voices": voices,
        "defaults": {"original_volume_db": _DEFAULT_DUCK_DB, "dubbing_volume_db": _DEFAULT_DUB_DB},
        "version": _VERSION,
        "render_queue": "fifo",
    }
