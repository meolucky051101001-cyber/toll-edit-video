"""
cloud_routes.py - FastAPI APIRouter cho Cloud GPU Processing trên Tool V1.
Cung cấp Dashboard giao diện Web và các endpoint REST API.
"""

import logging
import asyncio
import math
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .cloud_config import DEFAULT_CONFIG
from .cloud_job_store import CloudJobStore
from .cloud_worker import get_cloud_worker
from .colab_bridge import get_sessions, get_usage, stop_session, stop_all_sessions

logger = logging.getLogger("cloud.routes")
router = APIRouter(tags=["Cloud GPU Processing"])
store = CloudJobStore()


@router.get("/cloud", response_class=HTMLResponse)
async def cloud_dashboard_page():
    """Trang giao diện điều khiển Cloud GPU Pipeline theo phong cách Tool V1 tối giản."""
    local_token = ""
    token_file = Path(DEFAULT_CONFIG["db_path"]).parent / "bot_system" / ".dashboard_control_token"
    if token_file.exists():
        try:
            local_token = token_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    template_path = Path(__file__).resolve().parent.parent / "templates" / "cloud_dashboard.html"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read().replace("__REPLACE_TOKEN__", local_token)
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})

    return HTMLResponse("<h2>Không tìm thấy template cloud_dashboard.html.</h2>", status_code=404)


@router.get("/api/cloud/status")
async def api_cloud_status():
    """Tra cứu trạng thái usage, worker, active job và danh sách active session của Cloud GPU."""
    worker = get_cloud_worker()
    usage, sessions = await asyncio.gather(asyncio.to_thread(get_usage), asyncio.to_thread(get_sessions))
    current_job = worker.get_current_job() or store.get_active_job()
    budget = store.get_cu_budget_info()

    return {
        "status": "ok",
        "usage": usage,
        "worker_running": worker.is_running(),
        "active_job": current_job.to_dict() if current_job else None,
        "sessions": sessions,
        "budget": budget,
    }


@router.get("/api/cloud/budget")
async def api_cloud_get_budget():
    """Lấy thông tin hạn mức CU trong ngày."""
    return {
        "status": "ok",
        "budget": store.get_cu_budget_info(),
    }


@router.post("/api/cloud/budget")
async def api_cloud_set_budget(payload: Dict[str, Any] = Body(...)):
    """Cập nhật hạn mức CU tối đa trong ngày."""
    raw_budget = payload.get("budget_daily")
    if raw_budget is None:
        raise HTTPException(status_code=400, detail="Thiếu giá trị budget_daily.")
    try:
        val = float(raw_budget)
        if not math.isfinite(val) or val <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Hạn mức budget_daily phải là số dương lớn hơn 0.")

    store.set_cu_budget_daily(val)
    return {
        "status": "ok",
        "message": f"Đã cập nhật hạn mức CU hôm nay thành {val:.2f} CU.",
        "budget": store.get_cu_budget_info(),
    }


@router.get("/api/cloud/jobs")
async def api_cloud_jobs(limit: int = Query(30, ge=1, le=100)):
    """Lấy danh sách các job trong hàng đợi Cloud GPU."""
    jobs = store.list_jobs(limit=limit)
    return {
        "status": "ok",
        "jobs": [j.to_dict() for j in jobs],
    }


