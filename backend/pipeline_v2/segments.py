"""Portable subtitle segment serialization between isolated processes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional


@dataclass
class GeometryBlock:
    text: str = ""
    start: float = 0.0
    end: float = 0.0
    x_pct: float = 0.0
    max_x_pct: float = 0.0
    y_pct: float = 0.0
    max_y_pct: float = 0.0
    prob: Optional[float] = 1.0
    is_subtitle: Optional[bool] = None
    is_packaging: Optional[bool] = None
    is_static: Optional[bool] = None
    in_subtitle_band: Optional[bool] = None
    type: Optional[str] = None


@dataclass
class RuntimeSegment:
    index: int
    start: timedelta
    end: timedelta
    content: str
    orig_content: Optional[str] = None
    source_segment_id: Optional[int] = None
    y_pct: Optional[float] = None
    max_y_pct: Optional[float] = None
    best_block: Optional[GeometryBlock] = None
    tracking_blocks: List[GeometryBlock] = field(default_factory=list)
    gender: str = "female"
    speaker_id: Optional[str] = None
    is_subtitle: Optional[bool] = None
    is_packaging: Optional[bool] = None
    is_static: Optional[bool] = None
    in_subtitle_band: Optional[bool] = None


def _block_to_dict(block: Any) -> Dict[str, Any]:
    res = {
        "text": str(getattr(block, "text", "")),
        "start": float(getattr(block, "start", 0.0)),
        "end": float(getattr(block, "end", 0.0)),
        "x_pct": float(getattr(block, "x_pct", 0.0)),
        "max_x_pct": float(getattr(block, "max_x_pct", 0.0)),
        "y_pct": float(getattr(block, "y_pct", 0.0)),
        "max_y_pct": float(getattr(block, "max_y_pct", 0.0)),
    }
    for key in ("prob", "is_subtitle", "is_packaging", "is_static", "in_subtitle_band", "type"):
        val = getattr(block, key, None) if not isinstance(block, dict) else block.get(key)
        if val is not None:
            res[key] = val
    return res


def _block_from_dict(data: Mapping[str, Any]) -> GeometryBlock:
    return GeometryBlock(
        text=str(data.get("text", "")),
        start=float(data.get("start", 0.0)),
        end=float(data.get("end", 0.0)),
        x_pct=float(data.get("x_pct", 0.0)),
        max_x_pct=float(data.get("max_x_pct", 0.0)),
        y_pct=float(data.get("y_pct", 0.0)),
        max_y_pct=float(data.get("max_y_pct", 0.0)),
        prob=float(data["prob"]) if data.get("prob") is not None else 1.0,
        is_subtitle=bool(data["is_subtitle"]) if data.get("is_subtitle") is not None else None,
        is_packaging=bool(data["is_packaging"]) if data.get("is_packaging") is not None else None,
        is_static=bool(data["is_static"]) if data.get("is_static") is not None else None,
        in_subtitle_band=bool(data["in_subtitle_band"]) if data.get("in_subtitle_band") is not None else None,
        type=str(data["type"]) if data.get("type") is not None else None,
    )


def segment_to_dict(segment: Any) -> Dict[str, Any]:
    best_block = getattr(segment, "best_block", None)
    tracking = getattr(segment, "tracking_blocks", []) or []
    source_segment_id = getattr(segment, "source_segment_id", None)
    res = {
        "index": int(segment.index),
        "start": float(segment.start.total_seconds()),
        "end": float(segment.end.total_seconds()),
        "content": str(segment.content),
        "orig_content": getattr(segment, "orig_content", None),
        "source_segment_id": (
            int(source_segment_id) if source_segment_id is not None else int(segment.index)
        ),
        "y_pct": getattr(segment, "y_pct", None),
        "max_y_pct": getattr(segment, "max_y_pct", None),
        "best_block": _block_to_dict(best_block) if best_block is not None else None,
        "tracking_blocks": [_block_to_dict(block) for block in tracking],
        "gender": str(getattr(segment, "gender", "female") or "female"),
        "speaker_id": getattr(segment, "speaker_id", None),
    }
    for key in ("is_subtitle", "is_packaging", "is_static", "in_subtitle_band"):
        val = getattr(segment, key, None) if not isinstance(segment, dict) else segment.get(key)
        if val is not None:
            res[key] = val
    return res


def segment_from_dict(data: Mapping[str, Any]) -> RuntimeSegment:
    best_block_data = data.get("best_block")
    source_segment_id = data.get("source_segment_id", data.get("index"))
    return RuntimeSegment(
        index=int(data["index"]),
        start=timedelta(seconds=float(data["start"])),
        end=timedelta(seconds=float(data["end"])),
        content=str(data.get("content", "")),
        orig_content=data.get("orig_content"),
        source_segment_id=(
            int(source_segment_id) if source_segment_id is not None else None
        ),
        y_pct=float(data["y_pct"]) if data.get("y_pct") is not None else None,
        max_y_pct=(
            float(data["max_y_pct"]) if data.get("max_y_pct") is not None else None
        ),
        best_block=(
            _block_from_dict(best_block_data) if best_block_data is not None else None
        ),
        tracking_blocks=[
            _block_from_dict(block) for block in data.get("tracking_blocks", [])
        ],
        gender=str(data.get("gender", "female") or "female"),
        speaker_id=data.get("speaker_id"),
        is_subtitle=bool(data["is_subtitle"]) if data.get("is_subtitle") is not None else None,
        is_packaging=bool(data["is_packaging"]) if data.get("is_packaging") is not None else None,
        is_static=bool(data["is_static"]) if data.get("is_static") is not None else None,
        in_subtitle_band=bool(data["in_subtitle_band"]) if data.get("in_subtitle_band") is not None else None,
    )


def segments_to_dicts(segments: Iterable[Any]) -> List[Dict[str, Any]]:
    return [segment_to_dict(segment) for segment in segments]


def segments_from_dicts(items: Iterable[Mapping[str, Any]]) -> List[RuntimeSegment]:
    return [segment_from_dict(item) for item in items]
