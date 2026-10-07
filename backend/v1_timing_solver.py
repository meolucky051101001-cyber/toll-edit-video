"""
v1_timing_solver.py - Timing Solver chống đè giọng và co giãn nhịp điệu (Tool V1 - Giai đoạn 4).
Hỗ trợ:
- Tính toán chính xác timing window giữa các câu thoại (chống audio collision).
- Co giãn tốc độ tự nhiên trong ngưỡng an toàn [0.85x - 1.35x] (tránh méo giọng chipmunk).
- Nhận diện câu vượt ngưỡng để gom batch nén ngữ nghĩa qua Gemini.
"""

from __future__ import annotations

import os
import sys
import math
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Ngưỡng tốc độ tự nhiên tối đa an toàn (1.35x). Trên ngưỡng này sẽ kích hoạt nén câu Gemini
DEFAULT_MIN_SPEED = 0.85
DEFAULT_MAX_NATURAL_SPEED = float(os.getenv("V1_MAX_NATURAL_SPEED", "1.35"))
HARD_GAP_BETWEEN_SPEECH_S = 0.08  # Khoảng đệm an toàn giữa 2 câu thoại


def calculate_timing_windows(
    segments: Sequence[Any],
    total_audio_duration: Optional[float] = None,
    hard_gap_s: float = HARD_GAP_BETWEEN_SPEECH_S,
) -> Dict[int, float]:
    """
    Tính toán khung thời lượng khả dụng tối đa (hard_max_s) cho từng câu thoại:
    - hard_max_s = next_start - cur_start - hard_gap_s.
    - Với câu cuối: hard_max_s = total_audio_duration - cur_start (hoặc cur_dur + 2.5s nếu chưa biết tổng).
    """
    windows: Dict[int, float] = {}
    if not segments:
        return windows

    sorted_segs = sorted(segments, key=lambda s: getattr(s, "start", 0.0).total_seconds() if hasattr(getattr(s, "start", None), "total_seconds") else float(getattr(s, "start", 0.0)))
    total_count = len(sorted_segs)

    for i, seg in enumerate(sorted_segs):
        idx = getattr(seg, "index", i + 1)
        cur_start = getattr(seg, "start", 0.0).total_seconds() if hasattr(getattr(seg, "start", None), "total_seconds") else float(getattr(seg, "start", 0.0))
        cur_end = getattr(seg, "end", 0.0).total_seconds() if hasattr(getattr(seg, "end", None), "total_seconds") else float(getattr(seg, "end", 0.0))
        cur_dur = max(0.5, cur_end - cur_start)

        if i + 1 < total_count:
            next_seg = sorted_segs[i + 1]
            next_start = getattr(next_seg, "start", 0.0).total_seconds() if hasattr(getattr(next_seg, "start", None), "total_seconds") else float(getattr(next_seg, "start", 0.0))
            available = next_start - cur_start - hard_gap_s
            hard_max_s = max(0.6, available)
        else:
            if total_audio_duration and total_audio_duration > cur_start:
                hard_max_s = max(0.6, total_audio_duration - cur_start - hard_gap_s)
            else:
                hard_max_s = max(0.6, cur_dur + 2.5)

        windows[idx] = round(hard_max_s, 3)

    return windows


def calculate_atempo_factor(
    actual_duration: float,
    window_duration: float,
    min_speed: float = DEFAULT_MIN_SPEED,
    max_speed: float = DEFAULT_MAX_NATURAL_SPEED,
) -> Tuple[float, str]:
    """
    Tính hệ số atempo an toàn:
    - Nếu audio vừa vặn khung (hoặc ratio <= 1.03): ratio = 1.0 (không transcode, giữ nguyên bản).
    - Nếu audio ngắn hơn: KHÔNG kéo dài (ratio = 1.0) để giữ nhịp tự nhiên.
    - Nếu ratio nằm trong [1.03, max_speed]: trả về (ratio, 'atempo_fit').
    - Nếu ratio > max_speed: trả về (max_speed, 'needs_condensation').
    """
    if window_duration <= 0 or actual_duration <= 0:
        return 1.0, "normal"

    if actual_duration <= window_duration:
        return 1.0, "normal"

    ratio = actual_duration / window_duration
    if ratio <= 1.03:
        return 1.0, "normal"

    if ratio <= max_speed:
        return round(ratio, 3), "atempo_fit"

    return round(max_speed, 3), "needs_condensation"


def detect_overly_long_segments(
    segments: Sequence[Any],
    windows: Dict[int, float],
    max_natural_speed: float = DEFAULT_MAX_NATURAL_SPEED,
    reading_speed_words_per_sec: float = 3.6,
) -> List[Dict[str, Any]]:
    """
    Nhận diện các câu thoại có nguy cơ tràn khung đọc (> max_natural_speed * window)
    dựa trên số từ dự kiến hoặc thời lượng đo đạc.
    """
    overly_long: List[Dict[str, Any]] = []

    for seg in segments:
        idx = getattr(seg, "index", 0)
        content = getattr(seg, "content", "") or ""
        text = content.strip()
        if not text:
            continue

        window_s = windows.get(idx, 2.0)
        word_count = len(text.split())
        est_duration = word_count / reading_speed_words_per_sec

        if window_s > 0 and (est_duration / window_s) > max_natural_speed:
            target_words = max(3, int((window_s - 0.05) * reading_speed_words_per_sec * 0.95))
            overly_long.append({
                "index": idx,
                "text": text,
                "target_seconds": window_s,
                "target_words": target_words,
                "current_seconds": round(est_duration, 2),
                "speed_ratio": round(est_duration / window_s, 2),
            })

    return overly_long


def build_safe_atempo_filter(speed_ratio: float) -> str:
    """
    Xây dựng chuỗi filter atempo hợp lệ cho FFmpeg trong dải [0.5x - 4.0x].
    Mỗi node atempo của ffmpeg chỉ nhận giá trị từ 0.5 đến 2.0.
    """
    ratio = max(0.25, min(4.0, float(speed_ratio)))
    if abs(ratio - 1.0) < 0.01:
        return "anull"

    filters = []
    current = ratio
    while current > 2.0:
        filters.append("atempo=2.0")
        current /= 2.0
    while current < 0.5:
        filters.append("atempo=0.5")
        current /= 0.5
    filters.append(f"atempo={current:.3f}")
    return ",".join(filters)


def plan_condensation_batches(
    overly_long_items: List[Dict[str, Any]],
    batch_size: int = 20,
) -> List[List[Dict[str, Any]]]:
    """Chia danh sách câu cần nén thành các lô nhỏ để gọi Gemini API an toàn và ổn định."""
    if not overly_long_items:
        return []
    return [overly_long_items[i : i + batch_size] for i in range(0, len(overly_long_items), batch_size)]
