"""
Phase A API Routes - Presets, Per-Video Config, and Bulk Enqueue.
Hỗ trợ cả Tool V1 và Tool V2 thông qua FastAPI APIRouter.
"""

import os
import sys
import uuid
import time
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Request, Body, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger("phase_a_routes")

router = APIRouter(tags=["Phase A - Presets & Job Config"])

try:
    import preset_service
    import job_config_service
except ImportError:
    from . import preset_service
    from . import job_config_service


# ===== PRESETS CRUD =====

@router.get("/api/presets")
async def api_list_presets(tool: str = Query("both", description="Lọc theo phiên bản tool: v1, v2 hoặc both")):
    """Liệt kê toàn bộ các preset cấu hình hiện có."""
    try:
        presets = preset_service.list_presets(tool=tool)
        return {"status": "ok", "presets": presets, "total": len(presets)}
    except Exception as e:
        logger.exception("Lỗi api_list_presets")
        raise HTTPException(500, f"Không thể tải danh sách preset: {e}")


@router.post("/api/presets")
async def api_create_preset(payload: Dict[str, Any] = Body(...)):
    """Tạo mới một preset cấu hình."""
    try:
        new_preset = preset_service.create_preset(payload)
        return {"status": "created", "preset": new_preset}
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    except Exception as e:
        logger.exception("Lỗi api_create_preset")
        raise HTTPException(500, f"Lỗi tạo preset: {e}")


@router.get("/api/presets/{preset_id}")
async def api_get_preset(preset_id: str):
    """Lấy chi tiết 1 preset theo ID."""
    preset = preset_service.get_preset(preset_id)
    if not preset:
        raise HTTPException(404, f"Không tìm thấy preset '{preset_id}'")
    return {"status": "ok", "preset": preset}


@router.patch("/api/presets/{preset_id}")
async def api_update_preset(preset_id: str, payload: Dict[str, Any] = Body(...)):
    """Cập nhật preset (kiểm tra optimistic locking với revision)."""
    expected_rev = payload.get("expected_revision")
    try:
        updated = preset_service.update_preset(preset_id, payload, expected_revision=expected_rev)
        return {"status": "updated", "preset": updated}
    except KeyError:
        raise HTTPException(404, f"Không tìm thấy preset '{preset_id}'")
    except ValueError as ve:
        raise HTTPException(409, str(ve))
    except Exception as e:
        logger.exception("Lỗi api_update_preset")
        raise HTTPException(500, f"Lỗi cập nhật preset: {e}")


@router.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str, expected_revision: Optional[int] = Query(None)):
    """Xóa một preset."""
    try:
        preset_service.delete_preset(preset_id, expected_revision=expected_revision)
        return {"status": "deleted", "preset_id": preset_id}
    except KeyError:
        raise HTTPException(404, f"Không tìm thấy preset '{preset_id}'")
    except ValueError as ve:
        raise HTTPException(409, str(ve))
    except Exception as e:
        logger.exception("Lỗi api_delete_preset")
        raise HTTPException(500, f"Lỗi xóa preset: {e}")


# ===== CONFIG RESOLUTION =====

@router.post("/api/config/resolve")
async def api_resolve_config(payload: Dict[str, Any] = Body(...)):
    """
    Xem trước cấu hình thực tế (effective config) sau khi tổng hợp:
    Mặc định hệ thống -> Preset được chọn -> Cấu hình riêng video.
    """
    preset_id = payload.get("preset_id")
    video_overrides = payload.get("video_overrides") or payload.get("overrides")
    system_defaults = payload.get("system_defaults")

    result = preset_service.resolve_effective_config(
        preset_id=preset_id,
        video_overrides=video_overrides,
        system_defaults=system_defaults,
    )
    return {"status": "ok", **result}


# ===== VIDEO DRAFTS (Bản nháp cấu hình riêng từng video) =====

@router.get("/api/video-drafts")
async def api_get_all_video_drafts():
    """Lấy danh sách bản nháp cấu hình riêng cho tất cả video."""
    drafts = job_config_service.list_all_drafts()
    return {"status": "ok", "drafts": drafts}


@router.get("/api/video-draft/{video_name}")
async def api_get_video_draft(video_name: str):
    """Lấy bản nháp cấu hình riêng của 1 video."""
    draft = job_config_service.get_video_draft(video_name)
    return {"status": "ok", "draft": draft}


@router.post("/api/video-draft/{video_name}")
async def api_save_video_draft(video_name: str, payload: Dict[str, Any] = Body(...)):
    """Lưu hoặc cập nhật bản nháp cấu hình riêng cho video."""
    saved = job_config_service.save_video_draft(video_name, payload)
    return {"status": "saved", "draft": saved}


