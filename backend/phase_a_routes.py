"""
Phase A API Routes - Presets, Per-Video Config, and Bulk Enqueue cho Tool V1.
"""

import uuid
import logging
from pathlib import Path
from typing import Dict, Any, Optional
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

    try:
        result = preset_service.resolve_effective_config(
            preset_id=preset_id,
            video_overrides=video_overrides,
            system_defaults=system_defaults,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc))
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
    try:
        preset_service.resolve_effective_config(preset_id=payload.get("preset_id"), video_overrides=payload.get("overrides"))
        saved = job_config_service.save_video_draft(video_name, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc))
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
    import asyncio
    from batch_plan_service import BatchPlanStore, PlanConflict, public_plan
    runtime = getattr(request.app.state, "pipeline_runtime", None)
    if runtime is None:
        raise HTTPException(503, "Pipeline runtime chưa sẵn sàng.")
    store = BatchPlanStore(runtime.WORKSPACE)
    try:
        plan = await asyncio.to_thread(store.prepare, payload, runtime.get_input_dir(), runtime.get_output_dir(), job_config_service.freeze_job_config)
        if payload.get("trigger", True):
            return await runtime.admit_batch_plan(plan["batch_id"])
        return JSONResponse(status_code=201, content={**public_plan(plan), "message": "Đã lưu tập video và cấu hình; chưa khởi chạy."})
    except PlanConflict as exc:
        raise HTTPException(409, {"code": "batch_conflict", "message": str(exc)})
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, {"code": "invalid_batch", "message": str(exc)})


def _safe_bare_name(value, label: str = "Tên video") -> str:
    """Chấp nhận tên trần hợp lệ cho Windows — chặn path traversal từ client."""
    clean = str(value or "").strip()
    if not clean or clean in (".", "..") or clean != Path(clean).name:
        raise HTTPException(400, f"{label} không hợp lệ (không cho phép đường dẫn).")
    if any(ch in '<>:"|?*' for ch in clean):
        raise HTTPException(400, f"{label} chứa ký tự không hợp lệ.")
    return clean
