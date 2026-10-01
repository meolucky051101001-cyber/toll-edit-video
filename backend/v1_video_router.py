"""
v1_video_router.py - Bộ điều phối phân loại video theo độ dài và độ phức tạp (Giai đoạn 1).
Phân loại thông minh: SHORT (<= 180s), MEDIUM (180s - 600s), LONG (> 600s, max 3600s).
Xem xét đa yếu tố: Thời lượng, độ phân giải (4K/1080p), FPS, bitrate, kích thước file và VRAM.
Tạo snapshot bất biến (immutable) lưu vào frozen config của job.
"""

from __future__ import annotations

import os
import json
import logging
import subprocess
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ===== ENUMS & CẤU HÌNH THỜI LƯỢNG =====

class VideoMode(str, Enum):
    AUTO = "AUTO"
    SHORT = "SHORT"
    MEDIUM = "MEDIUM"
    LONG = "LONG"
    CUSTOM = "CUSTOM"


def get_router_thresholds() -> Dict[str, float]:
    """Lấy các ngưỡng phân loại có thể cấu hình được từ biến môi trường. Mặc định: <= 7 phút là SHORT, > 7 phút là LONG."""
    short_max = float(os.getenv("V1_SHORT_MAX_SECONDS", "420.0"))
    medium_max = float(os.getenv("V1_MEDIUM_MAX_SECONDS", "420.0"))
    max_duration_min = float(os.getenv("V1_MAX_VIDEO_DURATION_MINUTES", "60.0"))
    max_duration_sec = max_duration_min * 60.0
    return {
        "short_max_seconds": short_max,
        "medium_max_seconds": medium_max,
        "max_video_seconds": max_duration_sec,
        "max_video_minutes": max_duration_min,
    }


def is_router_enabled() -> bool:
    """Kiểm tra Feature Flag bật/tắt Video Router."""
    val = os.getenv("ENABLE_VIDEO_ROUTER", "true").strip().lower()
    return val in ("1", "true", "yes", "on")


# ===== TRÍCH XUẤT METADATA TỆP VIDEO / AUDIO =====

@dataclass
class VideoMetadata:
    duration_s: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    bitrate_kbps: float = 0.0
    file_size_bytes: int = 0
    has_audio: bool = False
    audio_channels: int = 0
    audio_samplerate: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def extract_media_metadata(file_path: str | Path) -> VideoMetadata:
    """
    Trích xuất toàn diện metadata của video/audio bằng ffprobe mà không tải nặng bộ nhớ.
    Hoạt động an toàn, có fallback nếu ffprobe không khả dụng hoặc file bị lỗi.
    """
    path = Path(file_path)
    meta = VideoMetadata()
    if not path.is_file():
        return meta

    try:
        meta.file_size_bytes = path.stat().st_size
    except OSError:
        pass

    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,channels,sample_rate",
        "-of", "json",
        str(path)
    ]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            format_info = data.get("format", {})
            meta.duration_s = float(format_info.get("duration", 0.0) or 0.0)
            if not meta.file_size_bytes:
                meta.file_size_bytes = int(format_info.get("size", 0) or 0)
            bit_rate = float(format_info.get("bit_rate", 0.0) or 0.0)
            meta.bitrate_kbps = round(bit_rate / 1000.0, 1)

            streams = data.get("streams", [])
            for st in streams:
                ctype = st.get("codec_type")
                if ctype == "video" and not meta.width:
                    meta.width = int(st.get("width", 0) or 0)
                    meta.height = int(st.get("height", 0) or 0)
                    meta.video_codec = str(st.get("codec_name", "") or "")
                    fps_str = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/0"
                    if "/" in fps_str:
                        num, den = fps_str.split("/", 1)
                        try:
                            den_f = float(den)
                            if den_f > 0:
                                meta.fps = round(float(num) / den_f, 2)
                        except (ValueError, ZeroDivisionError):
                            pass
                elif ctype == "audio" and not meta.has_audio:
                    meta.has_audio = True
                    meta.audio_codec = str(st.get("codec_name", "") or "")
                    meta.audio_channels = int(st.get("channels", 0) or 0)
                    meta.audio_samplerate = int(st.get("sample_rate", 0) or 0)
    except Exception as exc:
        logger.warning("Không thể đọc ffprobe cho '%s': %s", path.name, exc)

    return meta


# ===== PHÂN TÍCH ĐỘ PHỨC TẠP & ĐIỀU PHỐI (ROUTING) =====

@dataclass
class RoutingDecision:
    resolved_mode: VideoMode
    requested_mode: str
    is_escalated: bool
    escalation_reasons: List[str]
    metadata: VideoMetadata
    thresholds: Dict[str, float]
    planned_pipeline: Dict[str, Any]
    warnings: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resolved_mode": self.resolved_mode.value,
            "requested_mode": self.requested_mode,
            "is_escalated": self.is_escalated,
            "escalation_reasons": self.escalation_reasons,
            "metadata": self.metadata.to_dict(),
            "thresholds": self.thresholds,
            "planned_pipeline": self.planned_pipeline,
            "warnings": self.warnings,
        }


