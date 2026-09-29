# -*- coding: utf-8 -*-
"""
FastAPI Routes for Phase B: Voice Preview & Reference Audio.
Mountable on both Tool V1 (main.py) and Tool V2 (dashboard_monitor.py).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from voice_preview_service import (
    SAMPLE_TEXTS,
    clean_cache,
    get_preview_audio_path,
    get_voice_catalog,
    resolve_verified_sample,
    synthesize_voice_preview,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Voice Preview (Phase B)"])


class VoicePreviewRequest(BaseModel):
    voice_id: str = Field(..., description="Mã giọng đọc (ví dụ: microsoft-hoaimy, capcut-BV562_streaming)")
    text: Optional[str] = Field(None, description="Đoạn văn bản cần đọc thử (tối đa 350 ký tự)")
    speed: Optional[float] = Field(1.0, ge=0.5, le=2.0, description="Tốc độ đọc (0.5x - 2.0x)")
    pitch: Optional[str] = Field("+0Hz", description="Độ cao giọng")


@router.post("/api/voice-preview")
async def api_create_voice_preview(req: VoicePreviewRequest):
    """
    Tạo hoặc lấy âm thanh nghe thử cho một giọng đọc và tốc độ cụ thể.
    Sử dụng bộ đệm (cache) và semaphore đơn luồng để bảo vệ GPU/CPU.
    """
    if not req.voice_id or not req.voice_id.strip():
        raise HTTPException(status_code=400, detail="Thiếu mã giọng đọc (voice_id).")

    try:
        result = await synthesize_voice_preview(
            voice_id=req.voice_id.strip(),
            text=req.text or "",
            speed=req.speed or 1.0,
            pitch=req.pitch or "+0Hz",
        )
        return result
    except Exception as exc:
        logger.error("Lỗi tạo preview giọng %s: %s", req.voice_id, exc, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Không thể tạo âm thanh nghe thử: {str(exc)}"
        )


@router.get("/api/voice-preview/{preview_id}/audio")
async def api_get_voice_preview_audio(preview_id: str):
    """
    Phục vụ stream file audio nghe thử định dạng MP3.
    """
    audio_path = get_preview_audio_path(preview_id)
    if not audio_path or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy file âm thanh xem trước.")

    return FileResponse(
        path=str(audio_path),
        media_type="audio/mpeg",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=86400",
        },
    )


@router.get("/api/voice-preview/sample-texts")
async def api_get_sample_texts():
    """
    Danh sách các câu đọc thử nghiệm mẫu phân theo chủ đề.
    """
    return {"sample_texts": SAMPLE_TEXTS}


@router.get("/api/voice-preview/catalog")
async def api_get_voice_catalog():
    """
    Danh mục giọng đọc hỗ trợ cùng tình trạng mẫu kiểm định có sẵn.
    """
    cat = get_voice_catalog()
    enhanced = []
    for v in cat:
        v_id = v.get("id")
        has_sample = resolve_verified_sample(v_id) is not None
        item = dict(v)
        item["has_verified_sample"] = has_sample
        enhanced.append(item)
    return {"voices": enhanced}


@router.delete("/api/voice-preview/cache")
async def api_clear_voice_preview_cache():
    """
    Dọn dẹp toàn bộ bộ đệm file âm thanh nghe thử.
    """
    try:
        clean_cache(max_size_mb=0, ttl_days=0)
        return {"status": "success", "message": "Đã làm sạch bộ đệm âm thanh nghe thử."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
