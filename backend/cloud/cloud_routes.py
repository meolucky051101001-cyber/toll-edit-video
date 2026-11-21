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

import psutil
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .cloud_contract import (
    CHECKABLE_GPUS,
    DASHBOARD_GPU_SETTING,
    GPU_CHECK_TASK,
    GPU_CU_RATES,
    admit_and_enqueue_job,
    fetch_provider_status_nonblocking,
    get_cloud_status_snapshot,
    request_job_cancellation,
    resolve_cloud_source_channel,
    run_provider_action_nonblocking,
    stamp_provider_usage_observation,
)
from .cloud_job_store import CloudJobStore
from .cloud_job_store import MAX_CONCURRENT_JOBS
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
from .cloud_telegram_bot import (
    get_cloud_default_gpu,
    get_cloud_telegram_bot_status,
    peek_cloud_telegram_bot,
    peek_cloud_telegram_bot_worker,
)
from .cloud_resource_monitor import get_cloud_session_resource_snapshot
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


def _read_provider_usage_observation() -> Dict[str, Any]:
    usage = get_usage()
    return stamp_provider_usage_observation(usage, observed_at=time.time()) if isinstance(usage, dict) else usage


async def _fresh_usage_for_admission() -> Optional[Dict[str, Any]]:
    snapshot = await asyncio.to_thread(store.get_provider_usage_snapshot)
    if snapshot is not None and (snapshot.get("is_stale") or not snapshot.get("ok")):
        probe = await asyncio.to_thread(
            run_provider_action_nonblocking, _read_provider_usage_observation, timeout_seconds=8.0,
        )
        if probe.get("completed") and isinstance(probe.get("result"), dict):
            return probe["result"]
    return None


def _checked_gpu_option(raw: Any) -> str:
    gpu = raw.strip().upper() if isinstance(raw, str) else ""
    if gpu not in CHECKABLE_GPUS:
        raise HTTPException(status_code=400, detail="Chọn T4, L4, A100 hoặc H100.")
    return gpu


def _read_host_memory() -> Optional[Dict[str, Any]]:
    """Physical RAM on the host running this node, not on a Colab VM."""
    try:
        memory = psutil.virtual_memory()
    except (OSError, RuntimeError, psutil.Error):
        return None
    if memory.total <= 0:
        return None
    available = max(0, min(memory.available, memory.total))
    used = memory.total - available
    return {
        "used_bytes": used,
        "total_bytes": memory.total,
        "percent_used": round(100 * used / memory.total, 1),
    }


def _worker_has_jobs(worker: Any) -> bool:
    if worker is None:
        return False
    try:
        get_jobs = getattr(worker, "get_current_jobs", None)
        if callable(get_jobs):
            return bool(get_jobs())
        return worker.get_current_job() is not None
    except Exception:
        return False


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


