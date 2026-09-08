"""Conservative decisions for skipping expensive optional stages."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union


PathLike = Union[str, os.PathLike]


@dataclass(frozen=True)
class AdaptiveDecision:
    should_run: bool
    reason: str
    confidence: float
    metrics: Mapping[str, Any] = field(default_factory=dict)


def _run(command: Sequence[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    creation_flags = 0
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creation_flags,
    )


def decide_demucs(
    audio_path: PathLike,
    clean_audio_hint: Optional[bool] = None,
    ffmpeg_binary: str = "ffmpeg",
) -> AdaptiveDecision:
    """Skip Demucs only on an explicit hint or a high-confidence clean signal.

    An uncertain probe always runs Demucs to avoid silently degrading ASR and
    background audio quality.
    """

    if clean_audio_hint is True:
        return AdaptiveDecision(False, "explicit_clean_audio_hint", 1.0)
    if clean_audio_hint is False:
        return AdaptiveDecision(True, "explicit_mixed_audio_hint", 1.0)
    path = Path(audio_path)
    if not path.is_file():
        return AdaptiveDecision(True, "audio_missing_or_unreadable", 0.0)
    command = [
        ffmpeg_binary,
        "-hide_banner",
        "-nostats",
        "-t",
        "45",
        "-i",
        str(path),
        "-af",
        "astats=metadata=0:reset=0",
        "-f",
        "null",
        "-",
    ]
    try:
        result = _run(command)
    except (OSError, subprocess.SubprocessError):
        return AdaptiveDecision(True, "audio_probe_failed", 0.0)
    if result.returncode != 0:
        return AdaptiveDecision(True, "audio_probe_failed", 0.0)
    overall = result.stderr.rsplit("Overall", 1)[-1]

    def metric(label: str) -> Optional[float]:
        match = re.search(
            r"{}:\s*(-?[0-9.]+)".format(re.escape(label)), overall
        )
        return float(match.group(1)) if match else None

    metrics = {
        "crest_factor": metric("Crest factor"),
        "noise_floor_db": metric("Noise floor dB"),
        "dynamic_range_db": metric("Dynamic range"),
        "entropy": metric("Entropy"),
    }
    values_available = all(value is not None for value in metrics.values())
    looks_clean = bool(
        values_available
        and metrics["crest_factor"] >= 4.5
        and metrics["noise_floor_db"] <= -45.0
        and metrics["dynamic_range_db"] >= 18.0
        and metrics["entropy"] <= 0.95
    )
    if looks_clean:
        return AdaptiveDecision(
            False, "high_confidence_clean_speech_heuristic", 0.82, metrics
        )
    return AdaptiveDecision(True, "mixed_or_uncertain_audio", 0.7, metrics)


def decide_ocr_from_scores(
    text_like_frame_scores: Sequence[float], threshold: float = 0.25
) -> AdaptiveDecision:
    if not text_like_frame_scores:
        return AdaptiveDecision(True, "no_frame_samples", 0.0)
    positive_ratio = sum(score >= 0.5 for score in text_like_frame_scores) / len(
        text_like_frame_scores
    )
    peak = max(text_like_frame_scores)
    likelihood = min(1.0, positive_ratio * 0.75 + peak * 0.25)
    should_run = likelihood >= threshold
    return AdaptiveDecision(
        should_run,
        "burned_subtitle_likely" if should_run else "no_persistent_text_band",
        abs(likelihood - threshold) / max(threshold, 1.0 - threshold),
        {
            "sample_count": len(text_like_frame_scores),
            "positive_ratio": positive_ratio,
            "peak_score": peak,
            "likelihood": likelihood,
        },
    )


def decide_ocr(video_path: PathLike, sample_count: int = 8) -> AdaptiveDecision:
    """Use lightweight CPU vision to find persistent subtitle-like text bands."""

    try:
        import cv2
    except ImportError:
        return AdaptiveDecision(True, "opencv_unavailable", 0.0)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return AdaptiveDecision(True, "video_probe_failed", 0.0)
    try:
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            return AdaptiveDecision(True, "frame_count_unavailable", 0.0)
        scores = []
        for index in range(1, sample_count + 1):
            frame_index = int(total_frames * index / (sample_count + 1))
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                continue
            height, width = frame.shape[:2]
            region = frame[
                int(height * 0.40) : int(height * 0.94),
                int(width * 0.05) : int(width * 0.95),
            ]
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 80, 180)
            component_count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
                edges, connectivity=8
            )
            text_like = 0
            for component in range(1, component_count):
                _x, _y, comp_width, comp_height, area = stats[component]
                if (
                    area >= 8
                    and 2 <= comp_width <= region.shape[1] * 0.25
                    and region.shape[0] * 0.015 <= comp_height <= region.shape[0] * 0.18
                    and 0.15 <= comp_width / max(comp_height, 1) <= 12.0
                ):
                    text_like += 1
            scores.append(min(1.0, text_like / 12.0))
        return decide_ocr_from_scores(scores)
    except Exception:
        return AdaptiveDecision(True, "lightweight_text_probe_failed", 0.0)
    finally:
        capture.release()


def probe_video_dimensions(
    video_path: PathLike, ffprobe_binary: str = "ffprobe"
) -> Tuple[int, int]:
    result = _run(
        [
            ffprobe_binary,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video_path),
        ],
        timeout=30.0,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    payload = json.loads(result.stdout)
    stream = payload["streams"][0]
    return int(stream["width"]), int(stream["height"])


def probe_video_duration(video_path: PathLike, ffprobe_binary: str = "ffprobe") -> float:
    result = _run(
        [
            ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        timeout=30.0,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe duration failed")
    duration = float(result.stdout.strip())
    if duration <= 0:
        raise RuntimeError("Video duration is invalid")
    return duration


def choose_output_dimensions(
    source_width: int,
    source_height: int,
    requested_width: Optional[int] = None,
    requested_height: Optional[int] = None,
    preserve_source_resolution: bool = True,
) -> Tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Source dimensions must be positive")
    if preserve_source_resolution or requested_width is None or requested_height is None:
        return source_width, source_height
    if requested_width <= 0 or requested_height <= 0:
        raise ValueError("Requested dimensions must be positive")
    scale = min(
        requested_width / source_width,
        requested_height / source_height,
        1.0,
    )
    width = max(2, int(source_width * scale) // 2 * 2)
    height = max(2, int(source_height * scale) // 2 * 2)
    return width, height


