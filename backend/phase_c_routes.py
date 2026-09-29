# -*- coding: utf-8 -*-
"""
FastAPI Routes for Phase C: Segment Editing, Selective Regeneration & Revised Video Publishing.
Mountable on both Tool V1 (main.py) and Tool V2 (dashboard_monitor.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from segment_editor_service import (
    get_job_segments,
    get_segment_audio_path,
    publish_revised_video,
    regenerate_segments,
    save_segment_draft,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Segment Editor (Phase C)"])


class SaveSegmentsDraftRequest(BaseModel):
    expected_revision: int = Field(1, description="Phiên bản mong muốn để kiểm tra xung đột")
    segments: List[Dict[str, Any]] = Field(..., description="Danh sách các câu đã chỉnh sửa")


class RegenerateSegmentsRequest(BaseModel):
    segment_ids: List[str] = Field(..., description="Danh sách các mã câu cần tạo lại âm thanh (ví dụ: ['seg_1', 'seg_3'])")
    expected_revision: int = Field(1, description="Phiên bản mong đợi")
    idempotency_key: Optional[str] = Field("", description="Mã chống submit trùng lặp")


class PublishRevisedRequest(BaseModel):
    expected_revision: int = Field(1, description="Phiên bản xuất bản")


@router.get("/api/jobs/{job_id}/segments")
async def api_get_job_segments(job_id: str):
    """
    Lấy danh sách phân đoạn câu (transcript / phụ đề / âm thanh) của video theo job_id.
    """
    if not job_id or not job_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu mã job_id")

    try:
        data = get_job_segments(job_id.strip())
        return data
    except Exception as exc:
        logger.error("Lỗi lấy segments cho %s: %s", job_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.patch("/api/jobs/{job_id}/segments")
async def api_save_job_segments_draft(job_id: str, req: SaveSegmentsDraftRequest):
    """
    Lưu bản nháp chỉnh sửa câu với kiểm tra khóa lạc quan (optimistic locking).
    """
    if not job_id or not job_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu mã job_id")

    try:
        res = save_segment_draft(
            job_id_or_name=job_id.strip(),
            updated_segments=req.segments,
            expected_revision=req.expected_revision,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=409, detail=str(val_err))
    except Exception as exc:
        logger.error("Lỗi lưu bản nháp segment %s: %s", job_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/jobs/{job_id}/regenerate")
async def api_regenerate_job_segments(job_id: str, req: RegenerateSegmentsRequest):
    """
    Tạo lại âm thanh cho đúng các câu đã chọn.
    Tự động nén atempo nếu câu vượt khung thời lượng, sao lưu file cũ và cập nhật bản nháp.
    """
    if not job_id or not job_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu mã job_id")
    if not req.segment_ids:
        raise HTTPException(status_code=400, detail="Danh sách câu cần tạo lại không được để rỗng")

    try:
        res = await regenerate_segments(
            job_id_or_name=job_id.strip(),
            segment_ids=req.segment_ids,
            expected_revision=req.expected_revision,
            idempotency_key=req.idempotency_key or "",
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=409, detail=str(val_err))
    except Exception as exc:
        logger.error("Lỗi tạo lại câu cho %s: %s", job_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/jobs/{job_id}/publish-revised")
async def api_publish_revised_job(job_id: str, req: PublishRevisedRequest):
    """
    Xuất video thành phẩm bản sửa mới (Dubbed_..._rev{revision}.mp4).
    Tuyệt đối giữ nguyên video thành phẩm cũ, remux track âm thanh mới với video gốc.
    """
    if not job_id or not job_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu mã job_id")

    try:
        res = await publish_revised_video(
            job_id_or_name=job_id.strip(),
            expected_revision=req.expected_revision,
        )
        return res
    except ValueError as val_err:
        raise HTTPException(status_code=409, detail=str(val_err))
    except Exception as exc:
        logger.error("Lỗi xuất bản sửa mới cho %s: %s", job_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/jobs/{job_id}/segments/{segment_id}/audio")
async def api_get_segment_audio(job_id: str, segment_id: str):
    """
    Phục vụ stream audio MP3 của một câu cụ thể để phát nghe thử trên player.
    """
    p = get_segment_audio_path(job_id.strip(), segment_id.strip())
    if not p or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file âm thanh của phân đoạn này.")

    return FileResponse(
        path=str(p),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )
