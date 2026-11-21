"""
cloud_routes.py - FastAPI APIRouter cho Cloud GPU Processing trên Tool V1.
Cung cấp Dashboard giao diện Web và các endpoint REST API.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .cloud_contract import (
    admit_and_enqueue_job,
    fetch_provider_status_nonblocking,
    get_cloud_status_snapshot,
    request_job_cancellation,
    run_provider_action_nonblocking,
)
from .cloud_job_store import CloudJobStore
from .cloud_security import (
    CloudValidationError,
    is_loopback_client,
    is_valid_loopback_host,
    is_valid_loopback_origin,
    redact_secrets,
    resolve_local_control_token,
    validate_session_name,
)
from .cloud_worker import get_cloud_worker, peek_cloud_worker
from .cloud_telegram_bot import get_cloud_telegram_bot_status, peek_cloud_telegram_bot_worker
from .colab_bridge import (
    ColabProviderAdapter,
    abort_session_processes,
    get_sessions,
    get_sessions_status,
    get_usage,
    stop_session,
    stop_all_sessions,
)

_ORIG_GET_USAGE = get_usage
_ORIG_GET_SESSIONS = get_sessions

logger = logging.getLogger("cloud.routes")


async def _verify_local_cloud_request(request: Request) -> None:
    """
    Enforce loopback Host, loopback socket IP, loopback Origin (if supplied),
    and X-Local-Control-Token on state-changing routes even when mounted standalone.
    """
    host_header = request.headers.get("host", "")
    if not is_valid_loopback_host(host_header):
        raise HTTPException(status_code=403, detail=f"Host không hợp lệ: '{host_header}'")

    client_host = request.client.host if request.client else ""
    if not is_loopback_client(client_host):
        raise HTTPException(status_code=403, detail="Chỉ hỗ trợ truy cập nội bộ (localhost)")

    if request.method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
        origin_header = request.headers.get("origin", "").strip()
        if origin_header and not is_valid_loopback_origin(origin_header):
            raise HTTPException(status_code=403, detail=f"Origin không hợp lệ: '{origin_header}'")

        token = request.headers.get("X-Local-Control-Token", "")
        expected_token = resolve_local_control_token()
        if not token or not secrets.compare_digest(str(token), str(expected_token)):
            raise HTTPException(status_code=403, detail="Yêu cầu thiếu hoặc sai X-Local-Control-Token")


router = APIRouter(
    tags=["Cloud GPU Processing"],
    dependencies=[Depends(_verify_local_cloud_request)],
)
store = CloudJobStore()


@router.get("/cloud", response_class=HTMLResponse)
async def cloud_dashboard_page():
    """Trang giao diện điều khiển Cloud GPU Pipeline theo phong cách Tool V1 tối giản."""
    local_token = resolve_local_control_token()

    template_path = Path(__file__).resolve().parent.parent / "templates" / "cloud_dashboard.html"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            html = f.read().replace("__REPLACE_TOKEN__", local_token)
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})

    return HTMLResponse("<h2>Không tìm thấy template cloud_dashboard.html.</h2>", status_code=404)


@router.get("/api/cloud/status")
async def api_cloud_status(force_refresh: bool = Query(False)):
    """Tra cứu trạng thái usage, worker, active job và active session (non-blocking, read-only)."""
    usage_fn = None if get_usage is _ORIG_GET_USAGE else get_usage
    sessions_fn = None if get_sessions is _ORIG_GET_SESSIONS else get_sessions
    active_worker = peek_cloud_worker()
    bot_worker = peek_cloud_telegram_bot_worker()
    tb_status = get_cloud_telegram_bot_status()
    if bot_worker is not None and (active_worker is None or active_worker.get_current_job() is None):
        if bot_worker.get_current_job() is not None:
            active_worker = bot_worker
    snap = await asyncio.to_thread(
        get_cloud_status_snapshot,
        store,
        worker=active_worker,
        usage_fn=usage_fn,
        sessions_fn=sessions_fn,
        force_refresh=force_refresh,
    )
    if bot_worker is not None and bot_worker.get_current_job() is not None:
        snap["worker_running"] = True
        snap["worker_state"] = "running"
    snap["telegram_bot"] = tb_status
    return snap


@router.post("/api/cloud/rollout")
async def api_cloud_rollout(payload: Dict[str, Any] = Body(...)):
    action = payload.get("action")
    if action not in ("arm", "pause"):
        raise HTTPException(status_code=400, detail="Chọn arm hoặc pause.")
    try:
        rollout = await asyncio.to_thread(store.set_rollout, arm=action == "arm")
    except CloudValidationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
    return {"status": "ok", "rollout": rollout}


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
        if val <= 0:
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


@router.get("/api/cloud/ledger")
async def api_cloud_ledger(
    job_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
):
    """Lấy sổ cái chi phí CU không thể xoá (gồm cả job lỗi, huỷ hoặc đã ẩn khỏi UI)."""
    return {
        "status": "ok",
        "budget": store.get_cu_budget_info(),
        "ledger": store.list_cost_ledger(job_id=job_id, limit=limit),
    }


@router.post("/api/cloud/enqueue")
async def api_cloud_enqueue(request: Request, payload: Dict[str, Any] = Body(...)):
    """Thêm một video (file cục bộ hoặc link URL) vào hàng đợi Cloud GPU (hỗ trợ idempotency_key & options snapshot)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload không hợp lệ.")

    raw_target = payload.get("video_path") if "video_path" in payload else payload.get("url")
    if raw_target is None:
        raw_target = ""
    if not isinstance(raw_target, str):
        raise HTTPException(status_code=400, detail="Trường video_path/url phải là chuỗi.")

    gpu_type = payload.get("gpu_type", "T4")
    task_type = payload.get("task_type", "full_pipeline")
    voice = payload.get("voice", "BV562_streaming")
    if not isinstance(gpu_type, str) or not isinstance(task_type, str) or not isinstance(voice, str):
        raise HTTPException(status_code=400, detail="gpu_type, task_type và voice phải là chuỗi hợp lệ.")

    raw_options = payload.get("options")
    if raw_options is not None and not isinstance(raw_options, dict):
        raise HTTPException(status_code=400, detail="Trường options phải là JSON object.")

    options_snapshot: Dict[str, Any] = dict(raw_options or {})
    if "execution_mode" not in options_snapshot:
        options_snapshot["execution_mode"] = "cloud"
    if "allow_no_speech" in payload and "allow_no_speech" not in options_snapshot:
        options_snapshot["allow_no_speech"] = bool(payload.get("allow_no_speech"))

    idempotency_key = payload.get("idempotency_key")
    if idempotency_key is None:
        idempotency_key = request.headers.get("X-Idempotency-Key")

    fresh_usage: Optional[Dict[str, Any]] = None
    existing_snap = await asyncio.to_thread(store.get_provider_usage_snapshot)
    if existing_snap is not None and (existing_snap.get("is_stale") or not existing_snap.get("ok")):
        probe_res = await asyncio.to_thread(
            run_provider_action_nonblocking,
            get_usage,
            timeout_seconds=8.0,
        )
        if probe_res.get("completed") and isinstance(probe_res.get("result"), dict):
            fresh_usage = probe_res["result"]

    try:
        job, contract = await asyncio.to_thread(
            admit_and_enqueue_job, store,
            video_path=raw_target,
            source_channel="dashboard",
            gpu_type=gpu_type,
            task_type=task_type,
            voice=voice,
            enforce_budget=True,
            provider_usage=fresh_usage,
            claim_immediately=False,
            idempotency_key=idempotency_key,
            options=options_snapshot,
            rollout_permit=payload.get("rollout_permit"),
        )
    except CloudValidationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=redact_secrets(str(exc))) from exc

    # Chỉ bật worker khi có job queued; endpoint không tự chạy pipeline và không khởi động dư cho hit trùng đã kết thúc
    if job.state == "queued" and (
        not store.rollout_controlled or store.get_rollout().get("job_id") == job.job_id
    ):
        worker = get_cloud_worker()
        if not worker.is_running():
            await asyncio.to_thread(worker.start)

    dedup = bool(getattr(contract, "deduplicated", False))
    return {
        "status": "ok",
        "deduplicated": dedup,
        "message": (
            f"Đã trả về job hiện có {job.job_id} cho cùng idempotency_key."
            if dedup
            else f"Đã thêm job {job.job_id} vào hàng đợi Cloud GPU ({job.gpu_type})."
        ),
        "job": job.to_dict(),
        "contract": contract.to_dict(),
    }


@router.post("/api/cloud/job/cancel")
async def api_cloud_job_cancel(payload: Dict[str, Any] = Body(...)):
    """Huỷ một công việc trong hàng đợi hoặc đang chạy (non-blocking khi provider treo)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload không hợp lệ.")
    raw_job_id = payload.get("job_id")
    if not isinstance(raw_job_id, str) or not raw_job_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu job_id.")
    job_id = raw_job_id.strip()

    target_worker = peek_cloud_worker()
    bot_worker = peek_cloud_telegram_bot_worker()
    if bot_worker is not None:
        cur_bot_job = bot_worker.get_current_job()
        if cur_bot_job is not None and cur_bot_job.job_id == job_id:
            target_worker = bot_worker

    res = await asyncio.to_thread(
        request_job_cancellation,
        store,
        job_id,
        worker=target_worker,
        adapter=ColabProviderAdapter(),
        abort_fn=abort_session_processes,
        reason="Huỷ theo yêu cầu người dùng qua Dashboard.",
    )
    if not res.get("found"):
        raise HTTPException(status_code=404, detail="Không tìm thấy job.")

    return {
        "status": "ok",
        **{k: v for k, v in res.items() if k != "found"},
    }


@router.post("/api/cloud/job/delete")
async def api_cloud_job_delete(payload: Dict[str, Any] = Body(...)):
    """Ẩn bản ghi job đã kết thúc khỏi danh sách UI (giữ nguyên sổ chi phí & đối soát)."""
    job_id = str(payload.get("job_id", "")).strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="Thiếu job_id.")

    deleted = store.delete_job(job_id)
    return {
        "status": "ok",
        "deleted": deleted,
        "message": (
            f"Đã ẩn job {job_id} khỏi danh sách (dữ liệu sổ chi phí vẫn được lưu)."
            if deleted
            else "Không thể ẩn: không tìm thấy job hoặc job chưa kết thúc/thu hồi session xong."
        ),
    }


@router.post("/api/cloud/session/stop")
async def api_cloud_session_stop(payload: Dict[str, Any] = Body(...)):
    """Dừng một session Colab cụ thể theo tên (non-blocking nếu provider treo)."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Payload không hợp lệ.")
    raw_session = payload.get("session_name", "")
    if not isinstance(raw_session, str):
        raise HTTPException(status_code=400, detail="session_name phải là chuỗi.")
    try:
        session_name = validate_session_name(raw_session)
    except CloudValidationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc

    warm_row: Optional[Dict[str, Any]] = None
    get_warm_fn = getattr(store, "get_warm_session", None)
    if callable(get_warm_fn):
        try:
            warm_row = await asyncio.to_thread(get_warm_fn, session_name)
        except Exception as exc:
            logger.warning("Không đọc được thông tin session giữ ấm %s: %s", session_name, redact_secrets(str(exc)))

    action_res = await asyncio.to_thread(
        run_provider_action_nonblocking,
        stop_session,
        session_name,
        timeout_seconds=1.0,
    )
    if action_res["timed_out"]:
        return {
            "status": "pending",
            "timed_out": True,
            "message": f"Đã gửi yêu cầu dừng session {session_name}; provider đang phản hồi chậm.",
        }
    if not action_res["completed"]:
        return {
            "status": "error",
            "timed_out": False,
            "message": f"Lỗi dừng session: {redact_secrets(str(action_res.get('error') or ''))}",
        }
    ok, msg = action_res["result"]
    if ok and warm_row and warm_row.get("parked_at") is not None:
        record_warm_idle = getattr(store, "record_warm_idle_usage", None)
        if callable(record_warm_idle):
            try:
                await asyncio.to_thread(
                    record_warm_idle,
                    session_name,
                    str(warm_row.get("gpu_type") or "T4"),
                    parked_at=float(warm_row["parked_at"]),
                    ended_at=time.time(),
                    source_job_id=warm_row.get("source_job_id"),
                )
            except Exception as exc:
                logger.warning(
                    "Không ghi được CU giữ ấm của %s khi dừng từ Dashboard: %s",
                    session_name,
                    redact_secrets(str(exc)),
                )
    remove_warm_fn = getattr(store, "remove_warm_session", None)
    if ok and callable(remove_warm_fn):
        try:
            await asyncio.to_thread(remove_warm_fn, session_name)
        except Exception:
            pass
    safe_msg = redact_secrets(msg)
    return {
        "status": "ok" if ok else "error",
        "timed_out": False,
        "message": f"Đã dừng session {session_name}: {safe_msg}" if ok else f"Lỗi dừng session: {safe_msg}",
    }


