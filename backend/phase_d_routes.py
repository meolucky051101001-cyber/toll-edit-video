"""
phase_d_routes.py - REST Endpoints cho Giai đoạn D (Crash Recovery & Smart Resume).
Cung cấp:
- GET  /api/jobs/{job_id}/resume-plan
- POST /api/jobs/{job_id}/resume
- POST /api/jobs/{job_id}/unpause
"""

import logging
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
    mode: str = Field(..., description="Chế độ thực thi: 'recover', 'restart' hoặc 'unpause'")
    plan_token: Optional[str] = Field(None, description="Mã xác thực kế hoạch khôi phục nhận được từ GET /resume-plan")
    config_overrides: Optional[Dict[str, Any]] = Field(None, description="Cấu hình tùy biến nếu muốn đổi thông số")


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
        input_dir = None
        output_dir = None
        try:
            from dashboard_monitor import get_input_dir, get_output_dir
            input_dir = get_input_dir()
            output_dir = get_output_dir()
        except ImportError:
            try:
                from main import get_input_dir, get_output_dir
                input_dir = get_input_dir()
                output_dir = get_output_dir()
            except ImportError:
                pass

        plan = _service.build_resume_plan(
            job_id=job_id,
            input_dir=input_dir,
            output_dir=output_dir
        )
        return JSONResponse(status_code=200, content=plan)
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
    try:
        input_dir = None
        output_dir = None
        try:
            from dashboard_monitor import get_input_dir, get_output_dir
            input_dir = get_input_dir()
            output_dir = get_output_dir()
        except ImportError:
            try:
                from main import get_input_dir, get_output_dir
                input_dir = get_input_dir()
                output_dir = get_output_dir()
            except ImportError:
                pass

        result = _service.execute_resume_action(
            job_id=job_id,
            mode=payload.mode,
            plan_token=payload.plan_token,
            config_overrides=payload.config_overrides,
            input_dir=input_dir,
            output_dir=output_dir,
        )

        if payload.mode in ["recover", "restart"]:
            clean_name = job_id.replace("Dubbed_", "").replace(".mp4", "").strip()
            source_file = result.get("source_video")
            if source_file:
                # Dispatch for Tool V2
                try:
                    import dashboard_monitor as v2_main
                    import subprocess, sys
                    if hasattr(v2_main, "is_v2_batch_running") and not v2_main.is_v2_batch_running():
                        python_exe = v2_main.ROOT / "venv" / "Scripts" / "python.exe"
                        if not python_exe.exists():
                            python_exe = Path(sys.executable)
                        script = v2_main.ROOT / "batch_processor.py"
                        if script.is_file():
                            v2_main.BATCH_PROCESS = subprocess.Popen([str(python_exe), str(script)])
                            result["dispatched"] = True
                except Exception as ex:
                    logger.info(f"V2 direct dispatch check: {ex}")

        return JSONResponse(status_code=200, content=result)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except FileNotFoundError as fe:
        raise HTTPException(status_code=404, detail=str(fe))
    except RuntimeError as re:
        raise HTTPException(status_code=409, detail=str(re))
    except Exception as e:
        logger.error(f"Lỗi thực thi khôi phục cho {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Lỗi thực thi: {str(e)}")


@phase_d_router.post("/{job_id}/unpause")
async def unpause_job_shortcut(job_id: str):
    """Shortcut endpoint để tiếp tục nhanh tác vụ đang tạm dừng."""
    res = _service.execute_resume_action(job_id=job_id, mode="unpause")
    return JSONResponse(status_code=200, content=res)