@router.delete("/api/video-draft/{video_name}")
async def api_delete_video_draft(video_name: str):
    """Xóa bản nháp cấu hình riêng của video (khôi phục dùng preset mặc định)."""
    deleted = job_config_service.delete_video_draft(video_name)
    return {"status": "deleted" if deleted else "not_found", "video_name": video_name}


# ===== QUEUED JOB CONFIG PATCH =====

@router.patch("/api/jobs/{job_id}/config")
async def api_patch_queued_job_config(job_id: str, payload: Dict[str, Any] = Body(...)):
    """Cập nhật cấu hình của một job đang chờ trước khi worker nhận chạy."""
    preset_id = payload.get("preset_id")
    overrides = payload.get("overrides")
    expected_rev = payload.get("expected_revision")
    try:
        updated = job_config_service.update_queued_job_config(
            key=job_id,
            preset_id=preset_id,
            overrides=overrides,
            expected_revision=expected_rev,
        )
        return {"status": "updated", "config": updated}
    except KeyError:
        raise HTTPException(404, f"Không tìm thấy cấu hình job '{job_id}'")
    except ValueError as ve:
        raise HTTPException(409, str(ve))


# ===== BULK ENQUEUE (Chọn nhiều video & Tóm tắt trước chạy) =====

@router.post("/api/jobs/batch")
async def api_jobs_batch(request: Request, payload: Dict[str, Any] = Body(...)):
    """
    Xếp hàng hàng loạt video với cấu hình đóng băng riêng từng video.
    Hỗ trợ Idempotency key ngăn chặn double-click / gửi trùng.
    """
    idempotency_key = payload.get("idempotency_key")
    if idempotency_key:
        cached = job_config_service.check_idempotency(idempotency_key)
        if cached:
            logger.info("Trả về kết quả idempotency đã cache cho key: %s", idempotency_key)
            return JSONResponse(status_code=200, content=cached)

    items = payload.get("items", [])
    if not items:
        raise HTTPException(400, "Danh sách video rỗng (items is required).")

    batch_id = payload.get("batch_id") or f"batch_{uuid.uuid4().hex[:8]}"
    trigger_now = bool(payload.get("trigger", True))
    batch_video_mode = str(payload.get("video_mode") or "auto").strip() or "auto"
    
    frozen_items = []
    for it in items:
        filename = it.get("filename") or it.get("name")
        if not filename:
            continue
        p_id = it.get("preset_id")
        ovr = dict(it.get("overrides") or {})
        # Freeze the dashboard's S/M/L choice with the item. A later global
        # mode change must not alter a job that is already queued.
        ovr.setdefault("video_mode", str(it.get("video_mode") or batch_video_mode))
        job_id = it.get("job_id") or f"{batch_id}_{filename}"

        # Đóng băng cấu hình cho video này
        snapshot = job_config_service.freeze_job_config(
            video_name=filename,
            preset_id=p_id,
            overrides=ovr,
            job_id=job_id,
            batch_id=batch_id,
        )
        frozen_items.append({
            "filename": filename,
            "job_id": job_id,
            "preset_id": p_id,
            "preset_name": snapshot.get("preset_name"),
            "effective_config": snapshot.get("effective_config"),
            "warnings": snapshot.get("warnings", []),
        })

    # Trigger batch execution nếu được yêu cầu
    execution_triggered = False
    message = f"Đã xếp hàng {len(frozen_items)} video với cấu hình đóng băng."
    
    if trigger_now:
        # Kiểm tra xem đang chạy trong V1 hay V2
        is_v2 = "tool v2" in str(Path(__file__).resolve()).lower()
        if is_v2:
            # Trigger V2 batch
            try:
                from dashboard_monitor import is_v2_batch_running, is_v2_paused, api_run_batch
                if not is_v2_paused() and not is_v2_batch_running():
                    import asyncio
                    asyncio.create_task(api_run_batch(None, video_mode=batch_video_mode))
                    execution_triggered = True
                    message += " Đang khởi chạy tiến trình xử lý V2..."
            except Exception as e:
                logger.warning("Không thể tự kích hoạt batch V2: %s", e)
        else:
            # Trigger V1 batch
            try:
                from main import UNIFIED_PIPELINE_LOCK, BATCH_TASK, api_run_batch
                import asyncio
                asyncio.create_task(api_run_batch(None, video_mode=batch_video_mode))
                execution_triggered = True
                message += " Đang khởi chạy tiến trình xử lý V1..."
            except Exception as e:
                logger.warning("Không thể tự kích hoạt batch V1: %s", e)

    response_data = {
        "status": "started" if execution_triggered else "enqueued",
        "batch_id": batch_id,
        "total_items": len(frozen_items),
        "items": frozen_items,
        "message": message,
    }

    if idempotency_key:
        job_config_service.record_idempotency(idempotency_key, response_data)

    return JSONResponse(status_code=200, content=response_data)