@router.post("/api/cloud/stop")
async def api_cloud_stop():
    """Pause admission, cancel accepted jobs, and verify remote shutdown."""
    list_warm_fn = getattr(store, "list_warm_sessions", None)
    try:
        warm_rows = await asyncio.to_thread(list_warm_fn, include_expired=True) if callable(list_warm_fn) else []
    except Exception as exc:
        logger.warning("Không đọc được danh sách session giữ ấm khi dừng Cloud: %s", redact_secrets(str(exc)))
        warm_rows = []

    def cancel_admitted_jobs():
        store.set_rollout(arm=False)
        for job in store.list_jobs(limit=-1, include_hidden=True):
            store.request_cancel(job.job_id, reason="Dashboard yêu cầu dừng Cloud")

    await asyncio.to_thread(cancel_admitted_jobs)
    action = await asyncio.to_thread(
        run_provider_action_nonblocking, stop_all_sessions, timeout_seconds=0.8,
    )
    if action["timed_out"]:
        return {"status": "pending", "timed_out": True,
                "message": "Đã yêu cầu dừng Cloud; đang chờ provider xác nhận thu hồi."}
    if not action["completed"]:
        return {"status": "error", "timed_out": False,
                "message": "Chưa xác nhận dừng Cloud: " + redact_secrets(str(action.get("error") or ""))}
    stopped, message = action["result"]
    if stopped:
        record_warm_idle = getattr(store, "record_warm_idle_usage", None)
        if callable(record_warm_idle):
            for warm_row in warm_rows:
                if warm_row.get("parked_at") is None:
                    continue
                try:
                    await asyncio.to_thread(
                        record_warm_idle,
                        str(warm_row.get("session_name") or ""),
                        str(warm_row.get("gpu_type") or "T4"),
                        parked_at=float(warm_row["parked_at"]),
                        ended_at=time.time(),
                        source_job_id=warm_row.get("source_job_id"),
                    )
                except Exception as exc:
                    logger.warning(
                        "Không ghi được CU giữ ấm của %s khi dừng toàn bộ Cloud: %s",
                        warm_row.get("session_name"),
                        redact_secrets(str(exc)),
                    )
        from .cloud_orchestrator import clear_warm_registry_for_store

        await asyncio.to_thread(clear_warm_registry_for_store, store)
    # In-flight allocations still require each worker's session-specific cleanup
    # verification; a global stop must not prematurely settle their ledger.
    return {"status": "ok" if stopped else "error", "timed_out": False,
            "message": redact_secrets(str(message))}


@router.post("/api/cloud/worker/start")
async def api_cloud_worker_start():
    worker = get_cloud_worker()
    worker.start()
    return {"status": "ok", "worker_running": True}


@router.post("/api/cloud/worker/stop")
async def api_cloud_worker_stop(payload: Optional[Dict[str, Any]] = Body(default=None)):
    worker = get_cloud_worker()
    opts = payload or {}
    cancel_current = bool(opts.get("cancel_current", False))
    timeout = float(opts.get("timeout", 10.0))
    stop_info = worker.stop(timeout=timeout, cancel_current=cancel_current)
    return {"status": "ok", **stop_info}