@router.post("/api/cloud/enqueue")
async def api_cloud_enqueue(payload: Dict[str, Any] = Body(...)):
    """Thêm một video (file cục bộ hoặc link URL Douyin/TikTok/YouTube/FB) vào hàng đợi Cloud GPU."""
    input_target = (payload.get("video_path") or payload.get("url") or "").strip()
    if not input_target:
        raise HTTPException(status_code=400, detail="Thiếu đường dẫn file hoặc link video URL.")

    is_url = input_target.startswith("http://") or input_target.startswith("https://")
    if is_url:
        import uuid
        from urllib.parse import urlparse
        parsed = urlparse(input_target)
        host_part = parsed.netloc.replace("www.", "").split(".")[0] or "online"
        uid_short = uuid.uuid4().hex[:6]
        video_name = f"{host_part}_{uid_short}.mp4"
        video_path = input_target
    else:
        path_obj = Path(input_target)
        if not path_obj.exists():
            raise HTTPException(status_code=404, detail=f"File video không tồn tại trên máy: {input_target}")
        video_name = path_obj.name
        video_path = str(path_obj)

    gpu_type = payload.get("gpu_type", "T4").upper()
    task_type = payload.get("task_type", "full_pipeline")
    voice = payload.get("voice", "BV562_streaming")

    job = store.enqueue(
        video_path=video_path,
        video_name=video_name,
        gpu_type=gpu_type,
        task_type=task_type,
        voice=voice,
    )

    # Đảm bảo daemon worker đang chạy
    worker = get_cloud_worker()
    if not worker.is_running():
        worker.start()

    return {
        "status": "ok",
        "message": f"Đã thêm job {job.job_id} vào hàng đợi Cloud GPU ({gpu_type}).",
        "job": job.to_dict(),
    }


@router.post("/api/cloud/job/cancel")
async def api_cloud_job_cancel(payload: Dict[str, Any] = Body(...)):
    """Huỷ một công việc trong hàng đợi hoặc đang chạy."""
    job_id = payload.get("job_id", "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="Thiếu job_id.")

    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Không tìm thấy job.")

    # Persist cancellation before waiting on the provider so late work cannot publish.
    cancelled = store.cancel_job(job_id)
    stopped, message = True, "Job chưa có session."
    if job.session_name:
        stopped, message = await asyncio.to_thread(stop_session, job.session_name)
        if not stopped:
            store.note_cleanup_failure(job_id, message)
    return {
        "status": "ok" if stopped else "error",
        "cancelled": cancelled,
        "session_stopped": stopped,
        "message": message if not stopped else (f"Đã huỷ job {job_id}." if cancelled else "Job đã kết thúc hoặc không thể huỷ."),
    }


@router.post("/api/cloud/job/delete")
async def api_cloud_job_delete(payload: Dict[str, Any] = Body(...)):
    """Xoá bản ghi job khỏi database nhật ký."""
    job_id = payload.get("job_id", "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="Thiếu job_id.")

    deleted = store.delete_job(job_id)
    if not deleted and store.get_job(job_id):
        raise HTTPException(status_code=409, detail="Chỉ xóa lịch sử sau khi job và session đã dừng.")
    return {
        "status": "ok",
        "deleted": deleted,
        "message": f"Đã xoá job {job_id}." if deleted else "Không tìm thấy job để xoá.",
    }


@router.post("/api/cloud/session/stop")
async def api_cloud_session_stop(payload: Dict[str, Any] = Body(...)):
    """Dừng một session Colab cụ thể theo tên."""
    session_name = payload.get("session_name", "").strip()
    if not session_name:
        raise HTTPException(status_code=400, detail="Thiếu session_name.")

    ok, msg = await asyncio.to_thread(stop_session, session_name)
    return {
        "status": "ok" if ok else "error",
        "message": f"Đã dừng session {session_name}: {msg}" if ok else f"Lỗi dừng session: {msg}",
    }


@router.post("/api/cloud/stop")
async def api_cloud_stop():
    """Dừng khẩn cấp mọi session Colab đang mở để bảo vệ Compute Units."""
    store.cancel_all()
    ok, message = await asyncio.to_thread(stop_all_sessions)
    if ok:
        store.acknowledge_all_sessions_stopped()
    return {
        "status": "ok" if ok else "error",
        "message": message,
        "errors": [] if ok else [message],
    }


@router.post("/api/cloud/worker/start")
async def api_cloud_worker_start():
    worker = get_cloud_worker()
    worker.start()
    return {"status": "ok", "worker_running": True}


@router.post("/api/cloud/worker/stop")
async def api_cloud_worker_stop():
    worker = get_cloud_worker()
    stopped = await asyncio.to_thread(worker.stop)
    return {"status": "ok" if stopped else "stopping", "worker_running": worker.is_running()}