def _read_cloud_session_resources(active_jobs: list[Dict[str, Any]],
                                  warm_sessions: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Sample only sessions currently referenced by active jobs or unexpired warm rows."""
    try:
        active = {job["session_name"]: job for job in active_jobs if job.get("session_name")}
        warm = {
            item["session_name"]: item for item in warm_sessions
            if item.get("session_name") and item["session_name"] not in active
        }
        owners = {name: f"{job['job_id']}:{job['attempt']}" for name, job in active.items()}
        owners.update({name: f"warm:{item.get('source_job_id')}" for name, item in warm.items()})
        samples = get_cloud_session_resource_snapshot([*active, *warm], owners=owners)
        for sample in samples:
            name = sample["session_name"]
            job = active.get(name)
            if job:
                sample.update(job_id=job["job_id"], job_name=job["video_name"], job_state=job["state"],
                              requested_gpu=job["gpu_type"], warm=False)
                job_ram = sample.get("job_ram")
                active_stage = {"preparing": "setup", "processing": "pipeline"}.get(job["state"])
                if job_ram and job_ram["stage"] != active_stage:
                    sample["job_ram"] = None
            else:
                sample.update(requested_gpu=warm[name].get("gpu_type"), warm=True, job_ram=None)
        return samples
    except Exception:
        logger.warning("Unable to list Cloud-owned sessions for resource telemetry")
        return []


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


@router.get("/api/cloud/local-resources")
async def api_cloud_local_resources():
    """Read local RAM and shared job limits without contacting the Colab provider."""
    active_jobs, concurrency_limit = await asyncio.gather(
        asyncio.to_thread(store.list_active_jobs),
        asyncio.to_thread(store.get_concurrency_limit),
    )
    return {
        "status": "ok",
        "host_memory": _read_host_memory(),
        "active_job_count": len(active_jobs),
        "max_concurrent_jobs": concurrency_limit,
    }


@router.get("/api/cloud/status")
async def api_cloud_status(force_refresh: bool = Query(False)):
    """Tra cứu trạng thái usage, worker, active job và active session (non-blocking, read-only)."""
    usage_fn = None if get_usage is _ORIG_GET_USAGE else get_usage
    sessions_fn = None if get_sessions is _ORIG_GET_SESSIONS else get_sessions
    active_worker = peek_cloud_worker()
    bot_worker = peek_cloud_telegram_bot_worker()
    tb_status = get_cloud_telegram_bot_status()
    active_jobs = store.list_active_jobs() if hasattr(store, "list_active_jobs") else []
    has_active_jobs = bool(active_jobs) or store.get_active_job() is not None
    if bot_worker is not None and (active_worker is None or not _worker_has_jobs(active_worker)):
        if _worker_has_jobs(bot_worker):
            active_worker = bot_worker
    snap = await asyncio.to_thread(
        get_cloud_status_snapshot,
        store,
        worker=active_worker,
        usage_fn=usage_fn,
        sessions_fn=sessions_fn,
        force_refresh=force_refresh,
    )
    if _worker_has_jobs(bot_worker):
        snap["worker_running"] = True
        snap["worker_state"] = "running"
    elif not has_active_jobs and active_worker is None:
        snap["worker_running"] = False
    snap["telegram_bot"] = tb_status
    snap["default_gpu"] = tb_status.get("default_gpu") or await asyncio.to_thread(get_cloud_default_gpu, store)
    snap["gpu_concurrency_limits"] = await asyncio.to_thread(store.get_gpu_concurrency_limits)
    snap["host_memory"] = _read_host_memory()
    jobs, resources = await asyncio.to_thread(_read_dashboard_tracking, snap.get("warm_sessions", []))
    snap["tracked_jobs"] = jobs
    snap["session_resources"] = resources
    return snap


def _read_dashboard_tracking(warm_sessions: list[Dict[str, Any]]):
    bot = peek_cloud_telegram_bot()
    intake = bot.list_intake_jobs() if bot else []
    live_jobs = store.list_live_jobs()
    active = []
    tracked = []
    queue_keys = set()
    for job in live_jobs:
        channel = resolve_cloud_source_channel(job.options, job.idempotency_key)
        row = {
            "job_id": job.job_id, "attempt": job.attempt, "state": job.state,
            "video_name": redact_secrets(job.video_name), "gpu_type": job.gpu_type,
            "session_name": job.session_name, "percent": job.percent, "step_name": job.step_name,
            "source": "Telegram" if channel == "cloud_telegram_bot" else "Dashboard",
        }
        tracked.append(row)
        if job.idempotency_key:
            queue_keys.add(job.idempotency_key)
        if job.state != "queued":
            active.append(row)
    for row in intake:
        if row["queue_key"] not in queue_keys:
            tracked.append({key: value for key, value in row.items() if key != "queue_key"})
    return tracked, _read_cloud_session_resources(active, warm_sessions)


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


@router.post("/api/cloud/concurrency")
async def api_cloud_set_concurrency(payload: Dict[str, Any] = Body(...)):
    """Persist the maximum number of simultaneous Cloud jobs (1–4)."""
    raw_limit = payload.get("max_concurrent_jobs") if isinstance(payload, dict) else None
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise HTTPException(status_code=400, detail="Số luồng Cloud phải là số nguyên.")
    if not 1 <= raw_limit <= MAX_CONCURRENT_JOBS:
        raise HTTPException(
            status_code=400,
            detail=f"Số luồng Cloud phải từ 1 đến {MAX_CONCURRENT_JOBS}.",
        )
    if "gpu_type" in payload:
        gpu = _checked_gpu_option(payload["gpu_type"])
        await asyncio.to_thread(store.set_gpu_concurrency_limits, {gpu: raw_limit})
        return {"status": "ok", "gpu_type": gpu, "max_concurrent_jobs": raw_limit}
    limit = await asyncio.to_thread(store.set_concurrency_limit, raw_limit)
    return {"status": "ok", "max_concurrent_jobs": limit}


@router.get("/api/cloud/gpu-settings")
async def api_cloud_gpu_settings():
    default_gpu, history, limits = await asyncio.gather(
        asyncio.to_thread(get_cloud_default_gpu, store),
        asyncio.to_thread(store.list_gpu_evidence),
        asyncio.to_thread(store.get_gpu_concurrency_limits),
    )
    by_gpu = {item["gpu_type"]: item for item in history}
    return {
        "status": "ok",
        "default_gpu": default_gpu,
        "gpu_concurrency_limits": limits,
        "gpus": [
            {
                "gpu_type": gpu,
                "estimated_cu_per_hour": GPU_CU_RATES[gpu],
                "evidence": by_gpu.get(gpu),
            }
            for gpu in CHECKABLE_GPUS
        ],
    }


@router.post("/api/cloud/gpu-settings")
async def api_cloud_save_gpu_settings(payload: Dict[str, Any] = Body(...)):
    gpu = _checked_gpu_option(payload.get("gpu_type"))
    await asyncio.to_thread(store.set_setting, DASHBOARD_GPU_SETTING, gpu)
    return {"status": "ok", "default_gpu": gpu}


@router.post("/api/cloud/gpu-check")
async def api_cloud_gpu_check(payload: Dict[str, Any] = Body(...)):
    gpu = _checked_gpu_option(payload.get("gpu_type"))
    permit = payload.get("rollout_permit")
    if not isinstance(permit, str) and store.rollout_controlled:
        raise HTTPException(status_code=409, detail="Mở một lượt Cloud trước khi thử cấp GPU.")
    estimated_cu = GPU_CU_RATES[gpu] * 150 / 3600
    usage = await _fresh_usage_for_admission()
    decision = await asyncio.to_thread(
        store.check_admission_allowed, gpu_type=gpu, cu_estimated=estimated_cu, provider_usage=usage,
    )
    if not decision.get("allowed"):
        raise HTTPException(
            status_code=decision.get("http_status") or 409,
            detail=redact_secrets(decision.get("reason") or "Chưa xác nhận được hạn mức CU."),
        )
    try:
        job, _ = await asyncio.to_thread(
            store.enqueue_or_get,
            video_path="",
            video_name=f"Thử cấp GPU {gpu}",
            gpu_type=gpu,
            task_type=GPU_CHECK_TASK,
            cu_estimated=estimated_cu,
            rollout_permit=permit,
            options={"source_channel": "dashboard", "execution_mode": "cloud"},
        )
    except CloudValidationError as exc:
        raise HTTPException(status_code=exc.http_status, detail=redact_secrets(str(exc))) from exc
    worker = get_cloud_worker()
    if not worker.is_running():
        await asyncio.to_thread(worker.start)
    return {"status": "ok", "job_id": job.job_id, "gpu_type": gpu}


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

    gpu_type = payload["gpu_type"] if "gpu_type" in payload else await asyncio.to_thread(get_cloud_default_gpu, store)
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

    fresh_usage = await _fresh_usage_for_admission()

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
    if target_worker is not None:
        try:
            if target_worker.get_current_job(job_id) is None:
                target_worker = None
        except TypeError:
            current = target_worker.get_current_job()
            if current is None or current.job_id != job_id:
                target_worker = None
    bot_worker = peek_cloud_telegram_bot_worker()
    if bot_worker is not None:
        try:
            cur_bot_job = bot_worker.get_current_job(job_id)
        except TypeError:
            current = bot_worker.get_current_job()
            cur_bot_job = current if current is not None and current.job_id == job_id else None
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
    if ok and hasattr(store, "remove_warm_session"):
        try:
            await asyncio.to_thread(store.remove_warm_session, session_name)
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
