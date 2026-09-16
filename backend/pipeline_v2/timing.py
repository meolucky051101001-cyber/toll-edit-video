"""Duration budgeting and light-touch audio fitting before dubbing."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .segments import RuntimeSegment, segment_from_dict, segment_to_dict


PathLike = Union[str, os.PathLike]


def _creation_flags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


@dataclass(frozen=True)
class TimingPolicy:
    atempo_min: float = 0.92
    atempo_max: float = 1.40
    estimated_chars_per_second: float = 11.5
    min_segment_seconds: float = 0.35
    max_rewrite_rounds: int = 2

    def __post_init__(self) -> None:
        if not 0.5 <= self.atempo_min <= 1.0:
            raise ValueError("atempo_min must be between 0.5 and 1.0")
        if not 1.0 <= self.atempo_max <= 2.0:
            raise ValueError("atempo_max must be between 1.0 and 2.0")
        if self.estimated_chars_per_second <= 0:
            raise ValueError("estimated_chars_per_second must be positive")


@dataclass(frozen=True)
class TimingPlan:
    segment_index: int
    source_segment_id: int
    target_seconds: float
    estimated_seconds: float
    character_budget: int
    required_atempo: float
    fits: bool
    action: str


@dataclass(frozen=True)
class RewriteRequest:
    segment_index: int
    source_segment_id: int
    text: str
    target_seconds: float
    max_characters: int


def plan_actual_timing_rewrites(
    segments: Iterable[Any],
    audio_infos: Iterable[Mapping[str, Any]],
    safety_margin: float = 0.95,
) -> List[RewriteRequest]:
    """Create rewrite requests from measured TTS/RVC durations, not estimates."""

    if not 0.5 <= safety_margin <= 1.0:
        raise ValueError("safety_margin must be between 0.5 and 1.0")
    by_index = {int(info["index"]): dict(info) for info in audio_infos}
    requests = []
    for segment in segments:
        info = by_index.get(int(segment.index))
        if not info:
            continue
        target = max((segment.end - segment.start).total_seconds(), 0.1)
        actual = float(info.get("actual_audio_duration", 0.0) or 0.0)
        fits = bool(info.get("timing_fits", actual <= target + 0.08))
        if fits and actual <= target + 0.08:
            continue
        characters = max(normalized_character_count(str(segment.content)), 1)
        if actual > 0:
            budget = int(math.floor(characters * target / actual * safety_margin))
        else:
            budget = characters - 1
        budget = max(1, min(budget, max(characters - 1, 1)))
        requests.append(
            RewriteRequest(
                segment_index=int(segment.index),
                source_segment_id=int(
                    getattr(segment, "source_segment_id", None) or segment.index
                ),
                text=str(segment.content),
                target_seconds=target,
                max_characters=budget,
            )
        )
    return requests


@dataclass
class TimingSolveResult:
    segments: List[RuntimeSegment]
    plans: List[TimingPlan]
    unresolved_source_ids: List[int] = field(default_factory=list)
    rewrite_rounds: int = 0


@dataclass(frozen=True)
class AudioFitResult:
    output_path: str
    source_duration_seconds: float
    target_duration_seconds: float
    applied_atempo: float
    output_duration_seconds: float
    fits: bool


def normalized_character_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text.strip()))


def estimate_tts_duration(text: str, policy: Optional[TimingPolicy] = None) -> float:
    config = policy or TimingPolicy()
    characters = normalized_character_count(text)
    punctuation_pauses = len(re.findall(r"[,.!?;:…，。！？；：]", text)) * 0.10
    return characters / config.estimated_chars_per_second + punctuation_pauses


def plan_segment(
    segment: RuntimeSegment, policy: Optional[TimingPolicy] = None
) -> TimingPlan:
    config = policy or TimingPolicy()
    target = max(
        config.min_segment_seconds,
        (segment.end - segment.start).total_seconds(),
    )
    estimate = estimate_tts_duration(segment.content, config)
    required_atempo = estimate / target if target > 0 else float("inf")
    budget = max(
        4,
        int(math.floor(target * config.estimated_chars_per_second * config.atempo_max)),
    )
    fits = required_atempo <= config.atempo_max
    return TimingPlan(
        segment_index=segment.index,
        source_segment_id=int(segment.source_segment_id or segment.index),
        target_seconds=target,
        estimated_seconds=estimate,
        character_budget=budget,
        required_atempo=required_atempo,
        fits=fits,
        action="keep" if fits else "shorten_or_split",
    )


def _copy_segments(segments: Iterable[Any]) -> List[RuntimeSegment]:
    return [segment_from_dict(segment_to_dict(segment)) for segment in segments]


def _split_clauses(text: str) -> List[str]:
    raw_parts = [
        part.strip()
        for part in re.split(r"(?<=[,.!?;:…，。！？；：])\s*", text.strip())
        if part.strip()
    ]
    parts = []
    pending_prefix = ""
    for part in raw_parts:
        has_spoken_content = any(character.isalnum() for character in part)
        if has_spoken_content:
            parts.append((pending_prefix + part).strip())
            pending_prefix = ""
        elif parts:
            parts[-1] += part
        else:
            pending_prefix += part
    if pending_prefix and parts:
        parts[-1] += pending_prefix
    if len(parts) > 1:
        return parts
    words = text.split()
    if len(words) < 8:
        return [text.strip()]
    midpoint = len(words) // 2
    return [" ".join(words[:midpoint]), " ".join(words[midpoint:])]


def _split_segment(segment: RuntimeSegment) -> List[RuntimeSegment]:
    parts = _split_clauses(segment.content)
    if len(parts) <= 1:
        return [segment]
    total_duration = max((segment.end - segment.start).total_seconds(), 0.001)
    weights = [max(normalized_character_count(part), 1) for part in parts]
    total_weight = sum(weights)
    current = segment.start
    split_segments = []
    for position, (part, weight) in enumerate(zip(parts, weights)):
        if position == len(parts) - 1:
            end = segment.end
        else:
            end = current + (segment.end - segment.start) * (weight / total_weight)
        split = segment_from_dict(segment_to_dict(segment))
        split.start = current
        split.end = end
        split.content = part
        split.source_segment_id = int(segment.source_segment_id or segment.index)
        split_segments.append(split)
        current = end
    return split_segments


RewriteCallback = Callable[[Sequence[RewriteRequest]], Mapping[int, str]]


def solve_segment_timing(
    segments: Iterable[Any],
    policy: Optional[TimingPolicy] = None,
    rewrite_callback: Optional[RewriteCallback] = None,
) -> TimingSolveResult:
    config = policy or TimingPolicy()
    working = _copy_segments(segments)
    rewrite_rounds = 0
    if rewrite_callback is not None:
        for _round in range(config.max_rewrite_rounds):
            requests = []
            for segment in working:
                plan = plan_segment(segment, config)
                if not plan.fits:
                    requests.append(
                        RewriteRequest(
                            segment_index=segment.index,
                            source_segment_id=plan.source_segment_id,
                            text=segment.content,
                            target_seconds=plan.target_seconds,
                            max_characters=plan.character_budget,
                        )
                    )
            if not requests:
                break
            rewritten = dict(rewrite_callback(requests))
            if not rewritten:
                break
            changed = False
            for segment in working:
                replacement = rewritten.get(segment.index)
                if replacement and replacement.strip() != segment.content.strip():
                    segment.content = replacement.strip()
                    changed = True
            rewrite_rounds += 1
            if not changed:
                break

    expanded = []
    for segment in working:
        if plan_segment(segment, config).fits:
            expanded.append(segment)
        else:
            expanded.extend(_split_segment(segment))
    for index, segment in enumerate(expanded, 1):
        segment.index = index
    plans = [plan_segment(segment, config) for segment in expanded]
    unresolved = sorted(
        {
            plan.source_segment_id
            for plan in plans
            if not plan.fits
        }
    )
    return TimingSolveResult(expanded, plans, unresolved, rewrite_rounds)


class GeminiTimingRewriter:
    """Ask Gemini to shorten only segments that exceed their duration budget."""

    def __init__(
        self,
        api_key: str,
        models: Sequence[str] = (
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-3.5-flash",
        ),
        timeout_seconds: float = 60.0,
        max_batch_requests: int = 60,
    ):
        self.api_key = api_key
        self.models = tuple(models)
        self.timeout_seconds = timeout_seconds
        self.max_batch_requests = max(1, int(max_batch_requests))

    def __call__(self, requests: Sequence[RewriteRequest]) -> Mapping[int, str]:
        if not self.api_key or not requests:
            return {}
        import requests as http_requests

        rewritten = {}
        for offset in range(0, len(requests), self.max_batch_requests):
            batch = list(requests[offset : offset + self.max_batch_requests])
            items = [
                {
                    "id": item.segment_index,
                    "text": item.text,
                    "max_characters": item.max_characters,
                    "target_seconds": round(item.target_seconds, 2),
                }
                for item in batch
            ]
            prompt = (
                "Rút gọn các câu tiếng Việt để lồng tiếng đúng thời lượng. Giữ nguyên ý, "
                "đại từ, tên riêng và giọng điệu; không cắt cụt ý. Mỗi câu không vượt quá "
                "max_characters. Chỉ trả về JSON dạng [{\"id\":1,\"text\":\"...\"}].\n"
                + json.dumps(items, ensure_ascii=False)
            )
            allowed = {item.segment_index: item.max_characters for item in batch}
            for model in self.models:
                url = (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "{}:generateContent?key={}".format(model, self.api_key)
                )
                try:
                    response = http_requests.post(
                        url,
                        json={"contents": [{"parts": [{"text": prompt}]}]},
                        timeout=self.timeout_seconds,
                    )
                    if response.status_code != 200:
                        continue
                    text = response.json()["candidates"][0]["content"]["parts"][0][
                        "text"
                    ]
                    match = re.search(r"\[[\s\S]*\]", text)
                    if not match:
                        continue
                    payload = json.loads(match.group(0))
                    for item in payload:
                        segment_id = int(item["id"])
                        candidate = str(item["text"]).strip()
                        if (
                            segment_id in allowed
                            and normalized_character_count(candidate) <= allowed[segment_id]
                        ):
                            rewritten[segment_id] = candidate
                    break
                except (
                    KeyError,
                    TypeError,
                    ValueError,
                    http_requests.RequestException,
                ):
                    continue
        return rewritten


def probe_audio_duration(path: PathLike, ffprobe_binary: str = "ffprobe") -> float:
    result = subprocess.run(
        [
            ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=_creation_flags(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe duration failed")
    return float(result.stdout.strip())


def fit_audio_to_window(
    input_path: PathLike,
    output_path: PathLike,
    target_seconds: float,
    policy: Optional[TimingPolicy] = None,
    ffmpeg_binary: str = "ffmpeg",
    ffprobe_binary: str = "ffprobe",
) -> AudioFitResult:
    config = policy or TimingPolicy()
    if target_seconds <= 0:
        raise ValueError("target_seconds must be positive")
    source_duration = probe_audio_duration(input_path, ffprobe_binary)
    required = source_duration / target_seconds
    if required > 1.0:
        applied = min(required, config.atempo_max)
    elif required < 1.0:
        applied = max(required, config.atempo_min)
    else:
        applied = 1.0
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(input_path)]
    if abs(applied - 1.0) > 0.001:
        command.extend(["-filter:a", "atempo={:.6f}".format(applied)])
    command.extend(["-vn", str(output_path)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        creationflags=_creation_flags(),
    )
    if result.returncode != 0 or not Path(output_path).is_file():
        raise RuntimeError(result.stderr.strip() or "FFmpeg audio fitting failed")
    output_duration = probe_audio_duration(output_path, ffprobe_binary)
    fits = output_duration <= target_seconds + 0.08
    return AudioFitResult(
        output_path=str(output_path),
        source_duration_seconds=source_duration,
        target_duration_seconds=target_seconds,
        applied_atempo=applied,
        output_duration_seconds=output_duration,
        fits=fits,
    )