def route_video(
    file_path: str | Path,
    requested_mode: str = "auto",
    user_overrides: Optional[Dict[str, Any]] = None,
    workspace_path: Optional[str | Path] = None,
) -> RoutingDecision:
    """
    Phân tích video và đưa ra quyết định định tuyến chính xác.
    Đảm bảo:
    1. Tôn trọng chế độ chỉ định thủ công của người dùng (nếu có).
    2. Nếu ở chế độ AUTO: đánh giá thời lượng + độ phức tạp kỹ thuật.
    3. Cảnh báo hoặc từ chối video vượt quá ngưỡng an toàn tối đa (mặc định 60 phút).
    """
    thresholds = get_router_thresholds()
    meta = extract_media_metadata(file_path)
    req_norm = (requested_mode or "auto").strip().upper()

    escalation_reasons: List[str] = []
    warnings: List[str] = []
    is_escalated = False

    # 1. Kiểm tra giới hạn trần thời lượng an toàn
    max_limit = thresholds["max_video_seconds"]
    if meta.duration_s > max_limit:
        msg = (
            f"Thời lượng video ({meta.duration_s:.1f}s ~ {meta.duration_s / 60:.1f} phút) "
            f"vượt quá ngưỡng trần an toàn {thresholds['max_video_minutes']:.0f} phút."
        )
        warnings.append(msg)
        logger.warning(msg)

    # 2. Xử lý khi người dùng chỉ định Mode thủ công
    if req_norm in ("SHORT", "MEDIUM", "LONG", "CUSTOM"):
        resolved_mode = VideoMode(req_norm)
    else:
        # 3. Phân loại tự động (AUTO mode)
        dur = meta.duration_s
        short_max = thresholds["short_max_seconds"]
        medium_max = thresholds["medium_max_seconds"]

        if dur <= short_max:
            base_mode = VideoMode.SHORT
        elif dur <= medium_max:
            base_mode = VideoMode.MEDIUM
        else:
            base_mode = VideoMode.LONG

        # 4. Đánh giá đa yếu tố nâng hồ sơ (Escalation Rules)
        # Nếu video thuộc diện SHORT nhưng tải nặng (4K, FPS cao, Bitrate khủng, Dung lượng file lớn):
        if base_mode == VideoMode.SHORT and dur > 0:
            # 4K / UHD
            if meta.width >= 3840 or meta.height >= 2160:
                is_escalated = True
                escalation_reasons.append(f"Độ phân giải 4K UHD ({meta.width}x{meta.height})")
            # 60 FPS trở lên
            if meta.fps >= 50.0:
                is_escalated = True
                escalation_reasons.append(f"Tốc độ khung hình cao ({meta.fps} fps)")
            # Bitrate rất cao (> 16 Mbps)
            if meta.bitrate_kbps >= 16000.0:
                is_escalated = True
                escalation_reasons.append(f"Bitrate cao ({meta.bitrate_kbps:.0f} kbps)")
            # File lớn bất thường đối với video ngắn (> 1.2 GB)
            if meta.file_size_bytes > 1_200_000_000:
                is_escalated = True
                escalation_reasons.append(f"Dung lượng file lớn ({meta.file_size_bytes / 1048576:.0f} MB)")

            if is_escalated:
                resolved_mode = VideoMode.MEDIUM
                logger.info(
                    "Video '%s' (%.1fs) được nâng hồ sơ từ SHORT -> MEDIUM vì: %s",
                    Path(file_path).name, dur, ", ".join(escalation_reasons)
                )
            else:
                resolved_mode = base_mode
        else:
            resolved_mode = base_mode

    # 5. Xác định cấu hình Pipeline dự kiến tương ứng với Mode đã giải quyết
    planned_pipeline = resolve_planned_pipeline(
        resolved_mode, meta, user_overrides, workspace_path=workspace_path
    )

    return RoutingDecision(
        resolved_mode=resolved_mode,
        requested_mode=req_norm,
        is_escalated=is_escalated,
        escalation_reasons=escalation_reasons,
        metadata=meta,
        thresholds=thresholds,
        planned_pipeline=planned_pipeline,
        warnings=warnings,
    )


