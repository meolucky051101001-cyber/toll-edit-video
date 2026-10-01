"""
Conditional Fallback ASR for Tool V1.

Detects suspicious gaps in the initial ASR output where character speech
might have been suppressed by loud background music or aggressive VAD.
Only re-transcribes those specific short snippets on the original audio.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import math
from datetime import timedelta
from typing import Any, Dict, List, Sequence, Tuple

import srt
from pydub import AudioSegment

logger = logging.getLogger("v1_conditional_asr")


def is_chinese_text(text: str) -> bool:
    """Check if string contains valid Chinese characters."""
    return any("\u4e00" <= c <= "\u9fff" for c in text)


def is_hallucination(text: str) -> bool:
    """Detect common Whisper hallucinations and repetitive loops."""
    clean = text.strip()
    if len(clean) <= 1:
        return True
    # Repetitive characters: e.g. "啊啊啊啊啊" or "的的的的"
    if len(set(clean)) <= 2 and len(clean) >= 6:
        return True
    # Repetitive phrase loops
    tokens = clean.split()
    if len(tokens) >= 4 and len(set(tokens)) == 1:
        return True
    # Known prompt / credit hallucinations in Chinese
    hallucination_phrases = (
        "请不吝点赞",
        "点赞关注",
        "欢迎订阅",
        "谢谢观看",
        "字幕由",
        "关注我",
    )
    if any(p in clean for p in hallucination_phrases):
        return True
    return False


def detect_suspicious_gaps(
    segments: Sequence[srt.Subtitle],
    total_duration_seconds: float,
    min_gap_seconds: float = 2.5,
) -> List[Tuple[float, float]]:
    """
    Find gaps in subtitle timeline that are long enough to hold a missed speech line.
    """
    gaps: List[Tuple[float, float]] = []
    if not segments:
        if total_duration_seconds >= 1.5:
            gaps.append((0.0, total_duration_seconds))
        return gaps

    # Use the union of covered intervals: nested or unsorted ASR cues must
    # never cause a retry over speech that has already been recognized.
    cursor = 0.0
    for segment in sorted(segments, key=lambda item: item.start):
        start = max(0.0, min(total_duration_seconds, segment.start.total_seconds()))
        end = max(start, min(total_duration_seconds, segment.end.total_seconds()))
        threshold = 2.0 if cursor == 0 else min_gap_seconds
        if start - cursor >= threshold:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if total_duration_seconds - cursor >= 2.0:
        gaps.append((cursor, total_duration_seconds))

    return gaps


def filter_gaps_with_energy(
    audio_path: str,
    gaps: Sequence[Tuple[float, float]],
    min_energy_dbfs: float = -38.0,
) -> List[Tuple[float, float]]:
    """
    Filter gaps to keep only those containing non-trivial acoustic energy in original.wav.
    Genuine silence or low-level background noise is ignored.
    """
    if not gaps or not os.path.exists(audio_path):
        return []

    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception as e:
        logger.warning("Cannot load audio for gap analysis: %s", e)
        return []

    total_len_ms = len(audio)
    verified_gaps: List[Tuple[float, float]] = []

    for start_sec, end_sec in gaps:
        duration = end_sec - start_sec
        if duration < 0.8:
            continue

        start_ms = max(0, int(start_sec * 1000))
        end_ms = min(total_len_ms, int(end_sec * 1000))
        if end_ms <= start_ms:
            continue

        slice_audio = audio[start_ms:end_ms]
        # If segment is not dead silence, it might contain masked speech
        if slice_audio.dBFS >= min_energy_dbfs:
            verified_gaps.append((start_sec, end_sec))

    return verified_gaps


def retranscribe_gap_snippet(
    model: Any,
    original_audio: AudioSegment,
    gap_start: float,
    gap_end: float,
    temp_dir: str,
) -> List[Dict[str, Any]]:
    """
    Re-transcribe a short audio slice on original.wav using sensitive ASR settings.
    """
    pad_sec = 0.15
    start_sec = max(0.0, gap_start - pad_sec)
    end_sec = min(len(original_audio) / 1000.0, gap_end + pad_sec)
    duration = end_sec - start_sec
    if duration < 0.6:
        return []

    start_ms = int(start_sec * 1000)
    end_ms = int(end_sec * 1000)
    snippet = original_audio[start_ms:end_ms]

    temp_wav = os.path.join(temp_dir, f"gap_{int(gap_start*1000)}.wav")
    snippet.export(temp_wav, format="wav")

    # Use sensitive parameters for short suspected segments
    vad_active = duration > 4.0
    vad_params = dict(threshold=0.20, min_silence_duration_ms=300) if vad_active else None

    recovered: List[Dict[str, Any]] = []
    try:
        segments, _ = model.transcribe(
            temp_wav,
            beam_size=3,
            language="zh",
            vad_filter=vad_active,
            vad_parameters=vad_params,
            condition_on_previous_text=False,
            temperature=[0.0, 0.2],
        )
        for seg in segments:
            text = seg.text.strip()
            if not text or not is_chinese_text(text) or is_hallucination(text):
                continue
            # Energy alone also admits music. Reject low-confidence decoding
            # instead of treating every Chinese-looking result as speech.
            if (float(getattr(seg, "no_speech_prob", 0.0)) > 0.6
                    or float(getattr(seg, "avg_logprob", 0.0)) < -1.0
                    or float(getattr(seg, "compression_ratio", 0.0)) > 2.4):
                continue
            if not all(math.isfinite(float(v)) for v in (seg.start, seg.end)):
                continue
            abs_start = max(gap_start, start_sec + float(seg.start))
            abs_end = min(gap_end, start_sec + float(seg.end))
            if abs_end > abs_start + 0.3:
                recovered.append({
                    "start": abs_start,
                    "end": abs_end,
                    "text": text,
                })
    except Exception as e:
        logger.warning("Error retranscribing gap %.2f - %.2f: %s", gap_start, gap_end, e)
    finally:
        if os.path.exists(temp_wav):
            try:
                os.remove(temp_wav)
            except OSError:
                pass

    return recovered


def run_conditional_asr(
    original_audio_path: str,
    initial_segments: Sequence[srt.Subtitle],
    whisper_model: Any | None = None,
    min_gap_seconds: float = 2.5,
    max_retry_audio_seconds: float = 30.0,
    max_snippet_seconds: float = 8.0,
    retry_budget_seconds: float = 45.0,
) -> List[srt.Subtitle]:
    """
    High-level entrypoint:
    If suspicious gaps with acoustic energy exist, re-transcribes them on original_audio_path.
    Returns merged, re-indexed list of Subtitle objects.
    """
    if not os.path.exists(original_audio_path) or whisper_model is None:
        return list(initial_segments)

    try:
        audio = AudioSegment.from_file(original_audio_path)
        total_duration = len(audio) / 1000.0
    except Exception:
        return list(initial_segments)

    # 1. Detect gaps
    raw_gaps = detect_suspicious_gaps(initial_segments, total_duration, min_gap_seconds=min_gap_seconds)
    # 2. Filter gaps with non-trivial energy on original audio
    # Reuse the decoded audio and bound both snippet size and retry workload.
    # The wall-clock budget is checked between calls; the isolated worker
    # remains responsible for terminating a single hung model call.
    verified_gaps = []
    remaining = max(0.0, max_retry_audio_seconds)
    if max_snippet_seconds <= 0:
        raise ValueError("max_snippet_seconds must be positive")
    for left, right in raw_gaps:
        while right - left >= 0.8 and remaining >= 0.8:
            end = min(right, left + max_snippet_seconds, left + remaining)
            if audio[int(left * 1000):int(end * 1000)].dBFS >= -38.0:
                verified_gaps.append((left, end))
                remaining -= end - left
            left = end

    if not verified_gaps:
        return list(initial_segments)

    requested_seconds = sum(end - start for start, end in raw_gaps)
    if remaining < 0.8 and requested_seconds > max_retry_audio_seconds:
        logger.warning("ASR recovery audio budget reached; some gaps remain unverified")

    logger.info("Found %d suspicious gap(s) with audio energy to re-verify with sensitive ASR", len(verified_gaps))

    recovered_items: List[Dict[str, Any]] = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="v1-gap-asr-") as tmpdir:
        for gap_start, gap_end in verified_gaps:
            if time.monotonic() - started >= retry_budget_seconds:
                logger.warning("ASR recovery budget reached; remaining gaps unresolved")
                break
            items = retranscribe_gap_snippet(whisper_model, audio, gap_start, gap_end, tmpdir)
            if items:
                logger.info("🎯 Recovered missed speech in gap [%.2fs - %.2fs]: %s",
                            gap_start, gap_end, [x["text"] for x in items])
                recovered_items.extend(items)

    if not recovered_items:
        return list(initial_segments)

    # Merge recovered subtitles with initial segments
    combined: List[Tuple[float, float, str]] = []
    for s in initial_segments:
        combined.append((s.start.total_seconds(), s.end.total_seconds(), s.content))
    for r in recovered_items:
        combined.append((r["start"], r["end"], r["text"]))

    # Sort by start time
    combined.sort(key=lambda x: x[0])

    merged_subs: List[srt.Subtitle] = []
    for idx, (st, et, txt) in enumerate(combined, start=1):
        merged_subs.append(
            srt.Subtitle(
                index=idx,
                start=timedelta(seconds=st),
                end=timedelta(seconds=et),
                content=txt.strip(),
            )
        )

    return merged_subs
