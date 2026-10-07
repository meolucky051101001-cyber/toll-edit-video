"""
v1_asr_chunking.py - ASR Chunking Engine an toàn cho video dài (Tool V1 - Giai đoạn 4).
Hỗ trợ chia đoạn âm thanh 240 giây với overlap 0.75 giây cho video dài (tối đa 60 phút).
Khử trùng lặp từ ở ranh giới chunk, offset timestamp mượt mà, bảo toàn 100% câu thoại.
"""

from __future__ import annotations

import os
import sys
import math
import logging
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE_S = 240.0  # 4 phút
DEFAULT_OVERLAP_S = 0.75      # 0.75 giây overlap an toàn


def get_audio_duration_seconds(audio_path: str | Path) -> float:
    """Lấy thời lượng chính xác của file âm thanh bằng soundfile hoặc pydub."""
    path = str(audio_path)
    try:
        import soundfile as sf
        info = sf.info(path)
        return float(info.duration)
    except Exception:
        pass

    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(path)
        return len(seg) / 1000.0
    except Exception as exc:
        logger.warning("Không thể đọc thời lượng audio %s: %s", path, exc)
        return 0.0


def chunk_audio_intervals(
    total_duration: float,
    chunk_size: float = DEFAULT_CHUNK_SIZE_S,
    overlap: float = DEFAULT_OVERLAP_S,
) -> List[Tuple[float, float]]:
    """
    Chia tổng thời lượng thành danh sách các khoảng [start_s, end_s] với overlap.
    Nếu thời lượng <= chunk_size, trả về đúng 1 khoảng [0.0, total_duration].
    """
    if not all(math.isfinite(v) for v in (total_duration, chunk_size, overlap)) or chunk_size <= 0 or not 0 <= overlap < chunk_size:
        raise ValueError("Invalid ASR chunk interval")
    if total_duration <= 0.0:
        return []
    if total_duration <= chunk_size:
        return [(0.0, round(total_duration, 3))]

    step = max(1.0, chunk_size - overlap)
    intervals: List[Tuple[float, float]] = []
    cursor = 0.0

    while cursor < total_duration:
        end = min(total_duration, cursor + chunk_size)
        intervals.append((round(cursor, 3), round(end, 3)))
        if end >= total_duration:
            break
        cursor += step

    return intervals


def deduplicate_boundary_segments(
    prev_segments: List[Dict[str, Any]],
    next_segments: List[Dict[str, Any]],
    overlap_point: float,
    tolerance_s: float = 1.0,
) -> List[Dict[str, Any]]:
    """
    Khử trùng lặp câu/từ ở ranh giới giữa 2 chunk liên tiếp.
    Nếu câu đầu của chunk sau nằm trong vùng overlap và có nội dung trùng lặp
    hoặc mốc thời gian chồng lấn với câu cuối của chunk trước, loại bỏ câu trùng lặp.
    """
    if not prev_segments:
        return list(next_segments)
    if not next_segments:
        return []

    filtered = []
    last = prev_segments[-1]
    for original in next_segments:
        seg = dict(original)
        start, end = float(seg["start"]), float(seg["end"])
        previous_text = str(last.get("text", "")).strip()
        text = str(seg.get("text", "")).strip()
        overlaps = (float(last["end"]) > overlap_point and start < float(last["end"])
                    and end > float(last["start"]))
        if overlaps and text and text.casefold() == previous_text.casefold():
            last["end"] = max(float(last["end"]), end)
            continue
        if overlaps and len(previous_text) > 3 and text.casefold().startswith(previous_text.casefold()):
            # Keep the additional words from the next chunk, not only the prefix.
            last["text"], last["end"] = text, max(float(last["end"]), end)
            continue
        if overlaps and len(text) > 3 and text.casefold() in previous_text.casefold() and end <= float(last["end"]):
            continue
        # Different words must not be discarded merely because timestamps overlap.
        filtered.append(seg)
    return filtered


def slice_audio_chunk(
    source_audio_path: str | Path,
    start_s: float,
    duration_s: float,
    target_chunk_path: str | Path,
) -> str:
    """Cắt một đoạn audio WAV bằng soundfile hoặc ffmpeg mà không làm suy hao chất lượng."""
    src = str(source_audio_path)
    dst = str(target_chunk_path)

    try:
        import soundfile as sf
        with sf.SoundFile(src) as f:
            sr = f.samplerate
            channels = f.channels
            start_frame = int(start_s * sr)
            num_frames = int(duration_s * sr)
            f.seek(start_frame)
            data = f.read(num_frames)
            sf.write(dst, data, sr)
        if os.path.exists(dst) and os.path.getsize(dst) > 100:
            return dst
    except Exception as exc:
        logger.debug("Slice bằng soundfile không thành công, thử ffmpeg: %s", exc)

    # Fallback qua ffmpeg subprocess
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_s:.3f}",
        "-t", f"{duration_s:.3f}",
        "-i", src,
        "-acodec", "copy",
        dst
    ]
    subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=True,
    )
    return dst
