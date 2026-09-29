"""Duration budgeting and light-touch audio fitting before dubbing."""

from __future__ import annotations

import json
import math
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

from .segments import RuntimeSegment, segment_from_dict, segment_to_dict

try:
    from ..subtitle_text import (
        clean_incomplete_segment_stops,
        normalize_subtitle_text,
        split_subtitle_sentences,
    )
except ImportError:
    from subtitle_text import (
        clean_incomplete_segment_stops,
        normalize_subtitle_text,
        split_subtitle_sentences,
    )


PathLike = Union[str, os.PathLike]


def _creation_flags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


@dataclass(frozen=True)
class TimingPolicy:
    atempo_min: float = 1.00
    atempo_max: float = 1.50
    estimated_chars_per_second: float = 11.5
    min_segment_seconds: float = 0.45
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
    if characters <= 0:
        return 0.0
    punctuation_pauses = len(re.findall(r"[,.!?;:…，。！？；：]", text)) * 0.10
    raw = characters / config.estimated_chars_per_second + punctuation_pauses
    # Acoustic floor for speech synthesis (onset, vowels, release, tail):
    # Standard TTS audio clip is at least 1.10s.
    return max(1.10, raw)


def plan_segment(
    segment: RuntimeSegment, policy: Optional[TimingPolicy] = None
) -> TimingPlan:
    clean_chars = [c for c in str(segment.content or "") if c.isalnum() or '\u4e00' <= c <= '\u9fff']
    if getattr(segment, "is_unvoiced_scene", False) or getattr(segment, "is_silent_fallback", False) or not clean_chars:
        target = max(
            0.1,
            (segment.end - segment.start).total_seconds(),
        )
        return TimingPlan(
            segment_index=segment.index,
            source_segment_id=int(getattr(segment, "source_segment_id", None) or segment.index),
            target_seconds=target,
            estimated_seconds=target,
            character_budget=9999,
            required_atempo=1.0,
            fits=True,
            action="keep",
        )
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