def resolve_planned_pipeline(
    mode: VideoMode,
    meta: VideoMetadata,
    overrides: Optional[Dict[str, Any]] = None,
    workspace_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Xác định các thông số thực thi tương ứng với từng Mode."""
    overrides = overrides or {}

    if mode == VideoMode.SHORT:
        pipeline = {
            "separator_engine": overrides.get("separation_mode", "roformer"),
            "separation_model": overrides.get("separation_mode", "roformer"),
            "asr_chunking": False,
            "asr_chunk_size_s": 0.0,
            "asr_overlap_s": 0.0,
            "ocr_strategy": "full",
            "mixer_mode": "direct",
            "ducking_mode": overrides.get("ducking_mode", "soft"),
            "render_encoder": "h264_nvenc",
            "estimated_timeout_s": 900,  # 15 phút
        }
    elif mode == VideoMode.MEDIUM:
        pipeline = {
            "separator_engine": overrides.get("separation_mode", "demucs"),
            "separation_model": overrides.get("separation_mode", "demucs"),
            "asr_chunking": True,
            "asr_chunk_size_s": 240.0,
            "asr_overlap_s": 0.75,
            "ocr_strategy": "smart_skip",
            "mixer_mode": "hierarchical",
            "ducking_mode": overrides.get("ducking_mode", "soft"),
            "render_encoder": "h264_nvenc",
            "estimated_timeout_s": 1800,  # 30 phút
        }
    elif mode == VideoMode.LONG:
        pipeline = {
            "separator_engine": overrides.get("separation_mode", "demucs"),
            "separation_model": overrides.get("separation_mode", "demucs"),
            "asr_chunking": True,
            "asr_chunk_size_s": 240.0,
            "asr_overlap_s": 0.75,
            "ocr_strategy": "smart_skip",
            "mixer_mode": "hierarchical",
            "ducking_mode": overrides.get("ducking_mode", "soft"),
            "render_encoder": "h264_nvenc",
            "estimated_timeout_s": max(3600, int(meta.duration_s * 4.0)),
        }
    else:  # CUSTOM
        pipeline = {
            "separator_engine": overrides.get("separation_mode", "auto"),
            "asr_chunking": bool(overrides.get("asr_chunking", False)),
            "asr_chunk_size_s": float(overrides.get("asr_chunk_size_s", 240.0)),
            "asr_overlap_s": float(overrides.get("asr_overlap_s", 0.75)),
            "ocr_strategy": overrides.get("ocr_strategy", "smart_skip"),
            "mixer_mode": overrides.get("mixer_mode", "direct"),
            "ducking_mode": overrides.get("ducking_mode", "soft"),
            "render_encoder": "h264_nvenc",
            "estimated_timeout_s": 3600,
        }
    asr_model = overrides.get("asr_model")
    if not asr_model:
        try:
            try:
                from v1_feature_flags import get_feature_flags
            except ImportError:
                from backend.v1_feature_flags import get_feature_flags
            flags = get_feature_flags(workspace_path)
            if flags.get("ENABLE_QWEN_ASR") or flags.get("V1_ASR_MODEL") == "qwen3_asr":
                asr_model = "qwen3_asr"
            else:
                asr_model = "whisper_turbo"
        except Exception:
            val = os.getenv("ENABLE_QWEN_ASR", "false").lower() in ("true", "1", "yes")
            asr_model = "qwen3_asr" if val else "whisper_turbo"
    pipeline["asr_model"] = asr_model

    # Quy tắc: Tắt hoàn toàn smart skip OCR cho video ngắn (dưới 7 phút / 420s).
    # Kể cả khi video ngắn bị nâng hồ sơ lên MEDIUM (do 4K, 60fps, bitrate cao), OCR vẫn phải chạy "full".
    thresholds = get_router_thresholds()
    short_thresh = thresholds.get("short_max_seconds", 420.0)
    if (meta.duration_s > 0 and meta.duration_s <= short_thresh) or mode == VideoMode.SHORT:
        pipeline["ocr_strategy"] = "full"

    return pipeline


# ===== TẠO SNAPSHOT BẤT BIẾN (IMMUTABLE SNAPSHOT) =====

def create_router_snapshot(
    file_path: str | Path,
    requested_mode: str = "auto",
    job_id: Optional[str] = None,
    user_overrides: Optional[Dict[str, Any]] = None,
    workspace_path: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """
    Tạo snapshot thông tin định tuyến bất biến để ghi nhận vào frozen config.
    Một khi đã tạo, snapshot này đại diện cho quyết định xử lý duy nhất của job đó.
    """
    path = Path(file_path)
    decision = route_video(
        path,
        requested_mode=requested_mode,
        user_overrides=user_overrides,
        workspace_path=workspace_path,
    )
    now = time.time()

    return {
        "router_version": "1.0.0",
        "created_at": now,
        "job_id": job_id or "",
        "video_name": path.name,
        "video_path": str(path.resolve()),
        "resolved_mode": decision.resolved_mode.value,
        "requested_mode": decision.requested_mode,
        "is_escalated": decision.is_escalated,
        "escalation_reasons": decision.escalation_reasons,
        "metadata": decision.metadata.to_dict(),
        "planned_pipeline": decision.planned_pipeline,
        "thresholds": decision.thresholds,
        "warnings": decision.warnings,
        "is_immutable": True,
    }
