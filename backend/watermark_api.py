# -*- coding: utf-8 -*-
"""FastAPI routes for Watermark & Logo Removal in Tool V2."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from ai.watermark_removal_service import get_watermark_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watermark", tags=["Watermark Remover"])

# Control config file for Auto-Pipeline watermark setting
BASE_DIR = Path(__file__).resolve().parent
CONTROL_DIR = Path(
    os.getenv("TOOL_V2_CONTROL_DIR", str(BASE_DIR.parent / "workspace" / "control"))
)
CONTROL_DIR.mkdir(parents=True, exist_ok=True)
WATERMARK_CONFIG_FILE = CONTROL_DIR / "v2_watermark_config.json"

# In-memory progress tracking for video processing tasks
TASKS: Dict[str, Dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()


class VideoWatermarkRequest(BaseModel):
    input_path: str = Field(..., description="Đường dẫn tuyệt đối tới video đầu vào")
    output_path: Optional[str] = Field(None, description="Đường dẫn video kết quả (tùy chọn)")
    mode: str = Field("both", description="Chế độ: 'both', 'gemini', 'veo3', 'custom'")
    gain: float = Field(0.6, description="Độ đậm nhạt của watermark mask (mặc định 0.6)")
    scale: float = Field(1.01, description="Tỉ lệ scale kích thước box (mặc định 1.01)")
    offset_x: int = Field(-24, description="Độ dịch tọa độ X (mặc định -24)")
    offset_y: int = Field(-24, description="Độ dịch tọa độ Y (mặc định -24)")
    custom_box: Optional[Dict[str, int]] = Field(
        None, description="Tọa độ custom box: {'x': int, 'y': int, 'width': int, 'height': int}"
    )
    custom_method: str = Field("inpaint", description="Phương pháp custom: 'inpaint' hoặc 'blur'")


class ImageWatermarkRequest(BaseModel):
    input_path: str = Field(..., description="Đường dẫn ảnh đầu vào")
    output_path: Optional[str] = Field(None, description="Đường dẫn ảnh đầu ra")
    mode: str = Field("gemini", description="Chế độ: 'gemini' hoặc 'custom'")
    gain: float = Field(0.6, description="Gain mask")
    custom_box: Optional[Dict[str, int]] = None


class WatermarkConfigModel(BaseModel):
    auto_remove: bool = Field(False, description="Tự động gỡ watermark cho video đầu vào trong pipeline")
    mode: str = Field("both", description="Chế độ mặc định: 'both', 'gemini', 'veo3'")
    gain: float = Field(0.6, description="Gain mask mặc định")


def get_watermark_config() -> Dict[str, Any]:
    """Đọc cấu hình tự động gỡ watermark."""
    if WATERMARK_CONFIG_FILE.is_file():
        try:
            return json.loads(WATERMARK_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "auto_remove": False,
        "mode": "both",
        "gain": 0.6,
        "updated_at": "",
    }


def save_watermark_config(cfg: Dict[str, Any]) -> None:
    """Lưu cấu hình tự động gỡ watermark."""
    tmp = WATERMARK_CONFIG_FILE.with_suffix(".tmp")
    cfg["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, WATERMARK_CONFIG_FILE)


def _run_video_task(task_id: str, req: VideoWatermarkRequest) -> None:
    svc = get_watermark_service()
    src = Path(req.input_path).resolve()

    if not req.output_path:
        out_name = f"{src.stem}_clean{src.suffix}"
        dest = src.parent / out_name
    else:
        dest = Path(req.output_path).resolve()

    def on_progress(processed: int, total: Optional[int], pct: float) -> None:
        with TASKS_LOCK:
            if task_id in TASKS:
                TASKS[task_id].update({
                    "frames_processed": processed,
                    "total_frames": total,
                    "percent": pct,
                })

    try:
        with TASKS_LOCK:
            TASKS[task_id]["status"] = "processing"

        result = svc.process_video(
            input_path=src,
            output_path=dest,
            mode=req.mode,
            gain=req.gain,
            scale=req.scale,
            offset_x=req.offset_x,
            offset_y=req.offset_y,
            custom_box=req.custom_box,
            custom_method=req.custom_method,
            progress_callback=on_progress,
        )

        with TASKS_LOCK:
            if result.get("success"):
                TASKS[task_id].update({
                    "status": "completed",
                    "percent": 100.0,
                    "output_path": str(dest),
                    "result": result,
                })
            else:
                TASKS[task_id].update({
                    "status": "error",
                    "error": result.get("error", "Xử lý thất bại"),
                })
    except Exception as e:
        logger.error(f"Lỗi task watermark {task_id}: {e}", exc_info=True)
        with TASKS_LOCK:
            TASKS[task_id].update({
                "status": "error",
                "error": str(e),
            })


@router.post("/remove-video")
async def api_remove_video_watermark(
    req: VideoWatermarkRequest, bg: BackgroundTasks
) -> Dict[str, Any]:
    """Tạo tác vụ xử lý xóa watermark video dưới nền."""
    src = Path(req.input_path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"Không tìm thấy file: {req.input_path}")

    task_id = f"wm_{uuid.uuid4().hex[:12]}"
    with TASKS_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "percent": 0.0,
            "frames_processed": 0,
            "total_frames": None,
            "input_path": str(src),
            "output_path": None,
            "created_at": time.time(),
        }

    bg.add_task(_run_video_task, task_id, req)

    return {
        "status": "success",
        "task_id": task_id,
        "message": "Đã bắt đầu tác vụ gỡ watermark video",
    }


@router.get("/status/{task_id}")
async def api_get_task_status(task_id: str) -> Dict[str, Any]:
    """Kiểm tra tiến độ tác vụ gỡ watermark."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Không tìm thấy task_id")
    return task


@router.post("/remove-image")
async def api_remove_image_watermark(req: ImageWatermarkRequest) -> Dict[str, Any]:
    """Xóa watermark trên ảnh tĩnh (chạy trực tiếp)."""
    src = Path(req.input_path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail=f"Không tìm thấy file ảnh: {req.input_path}")

    if not req.output_path:
        dest = src.parent / f"{src.stem}_clean{src.suffix}"
    else:
        dest = Path(req.output_path)

    svc = get_watermark_service()
    try:
        res = await asyncio.to_thread(
            svc.process_image,
            input_path=src,
            output_path=dest,
            mode=req.mode,
            gain=req.gain,
            custom_box=req.custom_box,
        )
        return {"status": "success", "result": res}
    except Exception as e:
        logger.error(f"Lỗi xóa watermark ảnh: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config")
async def api_get_watermark_config() -> Dict[str, Any]:
    """Lấy cấu hình watermark của hệ thống."""
    return get_watermark_config()


@router.post("/config")
async def api_set_watermark_config(req: WatermarkConfigModel) -> Dict[str, Any]:
    """Lưu cấu hình watermark tự động."""
    cfg = get_watermark_config()
    cfg["auto_remove"] = req.auto_remove
    cfg["mode"] = req.mode
    cfg["gain"] = req.gain
    save_watermark_config(cfg)
    return {"status": "success", "config": cfg}


@router.get("/probe")
async def api_probe_video(path: str) -> Dict[str, Any]:
    """Lấy thông tin kích thước và thời lượng video."""
    src = Path(path)
    if not src.is_file():
        raise HTTPException(status_code=400, detail="Không tìm thấy file")
    svc = get_watermark_service()
    try:
        info = await asyncio.to_thread(svc.probe_video, src)
        return {
            "status": "success",
            "width": info.width,
            "height": info.height,
            "fps": info.fps,
            "duration": info.duration,
            "total_frames": info.total_frames,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
