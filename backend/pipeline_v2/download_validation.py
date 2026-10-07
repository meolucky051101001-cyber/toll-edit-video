"""Validation primitives used before a downloaded video becomes authoritative."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Union


PathLike = Union[str, os.PathLike]
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_CONTENT_RANGE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)
_NON_VIDEO_FORMATS = {"image2", "image2pipe", "rawvideo"}


class DownloadValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedVideoProbe:
    path: Path
    format_name: str
    duration_seconds: float
    video_stream_count: int
    audio_stream_count: int


def require_partial_content(
    status_code: int,
    headers: Mapping[str, Any],
    requested_start: int,
    requested_end: int,
    expected_total: Optional[int] = None,
) -> None:
    """Reject servers that ignore Range and would silently corrupt merged chunks."""

    if int(status_code) != 206:
        raise DownloadValidationError(
            "HTTP Range request must return 206 Partial Content, got {}".format(
                status_code
            )
        )
    value = str(headers.get("Content-Range") or headers.get("content-range") or "")
    match = _CONTENT_RANGE.match(value.strip())
    if not match:
        raise DownloadValidationError("Missing or invalid Content-Range header")
    actual_start, actual_end = int(match.group(1)), int(match.group(2))
    actual_total = None if match.group(3) == "*" else int(match.group(3))
    if actual_start != int(requested_start) or actual_end != int(requested_end):
        raise DownloadValidationError(
            "Content-Range {} does not match requested bytes {}-{}".format(
                value, requested_start, requested_end
            )
        )
    if expected_total is not None and actual_total != int(expected_total):
        raise DownloadValidationError(
            "Content-Range total {} does not match expected {}".format(
                actual_total, expected_total
            )
        )


def require_complete_response(status_code: int, headers: Mapping[str, Any]) -> None:
    """Accept a normal 200 or a 206 only when it contains the complete object."""

    if int(status_code) == 200:
        return
    if int(status_code) != 206:
        raise DownloadValidationError(
            "Video GET must return HTTP 200 or complete 206, got {}".format(status_code)
        )
    value = str(headers.get("Content-Range") or headers.get("content-range") or "")
    match = _CONTENT_RANGE.match(value.strip())
    if not match or match.group(3) == "*":
        raise DownloadValidationError("Partial video response has invalid Content-Range")
    start, end, total = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if start != 0 or end != total - 1:
        raise DownloadValidationError(
            "Refusing incomplete video response: {}".format(value)
        )


def _duration_from_probe(payload: Mapping[str, Any]) -> float:
    candidates = [dict(payload.get("format", {})).get("duration")]
    candidates.extend(
        stream.get("duration") for stream in payload.get("streams", [])
    )
    durations = []
    for candidate in candidates:
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if value > 0:
            durations.append(value)
    return max(durations, default=0.0)


def probe_downloaded_video(
    path: PathLike,
    ffprobe_binary: str = "ffprobe",
    expected_duration_seconds: Optional[float] = None,
) -> DownloadedVideoProbe:
    """Require a recognized container, a video stream and a positive duration."""

    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise DownloadValidationError("Downloaded file is missing or empty")
    command = [
        ffprobe_binary,
        "-v",
        "error",
        "-show_entries",
        "format=format_name,duration:stream=codec_type,duration",
        "-of",
        "json",
        str(candidate),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise DownloadValidationError(
            result.stderr.strip() or "ffprobe rejected the downloaded container"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadValidationError("ffprobe returned invalid JSON") from exc

    format_name = str(dict(payload.get("format", {})).get("format_name", "")).strip()
    if not format_name or any(
        name.strip().lower() in _NON_VIDEO_FORMATS for name in format_name.split(",")
    ):
        raise DownloadValidationError("Downloaded file has no supported video container")
    streams = list(payload.get("streams", []))
    video_streams = sum(stream.get("codec_type") == "video" for stream in streams)
    audio_streams = sum(stream.get("codec_type") == "audio" for stream in streams)
    if video_streams < 1:
        raise DownloadValidationError("Downloaded container has no video stream")
    duration = _duration_from_probe(payload)
    if duration <= 0.1:
        raise DownloadValidationError("Downloaded video duration is invalid")

    if expected_duration_seconds is not None and expected_duration_seconds > 0:
        tolerance = max(2.0, float(expected_duration_seconds) * 0.05)
        if abs(duration - float(expected_duration_seconds)) > tolerance:
            raise DownloadValidationError(
                "Downloaded duration {:.3f}s differs from expected {:.3f}s".format(
                    duration, expected_duration_seconds
                )
            )
    return DownloadedVideoProbe(
        path=candidate,
        format_name=format_name,
        duration_seconds=duration,
        video_stream_count=video_streams,
        audio_stream_count=audio_streams,
    )


