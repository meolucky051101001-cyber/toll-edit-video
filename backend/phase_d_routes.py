"""
phase_d_routes.py - REST Endpoints cho Giai đoạn D (Crash Recovery & Smart Resume).
Cung cấp:
- GET  /api/jobs/{job_id}/resume-plan
- POST /api/jobs/{job_id}/resume
- POST /api/jobs/{job_id}/unpause
"""

import logging
import asyncio
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from recovery_service import RecoveryService

logger = logging.getLogger("phase_d_routes")
phase_d_router = APIRouter(prefix="/api/jobs", tags=["Phase D Recovery"])
router = phase_d_router

# Singleton service instance
_service = RecoveryService()


class ResumeRequest(BaseModel):
    mode: str = Field(..., pattern="^(recover|restart|unpause)$")
    plan_token: Optional[str] = Field(None, description="Mã xác thực kế hoạch khôi phục nhận được từ GET /resume-plan")
    config_overrides: Optional[Dict[str, Any]] = Field(None, description="Cấu hình tùy biến nếu muốn đổi thông số")


def _runtime(request):
    runtime = getattr(request.app.state, "pipeline_runtime", None)
    if runtime is None:
        raise HTTPException(409, "Ứng dụng chưa hỗ trợ thực thi khôi phục video.")
    return runtime


@phase_d_router.get("/{job_id}/resume-plan")
async def get_job_resume_plan(job_id: str, request: Request):
    """
    Trả về kế hoạch khôi phục chi tiết cho job:
    - Stage nào tái sử dụng, stage nào phải chạy lại và lý do
    - Danh sách artifact hiện có vs artifact còn thiếu
    - Tình trạng file nguồn và fingerprint
    - Mã token xác thực plan_token cho bước thực thi tiếp theo
    """
    try:
        runtime = _runtime(request)
        plan = await asyncio.to_thread(_service.build_resume_plan,
            job_id=job_id,
            input_dir=runtime.get_input_dir(),
            output_dir=runtime.get_output_dir(),
        )
        return JSONResponse(status_code=200, content=plan)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Lỗi xây dựng kế hoạch khôi phục cho {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Không thể tạo kế hoạch khôi phục: {str(e)}")


@phase_d_router.post("/{job_id}/resume")
async def execute_job_resume(job_id: str, payload: ResumeRequest, request: Request):
    """
    Thực thi hành động khôi phục theo kế hoạch đã xác nhận.
    Hỗ trợ 3 chế độ:
    - 'unpause': Bỏ tạm dừng nếu tác vụ đang sống
    - 'recover': Tái sử dụng các artifact đã hoàn tất, chỉ chạy các bước còn thiếu
    - 'restart': Chạy lại mới 100% từ đầu
    """
    runtime = _runtime(request)
    if payload.mode == "unpause":
        return JSONResponse(content=_service.execute_resume_action(job_id=job_id, mode="unpause"))
    lock = runtime.UNIFIED_PIPELINE_LOCK
    task = runtime.BATCH_TASK
    if lock.locked() or (task is not None and not task.done()) or runtime.job_tracker.get_status().get("active"):
        raise HTTPException(409, "Đang xử lý video; chưa thể khôi phục hoặc chạy lại.")
    await lock.acquire()
    task_id = uuid.uuid4().hex
    dispatched = False
    reserved = False
    try:
        input_dir, output_dir = runtime.get_input_dir(), runtime.get_output_dir()
        # Reserve the shared slot before recovery can remove any artifacts.
        runtime.job_tracker.start_batch(1, str(input_dir), str(output_dir), job_id=task_id)
        reserved = True
        result = await asyncio.to_thread(_service.execute_resume_action,
            job_id=job_id,
            mode=payload.mode,
            plan_token=payload.plan_token,
            config_overrides=payload.config_overrides,
            input_dir=input_dir,
            output_dir=output_dir,
        )

        if not result.get("source_video"):
            raise RuntimeError("Không tìm thấy video nguồn để thực thi khôi phục.")
        runtime.shared_state.stop_requested = False
        runtime.BATCH_TASK = asyncio.create_task(
            runtime.single_video_runner(
                result["source_video"], str(output_dir), task_id,
                recovery_mode=payload.mode, config_overrides=result.get("frozen_config"),
                work_dir=result.get("work_dir"), pipeline_lock_held=True,
            ), name=f"autodub-recovery-{task_id}",
        )
        dispatched = True
        return JSONResponse(content={**result, "dispatched": True, "task_id": task_id})
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except FileNotFoundError as fe:
        raise HTTPException(status_code=404, detail=str(fe))
    except RuntimeError as re:
        raise HTTPException(status_code=409, detail=str(re))
    except Exception as e:
        logger.error(f"Lỗi thực thi khôi phục cho {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Lỗi thực thi: {str(e)}")
    finally:
        if not dispatched:
            if reserved:
                runtime.job_tracker.fail_batch("Không thể khởi chạy khôi phục", job_id=task_id)
                runtime.job_tracker.release_batch(task_id)
            lock.release()


@phase_d_router.post("/{job_id}/unpause")
async def unpause_job_shortcut(job_id: str):
    """Shortcut endpoint để tiếp tục nhanh tác vụ đang tạm dừng."""
    res = _service.execute_resume_action(job_id=job_id, mode="unpause")
    return JSONResponse(status_code=200, content=res)