def _split_clauses(text: str, min_words: int = 4, min_chars: int = 15) -> List[str]:
    sentences = split_subtitle_sentences(text)
    if len(sentences) > 1:
        merged_sents: List[str] = []
        for s in sentences:
            if not merged_sents:
                merged_sents.append(s)
            elif len(merged_sents[-1].split()) < min_words or len(merged_sents[-1]) < min_chars:
                merged_sents[-1] = merged_sents[-1] + " " + s
            elif len(s.split()) < min_words or len(s) < min_chars:
                merged_sents[-1] = merged_sents[-1] + " " + s
            else:
                merged_sents.append(s)
        if len(merged_sents) > 1:
            return merged_sents

    raw_parts = [
        part.strip()
        for part in re.split(r"(?<=[,.!?;:，。！？；：])\s*", text.strip())
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

    if len(parts) <= 1:
        words = text.split()
        if len(words) < 10:
            return [text.strip()]
        midpoint = len(words) // 2
        return [" ".join(words[:midpoint]), " ".join(words[midpoint:])]

    # Merge short comma-delimited parts into neighboring clauses to avoid micro-segments (< 4 words or < 15 chars)
    merged: List[str] = []
    for part in parts:
        words_count = len(part.split())
        chars_count = len(part)
        if not merged:
            merged.append(part)
        elif len(merged[-1].split()) < min_words or len(merged[-1]) < min_chars:
            merged[-1] = merged[-1] + " " + part
        elif words_count < min_words or chars_count < min_chars:
            merged[-1] = merged[-1] + " " + part
        else:
            merged.append(part)

    # Ensure the first segment isn't a dangling micro-fragment
    if len(merged) > 1 and (len(merged[0].split()) < min_words or len(merged[0]) < min_chars):
        merged[1] = merged[0] + " " + merged[1]
        merged.pop(0)

    if len(merged) > 1:
        return merged

    words = text.split()
    if len(words) < 10:
        return [text.strip()]
    midpoint = len(words) // 2
    return [" ".join(words[:midpoint]), " ".join(words[midpoint:])]


def _split_segment(segment: RuntimeSegment) -> List[RuntimeSegment]:
    total_duration = max((segment.end - segment.start).total_seconds(), 0.001)
    if total_duration < 0.70:
        return [segment]
    parts = _split_clauses(segment.content)
    if len(parts) <= 1:
        return [segment]
    if (total_duration / len(parts)) < 0.32:
        return [segment]
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


def borrow_gap_silence(
    segments: Sequence[RuntimeSegment],
    policy: Optional[TimingPolicy] = None,
    max_borrow: float = 0.45,
    min_buffer: float = 0.12,
) -> List[RuntimeSegment]:
    """Borrow silence from neighboring gaps so tight or overflowing segments fit naturally."""
    from datetime import timedelta
    config = policy or TimingPolicy()
    seg_list = list(segments)
    for i, seg in enumerate(seg_list):
        clean_chars = [c for c in str(seg.content or "") if c.isalnum() or '\u4e00' <= c <= '\u9fff']
        if not clean_chars or getattr(seg, "is_unvoiced_scene", False) or getattr(seg, "is_silent_fallback", False):
            continue
        target = (seg.end - seg.start).total_seconds()
        est = estimate_tts_duration(seg.content, config)
        desired = est / 1.18
        if desired <= target:
            continue
        needed = desired - target

        # 1. Borrow from gap after
        if i + 1 < len(seg_list):
            next_s = seg_list[i + 1].start.total_seconds()
            gap_after = max(0.0, next_s - seg.end.total_seconds())
            avail_after = max(0.0, gap_after - min_buffer)
            borrow_after = min(needed, avail_after, max_borrow)
            if borrow_after > 0.01:
                seg.end = seg.end + timedelta(seconds=borrow_after)
                needed -= borrow_after
                target += borrow_after

        # 2. Borrow from gap before if still needed
        if needed > 0.01 and i > 0:
            prev_e = seg_list[i - 1].end.total_seconds()
            gap_before = max(0.0, seg.start.total_seconds() - prev_e)
            avail_before = max(0.0, gap_before - min_buffer)
            borrow_before = min(needed, avail_before, max_borrow)
            if borrow_before > 0.01:
                seg.start = max(timedelta(0), seg.start - timedelta(seconds=borrow_before))
                needed -= borrow_before
                target += borrow_before
    return seg_list


def solve_segment_timing(
    segments: Iterable[Any],
    policy: Optional[TimingPolicy] = None,
    rewrite_callback: Optional[RewriteCallback] = None,
) -> TimingSolveResult:
    config = policy or TimingPolicy()
    working = _copy_segments(segments)
    for segment in working:
        segment.content = normalize_subtitle_text(segment.content)
    clean_incomplete_segment_stops(working)
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
                replacement = normalize_subtitle_text(rewritten.get(segment.index))
                if replacement and replacement.strip() != segment.content.strip():
                    segment.content = replacement.strip()
                    changed = True
            rewrite_rounds += 1
            if not changed:
                break
            clean_incomplete_segment_stops(working)

    working = borrow_gap_silence(working, config)
    expanded = []
    for segment in working:
        # Sentence changes are display cues in the ASS renderer. Keeping a
        # fitting speech window together avoids extra TTS/RVC requests and
        # artificial pauses without combining sentences on the subtitle card.
        if plan_segment(segment, config).fits:
            expanded.append(segment)
        else:
            expanded.extend(_split_segment(segment))
    expanded = borrow_gap_silence(expanded, config)
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


_timing_rewriter_unhealthy_until = 0.0


def _check_gemini_available() -> bool:
    return time.time() >= _timing_rewriter_unhealthy_until


def _mark_gemini_cooldown(cooldown_seconds: float = 30.0) -> None:
    global _timing_rewriter_unhealthy_until
    _timing_rewriter_unhealthy_until = time.time() + cooldown_seconds
    logger.info("Timing rewriter cooling down for %g seconds", cooldown_seconds)


def _local_shorten_vietnamese(text: str, max_chars: int) -> str:
    """Rule-based local shortening for Vietnamese dubbing when LLM is unavailable."""
    if not text:
        return ""
    cand = normalize_subtitle_text(text)
    if normalized_character_count(cand) <= max_chars:
        return cand

    # 1. Remove conversational filler phrases/particles
    fillers = [
        r",?\s*được không\??", r",?\s*được chưa\??", r",?\s*nhé\b", r",?\s*nhá\b", r",?\s*nha\b",
        r",?\s*ạ\b", r",?\s*nhỉ\b", r",?\s*nào\b", r",?\s*vậy\b", r",?\s*chứ\b",
        r"\bthực ra\s*", r"\bthực sự\s*", r"\brõ ràng\s*", r"\bnhưng mà\s*",
        r"\bcho tôi hỏi\s*", r"\btôi nghĩ rằng\s*",
    ]
    for f in fillers:
        cand = re.sub(f, "", cand, flags=re.IGNORECASE).strip()
        if normalized_character_count(cand) <= max_chars:
            return cand

    # 2. Keep whole words up to max_chars
    words = cand.split()
    trimmed = []
    curr_len = 0
    for w in words:
        w_len = len(w) + (1 if trimmed else 0)
        if curr_len + w_len <= max_chars:
            trimmed.append(w)
            curr_len += w_len
        else:
            break
    if trimmed:
        return " ".join(trimmed).strip(",.!? ")
    return cand[:max_chars].strip(",.!? ")


class GeminiTimingRewriter:
    """Ask Gemini to shorten only segments that exceed their duration budget."""

    def __init__(
        self,
        api_key: str,
        models: Optional[Sequence[str]] = None,
        timeout_seconds: float = 15.0,
        max_batch_requests: int = 60,
    ):
        self.api_key = api_key
        if models is None:
            try:
                from ai.model_policy import current_model_policy
            except ImportError:
                from backend.ai.model_policy import current_model_policy

            models = current_model_policy().gemini_candidates
        self.models = tuple(models)
        self.timeout_seconds = timeout_seconds
        self.max_batch_requests = max(1, int(max_batch_requests))

    def __call__(self, requests: Sequence[RewriteRequest]) -> Mapping[int, str]:
        if not requests:
            return {}
        rewritten = {}
        if self.api_key and _check_gemini_available():
            import requests as http_requests

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
                    "đại từ, tên riêng, giọng điệu và dấu phẩy ngắt nghỉ (,); không cắt cụt ý. Mỗi câu không vượt quá "
                    "max_characters. Không dùng dấu ba chấm (... hoặc …); chỉ đặt dấu chấm (. ! ?) "
                    "khi hết câu hoàn chỉnh, tuyệt đối không chèn dấu chấm ở câu lửng. "
                    "Chỉ trả về JSON dạng [{\"id\":1,\"text\":\"Câu đã rút gọn\"}].\n"
                    + json.dumps(items, ensure_ascii=False)
                )
                allowed = {item.segment_index: item.max_characters for item in batch}
                for model in self.models:
                    if not _check_gemini_available():
                        break
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        "{}:generateContent".format(model)
                    )
                    headers = {
                        "Content-Type": "application/json",
                        "x-goog-api-key": self.api_key,
                    }
                    try:
                        response = http_requests.post(
                            url,
                            headers=headers,
                            json={"contents": [{"parts": [{"text": prompt}]}]},
                            timeout=self.timeout_seconds,
                        )
                        if response.status_code == 429:
                            _mark_gemini_cooldown(180.0)
                            break
                        if response.status_code in (500, 502, 503, 504):
                            logger.info("Timing rewriter: model %s returned HTTP %s, trying backup model...", model, response.status_code)
                            continue
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
                            candidate = normalize_subtitle_text(item["text"])
                            if (
                                segment_id in allowed
                                and candidate
                                and normalized_character_count(candidate) <= allowed[segment_id]
                            ):
                                rewritten[segment_id] = candidate
                        break
                    except (
                        http_requests.Timeout,
                        http_requests.ConnectionError,
                    ):
                        _mark_gemini_cooldown(180.0)
                        break
                    except (
                        KeyError,
                        TypeError,
                        ValueError,
                        http_requests.RequestException,
                    ):
                        continue

        # Fallback to local rule-based shortening for any requests not resolved by Gemini
        for item in requests:
            if item.segment_index not in rewritten:
                cand = _local_shorten_vietnamese(item.text, item.max_characters)
                if cand and cand != item.text:
                    rewritten[item.segment_index] = cand

        return rewritten


