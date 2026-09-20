"""Audio enhancement engine for AutoDub / Tool V2.

Provides Selective Timeline Splicing to preserve 100% untouched original audio
during non-speech intervals (music interludes, foley, effects, pauses),
combined with High-Frequency Air Restoration (>14kHz) and smooth cosine crossfading.
"""

from __future__ import annotations

import math
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

import numpy as np
import scipy.signal
import soundfile as sf


def resample_audio(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample multi-channel or single-channel audio using polyphase filtering."""
    if orig_sr == target_sr:
        return audio
    gcd = math.gcd(orig_sr, target_sr)
    up = target_sr // gcd
    down = orig_sr // gcd
    return scipy.signal.resample_poly(audio, up, down, axis=0)


def _to_seconds(val: Any) -> float:
    if val is None:
        return 0.0
    if hasattr(val, "total_seconds"):
        return float(val.total_seconds())
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def build_speech_mask(
    num_samples: int,
    sample_rate: int,
    speech_segments: Sequence[Mapping[str, Any]],
    pad_ms: float = 100.0,
    crossfade_ms: float = 40.0,
    min_gap_ms: float = 400.0,
) -> np.ndarray:
    """Build a continuous [0.0, 1.0] mask where 1.0 = speech (separated BGM)

    and 0.0 = non-speech (100% pristine original audio).
    Transitions use smooth raised-cosine crossfading.
    """
    mask = np.zeros(num_samples, dtype=np.float32)
    if not speech_segments or num_samples <= 0:
        return mask

    pad_samples = int(round((pad_ms / 1000.0) * sample_rate))
    min_gap_samples = int(round((min_gap_ms / 1000.0) * sample_rate))
    fade_samples = max(4, int(round((crossfade_ms / 1000.0) * sample_rate)))

    # Parse and expand intervals
    raw_intervals = []
    for seg in speech_segments:
        start_val = seg.get("start", seg.get("start_time", 0.0))
        end_val = seg.get("end", seg.get("end_time", start_val))
        start_sec = _to_seconds(start_val)
        end_sec = _to_seconds(end_val)
        if end_sec <= start_sec:
            continue
        start_samp = max(0, int(round(start_sec * sample_rate)) - pad_samples)
        end_samp = min(num_samples, int(round(end_sec * sample_rate)) + pad_samples)
        if end_samp > start_samp:
            raw_intervals.append((start_samp, end_samp))

    if not raw_intervals:
        return mask

    # Sort intervals
    raw_intervals.sort(key=lambda item: item[0])

    # Merge overlapping or close intervals (gap < min_gap_samples)
    merged_intervals = []
    curr_start, curr_end = raw_intervals[0]
    for nxt_start, nxt_end in raw_intervals[1:]:
        if nxt_start <= curr_end + min_gap_samples:
            curr_end = max(curr_end, nxt_end)
        else:
            merged_intervals.append((curr_start, curr_end))
            curr_start, curr_end = nxt_start, nxt_end
    merged_intervals.append((curr_start, curr_end))

    # Apply raised-cosine fade in and fade out
    for start_idx, end_idx in merged_intervals:
        # Core speech block
        mask[start_idx:end_idx] = 1.0

        # Fade in transition at start
        f_in = min(fade_samples, (end_idx - start_idx) // 2)
        trans_start = max(0, start_idx - f_in)
        actual_in_len = start_idx - trans_start
        if actual_in_len > 0:
            # Raised-cosine ramp from 0.0 to 1.0
            ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, actual_in_len, endpoint=False)))
            mask[trans_start:start_idx] = np.maximum(mask[trans_start:start_idx], ramp_in)

        # Fade out transition at end
        f_out = min(fade_samples, (end_idx - start_idx) // 2)
        trans_end = min(num_samples, end_idx + f_out)
        actual_out_len = trans_end - end_idx
        if actual_out_len > 0:
            # Raised-cosine ramp from 1.0 to 0.0
            ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0, np.pi, actual_out_len, endpoint=False)))
            mask[end_idx:trans_end] = np.maximum(mask[end_idx:trans_end], ramp_out)

    return np.clip(mask, 0.0, 1.0)


def extract_high_frequencies(
    audio: np.ndarray,
    sample_rate: int,
    cutoff_hz: float = 14000.0,
) -> np.ndarray:
    """Extract pristine high-frequency 'air' (> cutoff_hz) using Butterworth filter."""
    nyquist = 0.5 * sample_rate
    if cutoff_hz >= nyquist - 500:
        return np.zeros_like(audio)

    normalized_cutoff = cutoff_hz / nyquist
    sos = scipy.signal.butter(4, normalized_cutoff, btype="highpass", output="sos")
    filtered = scipy.signal.sosfilt(sos, audio, axis=0)
    return filtered.astype(np.float32)


def preserve_pristine_background(
    original_audio_path: Union[str, os.PathLike],
    separated_bgm_path: Union[str, os.PathLike],
    speech_segments: Sequence[Mapping[str, Any]],
    output_path: Union[str, os.PathLike],
    pad_ms: float = 100.0,
    crossfade_ms: float = 40.0,
    min_gap_ms: float = 400.0,
    restore_high_freq: bool = True,
    hf_cutoff_hz: float = 14000.0,
) -> str:
    """Combine untouched original audio in non-speech intervals with separated BGM in speech intervals.

    Returns the path to the resulting enhanced pristine audio file.
    """
    orig_path = Path(original_audio_path)
    sep_path = Path(separated_bgm_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not orig_path.is_file():
        raise FileNotFoundError(f"Original audio file not found: {orig_path}")

    # If separated audio is missing or no speech segments exist, copy original
    if not sep_path.is_file() or not speech_segments:
        shutil.copy2(str(orig_path), str(out_path))
        return str(out_path)

    # Read original audio
    orig_data, orig_sr = sf.read(str(orig_path), dtype="float32")
    if orig_data.ndim == 1:
        orig_data = orig_data[:, np.newaxis]

    num_samples, num_channels = orig_data.shape

    # Read separated BGM
    sep_data, sep_sr = sf.read(str(sep_path), dtype="float32")
    if sep_data.ndim == 1:
        sep_data = sep_data[:, np.newaxis]

    # Handle channel mismatch
    if sep_data.shape[1] < num_channels:
        sep_data = np.repeat(sep_data, num_channels, axis=1)
    elif sep_data.shape[1] > num_channels:
        sep_data = sep_data[:, :num_channels]

    # Handle sample rate mismatch
    if sep_sr != orig_sr:
        sep_data = resample_audio(sep_data, sep_sr, orig_sr)

    # Align lengths
    if len(sep_data) < num_samples:
        pad_len = num_samples - len(sep_data)
        sep_data = np.pad(sep_data, ((0, pad_len), (0, 0)), mode="constant")
    elif len(sep_data) > num_samples:
        sep_data = sep_data[:num_samples]

    # High frequency restoration (>14kHz air)
    enhanced_sep = sep_data
    if restore_high_freq and orig_sr >= 32000:
        try:
            hf_air = extract_high_frequencies(orig_data, orig_sr, cutoff_hz=hf_cutoff_hz)
            # Add subtle high-frequency air to the separated track (scaled to avoid boost)
            enhanced_sep = np.clip(sep_data + 0.65 * hf_air, -1.0, 1.0)
        except Exception as exc:
            # Fallback to pure separated data if filtering fails
            enhanced_sep = sep_data

    # Generate smooth speech mask
    mask_1d = build_speech_mask(
        num_samples=num_samples,
        sample_rate=orig_sr,
        speech_segments=speech_segments,
        pad_ms=pad_ms,
        crossfade_ms=crossfade_ms,
        min_gap_ms=min_gap_ms,
    )
    mask = mask_1d[:, np.newaxis]

    # Selective Timeline Splicing:
    # 0.0 (non-speech) -> 100% original audio
    # 1.0 (speech) -> separated BGM with air restoration
    pristine_bgm = (1.0 - mask) * orig_data + mask * enhanced_sep

    # Write output WAV with 24-bit PCM for professional studio fidelity
    sf.write(str(out_path), pristine_bgm, orig_sr, subtype="PCM_24")
    return str(out_path)
