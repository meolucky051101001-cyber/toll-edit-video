"""Domain objects separating source Chinese text, Vietnamese subtitles, and dubbing utterances.

This module establishes three independent, stable entities with clear provenance:
- SourceTextTrack: Grounded Chinese text from OCR/ASR, geometry, and lifecycle.
- VietnameseSubtitle: Visual translation, wrapping, and subtitle card timing.
- DubUtterance: Audio dubbing utterance, speaker, and speech timing.
- JobTranslationOverride: Verified manual corrections scoped strictly by video_hash.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("pipeline_v2.domain")

TEXT_CLASSIFICATIONS = (
    "spoken_dialogue",
    "unvoiced_scene_text",
    "packaging_or_watermark",
    "uncertain",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class SourceTextTrack:
    """Represents detected source Chinese text with spatio-temporal coordinates."""

    track_id: str
    raw_text: str
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    start_time: float = 0.0
    end_time: float = 0.0
    start_pts: Optional[int] = None
    end_pts: Optional[int] = None
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    bounding_boxes: List[Dict[str, Any]] = field(default_factory=list)
    best_bbox: Optional[Tuple[float, float, float, float]] = None  # (x1, y1, x2, y2) in 0.0-1.0
    confidence: float = 1.0
    classification: str = "spoken_dialogue"
    origin: str = "ocr"  # "ocr", "asr", "reconciled", "manual_override"
    source_segment_id: Optional[int] = None
    needs_review: bool = False
    review_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.classification not in TEXT_CLASSIFICATIONS:
            self.classification = "uncertain"
            self.needs_review = True
            self.review_reason = f"Unknown classification: {self.classification}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "track_id": self.track_id,
            "raw_text": self.raw_text,
            "candidates": list(self.candidates),
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "start_pts": self.start_pts,
            "end_pts": self.end_pts,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "bounding_boxes": list(self.bounding_boxes),
            "best_bbox": list(self.best_bbox) if self.best_bbox is not None else None,
            "confidence": round(float(self.confidence), 4),
            "classification": self.classification,
            "origin": self.origin,
            "source_segment_id": self.source_segment_id,
            "needs_review": bool(self.needs_review),
            "review_reason": self.review_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceTextTrack":
        best_b = data.get("best_bbox")
        bbox_tuple = tuple(float(x) for x in best_b) if best_b and len(best_b) == 4 else None
        return cls(
            track_id=str(data["track_id"]),
            raw_text=str(data.get("raw_text", "")),
            candidates=list(data.get("candidates", [])),
            start_time=float(data.get("start_time", 0.0)),
            end_time=float(data.get("end_time", 0.0)),
            start_pts=int(data["start_pts"]) if data.get("start_pts") is not None else None,
            end_pts=int(data["end_pts"]) if data.get("end_pts") is not None else None,
            start_frame=int(data["start_frame"]) if data.get("start_frame") is not None else None,
            end_frame=int(data["end_frame"]) if data.get("end_frame") is not None else None,
            bounding_boxes=list(data.get("bounding_boxes", [])),
            best_bbox=bbox_tuple,
            confidence=float(data.get("confidence", 1.0)),
            classification=str(data.get("classification", "spoken_dialogue")),
            origin=str(data.get("origin", "ocr")),
            source_segment_id=(
                int(data["source_segment_id"]) if data.get("source_segment_id") is not None else None
            ),
            needs_review=bool(data.get("needs_review", False)),
            review_reason=data.get("review_reason"),
        )


@dataclass
class VietnameseSubtitle:
    """Represents a displayed Vietnamese subtitle card."""

    sub_id: str
    source_track_id: Optional[str]
    translated_text: str
    display_text: str
    start_time: float
    end_time: float
    start_frame: Optional[int] = None
    end_frame: Optional[int] = None
    style: str = "TextStyle"
    box_style: str = "BgStyle"
    is_unvoiced: bool = False
    line_count: int = 1
    alignment: int = 8

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sub_id": self.sub_id,
            "source_track_id": self.source_track_id,
            "translated_text": self.translated_text,
            "display_text": self.display_text,
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "style": self.style,
            "box_style": self.box_style,
            "is_unvoiced": bool(self.is_unvoiced),
            "line_count": int(self.line_count),
            "alignment": int(self.alignment),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "VietnameseSubtitle":
        return cls(
            sub_id=str(data["sub_id"]),
            source_track_id=str(data["source_track_id"]) if data.get("source_track_id") else None,
            translated_text=str(data.get("translated_text", "")),
            display_text=str(data.get("display_text", data.get("translated_text", ""))),
            start_time=float(data.get("start_time", 0.0)),
            end_time=float(data.get("end_time", 0.0)),
            start_frame=int(data["start_frame"]) if data.get("start_frame") is not None else None,
            end_frame=int(data["end_frame"]) if data.get("end_frame") is not None else None,
            style=str(data.get("style", "TextStyle")),
            box_style=str(data.get("box_style", "BgStyle")),
            is_unvoiced=bool(data.get("is_unvoiced", False)),
            line_count=int(data.get("line_count", 1)),
            alignment=int(data.get("alignment", 8)),
        )


@dataclass
class DubUtterance:
    """Represents a spoken audio utterance in the Vietnamese dub track."""

    utterance_id: str
    source_track_id: Optional[str]
    speaker_id: str
    gender: str
    start_time: float
    end_time: float
    audio_path: Optional[str] = None
    artifact_key: Optional[str] = None
    is_silent_placeholder: bool = False
    timing_fits: bool = True
    actual_duration: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "utterance_id": self.utterance_id,
            "source_track_id": self.source_track_id,
            "speaker_id": self.speaker_id,
            "gender": self.gender,
            "start_time": round(float(self.start_time), 3),
            "end_time": round(float(self.end_time), 3),
            "audio_path": self.audio_path,
            "artifact_key": self.artifact_key,
            "is_silent_placeholder": bool(self.is_silent_placeholder),
            "timing_fits": bool(self.timing_fits),
            "actual_duration": round(float(self.actual_duration), 3),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DubUtterance":
        return cls(
            utterance_id=str(data["utterance_id"]),
            source_track_id=str(data["source_track_id"]) if data.get("source_track_id") else None,
            speaker_id=str(data.get("speaker_id", "reconciled")),
            gender=str(data.get("gender", "female")),
            start_time=float(data.get("start_time", 0.0)),
            end_time=float(data.get("end_time", 0.0)),
            audio_path=data.get("audio_path"),
            artifact_key=data.get("artifact_key"),
            is_silent_placeholder=bool(data.get("is_silent_placeholder", False)),
            timing_fits=bool(data.get("timing_fits", True)),
            actual_duration=float(data.get("actual_duration", 0.0)),
        )


@dataclass(frozen=True)
class JobTranslationOverride:
    """A human-verified translation and classification override strictly bound to a video hash."""

    video_hash: str
    source_text: str
    override_translation: str
    track_id: Optional[str] = None
    override_classification: Optional[str] = None
    reviewer: str = "qa_team"
    reason: str = "manual_verification"
    created_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "video_hash": self.video_hash,
            "source_text": self.source_text,
            "override_translation": self.override_translation,
            "track_id": self.track_id,
            "override_classification": self.override_classification,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobTranslationOverride":
        return cls(
            video_hash=str(data["video_hash"]),
            source_text=str(data["source_text"]),
            override_translation=str(data["override_translation"]),
            track_id=str(data["track_id"]) if data.get("track_id") else None,
            override_classification=data.get("override_classification"),
            reviewer=str(data.get("reviewer", "qa_team")),
            reason=str(data.get("reason", "manual_verification")),
            created_at=str(data.get("created_at", _utc_now())),
        )


class JobTranslationOverrideStore:
    """Manages verified per-job translation overrides.
    
    CRITICAL: Never applies overrides from video A to video B. Overrides only
    activate when the target video's SHA-256 matches exactly.
    """

    def __init__(self, overrides_by_hash: Optional[Dict[str, List[JobTranslationOverride]]] = None):
        self._by_hash: Dict[str, List[JobTranslationOverride]] = overrides_by_hash or {}

    @classmethod
    def from_file(cls, path: Path) -> "JobTranslationOverrideStore":
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items: List[JobTranslationOverride] = []
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "video_hash" in item and "source_text" in item:
                        items.append(JobTranslationOverride.from_dict(item))
            elif isinstance(data, dict):
                # Format: {"video_hash": "...", "overrides": [...]}
                v_hash = str(data.get("video_hash", ""))
                for item in data.get("overrides", []):
                    item_dict = dict(item)
                    if "video_hash" not in item_dict:
                        item_dict["video_hash"] = v_hash
                    items.append(JobTranslationOverride.from_dict(item_dict))
            by_hash: Dict[str, List[JobTranslationOverride]] = {}
            for override in items:
                by_hash.setdefault(override.video_hash, []).append(override)
            return cls(by_hash)
        except Exception as exc:
            logger.warning("Failed to load translation overrides from %s: %s", path, exc)
            return cls()

    def get_override(
        self,
        current_video_hash: str,
        source_text: str,
        track_id: Optional[str] = None,
    ) -> Optional[JobTranslationOverride]:
        if not current_video_hash:
            return None
        candidates = self._by_hash.get(current_video_hash, [])
        if not candidates:
            return None

        norm_src = re.sub(r'[\s，。！？!?.,、\-_:;]+', '', source_text.strip())

        # Exact match by track_id first
        if track_id:
            for item in candidates:
                if item.track_id == track_id:
                    return item

        # Match by normalized source text
        for item in candidates:
            norm_item = re.sub(r'[\s，。！？!?.,、\-_:;]+', '', item.source_text.strip())
            if norm_item == norm_src:
                return item

        return None
