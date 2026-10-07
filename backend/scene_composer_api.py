# -*- coding: utf-8 -*-
"""FastAPI routes for Scene Motion Composer in Tool V2."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from ai.scene_composer_service import (
    MOTION_LABELS,
    MOTION_TYPES,
    compute_composer_timeline,
    get_audio_duration,
    parse_srt_file,
    render_scene_composer_video,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scene-composer", tags=["Scene Composer"])

TASKS: Dict[str, Dict[str, Any]] = {}
TASKS_LOCK = threading.Lock()


class TimelinePreviewRequest(BaseModel):
    image_dir: Optional[str] = Field(None, description="Thư mục chứa ảnh phân cảnh")
    image_paths: Optional[List[str]] = Field(None, description="Danh sách đường dẫn ảnh cụ thể")
    audio_path: str = Field(..., description="Đường dẫn file âm thanh/lồng tiếng")
    srt_path: Optional[str] = Field(None, description="Đường dẫn file phụ đề SRT (tùy chọn)")
    default_motion: str = Field("auto", description="Kiểu chuyển động camera mặc định")


class RenderSceneVideoRequest(BaseModel):
    image_dir: Optional[str] = None
    image_paths: Optional[List[str]] = None
    audio_path: str
    srt_path: Optional[str] = None
    output_path: Optional[str] = None
    aspect_ratio: str = Field("9:16", description="'9:16', '16:9', hoặc '1:1'")
    default_motion: str = Field("auto", description="Kiểu chuyển động camera")
    fps: int = Field(30, description="Tốc độ khung hình (mặc định 30)")


def _gather_images(
    image_dir: Optional[str], image_paths: Optional[List[str]]
) -> List[Path]:
    results = []
    if image_paths:
        for p in image_paths:
            f = Path(p).resolve()
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                results.append(f)
    elif image_dir:
        d = Path(image_dir).resolve()
        if d.is_dir():
            valid_exts = {".png", ".jpg", ".jpeg", ".webp"}
            for f in sorted(d.iterdir()):
                if f.is_file() and f.suffix.lower() in valid_exts:
                    results.append(f)
    return results


def _run_composer_task(task_id: str, req: RenderSceneVideoRequest) -> None:
    audio = Path(req.audio_path).resolve()
    images = _gather_images(req.image_dir, req.image_paths)

    if not images:
        with TASKS_LOCK:
            TASKS[task_id].update({
                "status": "error",
                "error": "Không tìm thấy file ảnh hợp lệ nào trong thư mục!",
            })
        return

    dur = get_audio_duration(audio)
    subs = parse_srt_file(req.srt_path) if req.srt_path else None
    timeline = compute_composer_timeline(
        images, dur, subtitles=subs, default_motion=req.default_motion
    )

    if not req.output_path:
        out_name = f"composed_{int(time.time())}.mp4"
        dest = audio.parent / out_name
    else:
        dest = Path(req.output_path).resolve()

    def on_progress(pct: int, msg: str) -> None:
        with TASKS_LOCK:
            if task_id in TASKS:
                TASKS[task_id].update({
                    "percent": pct,
                    "status_text": msg,
                })

    try:
        with TASKS_LOCK:
            TASKS[task_id]["status"] = "processing"

        result = render_scene_composer_video(
            timeline=timeline,
            audio_path=audio,
            output_path=dest,
            total_audio_duration=dur,
            aspect_ratio=req.aspect_ratio,
            fps=req.fps,
            progress_callback=on_progress,
        )

        with TASKS_LOCK:
            if result.get("success"):
                TASKS[task_id].update({
                    "status": "completed",
                    "percent": 100,
                    "output_path": str(dest),
                    "result": result,
                    "status_text": "Hoàn tất dựng video thành công!",
                })
            else:
                TASKS[task_id].update({
                    "status": "error",
                    "error": result.get("error", "Lỗi render video"),
                    "status_text": "Lỗi render video",
                })
    except Exception as e:
        logger.error(f"Lỗi task composer {task_id}: {e}", exc_info=True)
        with TASKS_LOCK:
            TASKS[task_id].update({
                "status": "error",
                "error": str(e),
                "status_text": f"Lỗi: {e}",
            })


@router.get("/motions")
async def api_get_motions() -> Dict[str, Any]:
    """Lấy danh sách các hiệu ứng chuyển động máy quay."""
    items = []
    for k in MOTION_TYPES:
        items.append({"type": k, "label": MOTION_LABELS.get(k, k)})
    return {"status": "success", "motions": items}


@router.post("/preview-timeline")
async def api_preview_timeline(req: TimelinePreviewRequest) -> Dict[str, Any]:
    """Xem trước mốc thời gian hiển thị và hiệu ứng từng cảnh trước khi dựng."""
    audio = Path(req.audio_path)
    if not audio.is_file():
        raise HTTPException(status_code=400, detail="Không tìm thấy file âm thanh")

    images = _gather_images(req.image_dir, req.image_paths)
    if not images:
        raise HTTPException(
            status_code=400, detail="Không tìm thấy ảnh nào trong đường dẫn đã chọn"
        )

    dur = get_audio_duration(audio)
    subs = parse_srt_file(req.srt_path) if req.srt_path else None
    timeline = compute_composer_timeline(
        images, dur, subtitles=subs, default_motion=req.default_motion
    )

    return {
        "status": "success",
        "audio_duration": dur,
        "total_scenes": len(timeline),
        "timeline": timeline,
    }


@router.post("/render")
async def api_render_scene_video(
    req: RenderSceneVideoRequest, bg: BackgroundTasks
) -> Dict[str, Any]:
    """Khởi tạo tác vụ dựng video từ ảnh và âm thanh dưới nền."""
    audio = Path(req.audio_path)
    if not audio.is_file():
        raise HTTPException(status_code=400, detail="Không tìm thấy file âm thanh")

    task_id = f"sc_{uuid.uuid4().hex[:12]}"
    with TASKS_LOCK:
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "pending",
            "percent": 0,
            "status_text": "Đang chuẩn bị dữ liệu...",
            "output_path": None,
            "created_at": time.time(),
        }

    bg.add_task(_run_composer_task, task_id, req)
    return {
        "status": "success",
        "task_id": task_id,
        "message": "Đã bắt đầu tác vụ dựng video phân cảnh",
    }


@router.get("/status/{task_id}")
async def api_get_composer_task_status(task_id: str) -> Dict[str, Any]:
    """Kiểm tra tiến độ tác vụ dựng video."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Không tìm thấy task_id")
    return task