def probe_audio_duration(path: PathLike, ffprobe_binary: str = "ffprobe") -> float:
    # 1. Thử đọc nhanh header âm thanh bằng soundfile (in-process, < 0.1ms, tránh spawn hàng nghìn subprocess trên Windows)
    try:
        ext = Path(path).suffix.lower()
        if ext in (".wav", ".flac", ".ogg", ".mp3", ".m4a"):
            import soundfile as sf
            info = sf.info(str(path))
            if info.duration > 0:
                return float(info.duration)
    except Exception:
        pass

    # 2. Fallback sang ffprobe cho các định dạng container video hoặc khi soundfile không đọc được
    try:
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
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timed out after 30s for: {path}")
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
    working_input = input_path
    temp_trimmed: Optional[Path] = None

    # If the source audio exceeds the target window, check if stripping leading/trailing
    # padding silence allows it to fit or reduces required speedup.
    if source_duration > target_seconds + 0.08:
        try:
            trimmed_candidate = Path(output_path).parent / f"trim_{Path(input_path).name}"
            trim_cmd = [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_path),
                "-af",
                "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-50dB,areverse,silenceremove=start_periods=1:start_duration=0.02:start_threshold=-50dB,areverse",
                str(trimmed_candidate),
            ]
            t_res = subprocess.run(
                trim_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                creationflags=_creation_flags(),
            )
            if t_res.returncode == 0 and trimmed_candidate.is_file():
                trimmed_dur = probe_audio_duration(trimmed_candidate, ffprobe_binary)
                # Only use trimmed if it contains audible speech (not pure silence collapsed to 0)
                if trimmed_dur >= 0.10:
                    working_input = trimmed_candidate
                    temp_trimmed = trimmed_candidate
                    source_duration = trimmed_dur
                else:
                    trimmed_candidate.unlink(missing_ok=True)
        except Exception:
            pass

    required = source_duration / target_seconds
    if required > 1.0:
        applied = min(required, config.atempo_max)
    elif required < 1.0:
        applied = max(required, config.atempo_min)
    else:
        applied = 1.0
    command = [ffmpeg_binary, "-hide_banner", "-loglevel", "error", "-y", "-i", str(working_input)]
    if abs(applied - 1.0) > 0.001:
        command.extend(["-filter:a", "atempo={:.6f}".format(applied)])
    command.extend(["-vn", str(output_path)])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            creationflags=_creation_flags(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFmpeg audio fitting timed out after 120s for: {input_path}")
    finally:
        if temp_trimmed is not None:
            try:
                temp_trimmed.unlink(missing_ok=True)
            except Exception:
                pass
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

