"""
Adaptive Auto-Ducking Audio Mixer for Tool V1.

Features:
1. Measures actual background music (BGM) and voice loudness (RMS/dBFS).
2. Calculates adaptive ducking depth based on actual BGM level (never a hardcoded fixed -15dB).
3. Smooth ducking transitions (150ms attack, 350ms release) to prevent abrupt volume jumps.
4. Anti-pumping: Keeps ducking active across short gaps (<400ms) between adjacent speech phrases.
5. Floating-point summation with a sample-peak ceiling before PCM conversion.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from pydub import AudioSegment
import numpy as np


@dataclass(frozen=True)
class AdaptiveDuckingSettings:
    """Settings for adaptive auto-ducking."""
    base_bgm_gain_db: float = -2.0
    base_voice_gain_db: float = 1.0
    max_peak_dbfs: float = -1.0
    attack_ms: int = 150
    release_ms: int = 350
    gap_merge_ms: int = 400
    # Dynamic ducking thresholds based on BGM level
    soft_bgm_threshold_dbfs: float = -25.0
    loud_bgm_threshold_dbfs: float = -15.0
    soft_duck_db: float = -5.0
    moderate_duck_db: float = -9.0
    loud_duck_db: float = -13.0


def calculate_adaptive_duck_gain(bgm_dbfs: float, settings: AdaptiveDuckingSettings) -> float:
    """
    Calculate ducking reduction in dB based on actual BGM loudness.
    Returns negative dB value (e.g. -5.0 to -13.0 dB).
    """
    if not math.isfinite(bgm_dbfs) or bgm_dbfs < -60.0:
        return 0.0  # Silent BGM doesn't need ducking
    
    if bgm_dbfs < settings.soft_bgm_threshold_dbfs:
        # Soft music: duck gently (-4 to -6 dB)
        return settings.soft_duck_db
    elif bgm_dbfs > settings.loud_bgm_threshold_dbfs:
        # Very loud music: duck deeper (-12 to -14 dB)
        return settings.loud_duck_db
    else:
        # Linear interpolation between soft and loud
        ratio = (bgm_dbfs - settings.soft_bgm_threshold_dbfs) / (
            settings.loud_bgm_threshold_dbfs - settings.soft_bgm_threshold_dbfs
        )
        return settings.soft_duck_db + ratio * (settings.loud_duck_db - settings.soft_duck_db)


def merge_ducking_intervals(
    dubs: Sequence[Mapping[str, Any]],
    total_duration_ms: int,
    attack_ms: int = 150,
    release_ms: int = 350,
    gap_merge_ms: int = 400,
) -> List[Tuple[int, int]]:
    """
    Calculate smoothed ducking time intervals [start_ms, end_ms] on the timeline.
    Merges adjacent intervals if the pause between them is less than gap_merge_ms (anti-pumping).
    """
    raw_intervals: List[Tuple[int, int]] = []
    for dub in dubs:
        start_sec = float(dub.get("start", 0.0))
        duration_sec = float(dub.get("actual_audio_duration", 0.0) or dub.get("duration", 0.0))
        if duration_sec <= 0 and "path" in dub and os.path.exists(dub["path"]):
            try:
                seg = AudioSegment.from_file(dub["path"])
                duration_sec = len(seg) / 1000.0
            except Exception:
                duration_sec = 1.0

        start_ms = max(0, int(start_sec * 1000) - attack_ms)
        end_ms = min(total_duration_ms, int((start_sec + duration_sec) * 1000) + release_ms)
        if end_ms > start_ms:
            raw_intervals.append((start_ms, end_ms))

    if not raw_intervals:
        return []

    raw_intervals.sort(key=lambda x: x[0])
    merged: List[Tuple[int, int]] = [raw_intervals[0]]
    for cur_start, cur_end in raw_intervals[1:]:
        prev_start, prev_end = merged[-1]
        if cur_start <= prev_end + gap_merge_ms:
            # Merge intervals to avoid rapid volume pumping
            merged[-1] = (prev_start, max(prev_end, cur_end))
        else:
            merged.append((cur_start, cur_end))

    return merged


def apply_adaptive_ducking(
    bgm: AudioSegment,
    dubs: Sequence[Mapping[str, Any]],
    settings: AdaptiveDuckingSettings,
) -> AudioSegment:
    """
    Apply smooth ducking gain envelope to background music track.
    """
    total_len = len(bgm)
    if total_len == 0 or not dubs:
        return bgm + settings.base_bgm_gain_db

    intervals = merge_ducking_intervals(
        dubs,
        total_duration_ms=total_len,
        attack_ms=settings.attack_ms,
        release_ms=settings.release_ms,
        gap_merge_ms=settings.gap_merge_ms,
    )

    if not intervals:
        return bgm + settings.base_bgm_gain_db

    # Apply envelopes in place on the timeline. Crossfading appended chunks
    # removes samples and shifts every subsequent music event earlier.
    normal_bgm = bgm + settings.base_bgm_gain_db

    result = AudioSegment.empty()
    cursor = 0

    for start_ms, end_ms in intervals:
        start_ms = max(0, min(total_len, start_ms))
        end_ms = max(start_ms, min(total_len, end_ms))

        # 1. Normal BGM segment before ducking
        if start_ms > cursor:
            part = normal_bgm[cursor:start_ms]
            result = result + part
            cursor = start_ms

        # 2. Ducked segment
        if end_ms > cursor:
            # A loud effect in one scene must not be averaged away by a long,
            # quiet soundtrack (or over-duck every other scene).
            duck_gain_reduction = calculate_adaptive_duck_gain(bgm[cursor:end_ms].dBFS, settings)
            part = normal_bgm[cursor:end_ms]
            attack = min(max(0, settings.attack_ms), len(part))
            release = min(max(0, settings.release_ms), len(part) - attack)
            pieces = []
            if attack:
                pieces.append(part[:attack].fade(from_gain=0, to_gain=duck_gain_reduction,
                                                 start=0, duration=attack))
            pieces.append(part[attack:len(part)-release] + duck_gain_reduction)
            if release:
                pieces.append(part[-release:].fade(from_gain=duck_gain_reduction,
                                                   to_gain=0, start=0, duration=release))
            for piece in pieces:
                result += piece
            cursor = end_ms

    # 3. Remaining normal BGM after last speech
    if cursor < total_len:
        rem_part = normal_bgm[cursor:]
        result = result + rem_part

    # Ensure length matches original BGM exactly
    if len(result) > total_len:
        result = result[:total_len]
    elif len(result) < total_len:
        result = result + normal_bgm[len(result):]

    return result


def apply_peak_limiter(audio: AudioSegment, max_peak_dbfs: float = -1.0) -> AudioSegment:
    """
    Enforce peak ceiling <= max_peak_dbfs to prevent clipping and distortion.
    """
    current_peak = audio.max_dBFS
    if math.isfinite(current_peak) and current_peak > max_peak_dbfs:
        reduction = current_peak - max_peak_dbfs
        audio = audio - reduction
    return audio


def mix_adaptive_audio(
    bgm_path: str,
    dubbing_audio_files: Sequence[Mapping[str, Any]],
    output_path: str,
    base_bgm_gain_db: float | None = None,
    base_voice_gain_db: float | None = None,
    settings: AdaptiveDuckingSettings | None = None,
) -> str:
    """
    Mix background music and dubbing audio with adaptive auto-ducking and peak limiting.
    """
    if settings is None:
        settings = AdaptiveDuckingSettings(
            base_bgm_gain_db=base_bgm_gain_db if base_bgm_gain_db is not None else -2.0,
            base_voice_gain_db=base_voice_gain_db if base_voice_gain_db is not None else 1.0,
        )
    elif base_bgm_gain_db is not None or base_voice_gain_db is not None:
        settings = replace(settings,
            base_bgm_gain_db=base_bgm_gain_db if base_bgm_gain_db is not None else settings.base_bgm_gain_db,
            base_voice_gain_db=base_voice_gain_db if base_voice_gain_db is not None else settings.base_voice_gain_db,
        )

    bgm = AudioSegment.from_file(bgm_path)
    # 1. Apply adaptive ducking on BGM
    ducked_bgm = apply_adaptive_ducking(bgm, dubbing_audio_files, settings)

    # Sum in float: integer PCM overlay clips before a later limiter can act.
    def samples(audio):
        audio = audio.set_frame_rate(bgm.frame_rate).set_channels(bgm.channels)
        return np.asarray(audio.get_array_of_samples(), dtype=np.float32) / float(1 << (8 * audio.sample_width - 1))

    mixed_samples = samples(ducked_bgm)
    for dub in dubbing_audio_files:
        path = dub.get("path")
        if not path or not os.path.isfile(path):
            raise FileNotFoundError("Missing dubbing audio: {}".format(path))
        dub_audio = AudioSegment.from_file(path)

        # Voice gain with individual peak guard
        voice_gain = settings.base_voice_gain_db
        if dub_audio.max_dBFS + voice_gain > -0.5:
            voice_gain = max(-5.0, -0.5 - dub_audio.max_dBFS)
        voice = samples(dub_audio) * (10.0 ** (voice_gain / 20.0))
        position = max(0, int(float(dub.get("start", 0.0)) * bgm.frame_rate)) * bgm.channels
        count = min(len(voice), len(mixed_samples) - position)
        if count > 0:
            mixed_samples[position:position + count] += voice[:count]

    # Sample peak, not an oversampled true-peak measurement.
    peak = float(np.max(np.abs(mixed_samples))) if len(mixed_samples) else 0.0
    ceiling = 10.0 ** (min(0.0, settings.max_peak_dbfs) / 20.0)
    if peak > ceiling:
        mixed_samples *= ceiling / peak
    pcm = (np.clip(mixed_samples, -1.0, 1.0) * 32767).astype('<i2')
    mixed = AudioSegment(pcm.tobytes(), sample_width=2,
                         frame_rate=bgm.frame_rate, channels=bgm.channels)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    mixed.export(output_path, format="wav").close()
    return output_path
