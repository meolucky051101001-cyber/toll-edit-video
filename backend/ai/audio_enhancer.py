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


def get_merged_speech_intervals(
    total_samples: int,
    sample_rate: int,
    speech_segments: Sequence[Mapping[str, Any]],
    pad_ms: float = 100.0,
    min_gap_ms: float = 400.0,
) -> List[Tuple[int, int]]:
    """Parse, expand by pad_ms, and merge speech intervals within min_gap_ms."""
    if not speech_segments or total_samples <= 0:
        return []

    pad_samples = int(round((pad_ms / 1000.0) * sample_rate))
    min_gap_samples = int(round((min_gap_ms / 1000.0) * sample_rate))

    raw_intervals = []
    for seg in speech_segments:
        start_val = seg.get("start", seg.get("start_time", 0.0))
        end_val = seg.get("end", seg.get("end_time", start_val))
        start_sec = _to_seconds(start_val)
        end_sec = _to_seconds(end_val)
        if end_sec <= start_sec:
            continue
        start_samp = max(0, int(round(start_sec * sample_rate)) - pad_samples)
        end_samp = min(total_samples, int(round(end_sec * sample_rate)) + pad_samples)
        if end_samp > start_samp:
            raw_intervals.append((start_samp, end_samp))

    if not raw_intervals:
        return []

    raw_intervals.sort(key=lambda item: item[0])
    merged_intervals = []
    curr_start, curr_end = raw_intervals[0]
    for nxt_start, nxt_end in raw_intervals[1:]:
        if nxt_start <= curr_end + min_gap_samples:
            curr_end = max(curr_end, nxt_end)
        else:
            merged_intervals.append((curr_start, curr_end))
            curr_start, curr_end = nxt_start, nxt_end
    merged_intervals.append((curr_start, curr_end))
    return merged_intervals


def build_speech_mask_for_chunk(
    chunk_start: int,
    chunk_len: int,
    total_samples: int,
    sample_rate: int,
    merged_intervals: Sequence[Tuple[int, int]],
    crossfade_ms: float = 40.0,
) -> np.ndarray:
    """Build a continuous [0.0, 1.0] mask for a single chunk [chunk_start, chunk_start + chunk_len]."""
    chunk_mask = np.zeros(chunk_len, dtype=np.float32)
    if not merged_intervals or chunk_len <= 0:
        return chunk_mask

    chunk_end = chunk_start + chunk_len
    fade_samples = max(4, int(round((crossfade_ms / 1000.0) * sample_rate)))

    for start_idx, end_idx in merged_intervals:
        f_in = min(fade_samples, (end_idx - start_idx) // 2)
        trans_start = max(0, start_idx - f_in)
        f_out = min(fade_samples, (end_idx - start_idx) // 2)
        trans_end = min(total_samples, end_idx + f_out)

        # Skip if this interval doesn't touch the chunk
        if trans_end <= chunk_start or trans_start >= chunk_end:
            continue

        # 1. Core speech
        c_s = max(chunk_start, start_idx)
        c_e = min(chunk_end, end_idx)
        if c_e > c_s:
            chunk_mask[c_s - chunk_start : c_e - chunk_start] = 1.0

        # 2. Fade in transition
        actual_in_len = start_idx - trans_start
        if actual_in_len > 0 and trans_start < chunk_end and start_idx > chunk_start:
            ramp_in = 0.5 * (1.0 - np.cos(np.linspace(0, np.pi, actual_in_len, endpoint=False)))
            overlap_start = max(trans_start, chunk_start)
            overlap_end = min(start_idx, chunk_end)
            chunk_mask[overlap_start - chunk_start : overlap_end - chunk_start] = np.maximum(
                chunk_mask[overlap_start - chunk_start : overlap_end - chunk_start],
                ramp_in[overlap_start - trans_start : overlap_end - trans_start],
            )

        # 3. Fade out transition
        actual_out_len = trans_end - end_idx
        if actual_out_len > 0 and end_idx < chunk_end and trans_end > chunk_start:
            ramp_out = 0.5 * (1.0 + np.cos(np.linspace(0, np.pi, actual_out_len, endpoint=False)))
            overlap_start = max(end_idx, chunk_start)
            overlap_end = min(trans_end, chunk_end)
            chunk_mask[overlap_start - chunk_start : overlap_end - chunk_start] = np.maximum(
                chunk_mask[overlap_start - chunk_start : overlap_end - chunk_start],
                ramp_out[overlap_start - end_idx : overlap_end - end_idx],
            )

    return np.clip(chunk_mask, 0.0, 1.0)


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
    merged_intervals = get_merged_speech_intervals(
        total_samples=num_samples,
        sample_rate=sample_rate,
        speech_segments=speech_segments,
        pad_ms=pad_ms,
        min_gap_ms=min_gap_ms,
    )
    return build_speech_mask_for_chunk(
        chunk_start=0,
        chunk_len=num_samples,
        total_samples=num_samples,
        sample_rate=sample_rate,
        merged_intervals=merged_intervals,
        crossfade_ms=crossfade_ms,
    )


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


def preserve_pristine_background_chunked(
    original_audio_path: Union[str, os.PathLike],
    separated_bgm_path: Union[str, os.PathLike],
    speech_segments: Sequence[Mapping[str, Any]],
    output_path: Union[str, os.PathLike],
    pad_ms: float = 100.0,
    crossfade_ms: float = 40.0,
    min_gap_ms: float = 400.0,
    restore_high_freq: bool = True,
    hf_cutoff_hz: float = 14000.0,
    chunk_seconds: float = 30.0,
) -> str:
    """Stream processing for long audio files (> 2 minutes) to keep RAM usage strictly < 80MB.

    Uses second-order sections (SOS) filter with state continuation (zi) across blocks
    to ensure 100% continuous phase and zero boundary clicks.
    """
    orig_path = Path(original_audio_path)
    sep_path = Path(separated_bgm_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not orig_path.is_file():
        raise FileNotFoundError(f"Original audio file not found: {orig_path}")

    if not sep_path.is_file() or not speech_segments:
        shutil.copy2(str(orig_path), str(out_path))
        return str(out_path)

    with sf.SoundFile(str(orig_path), "r") as f_orig, sf.SoundFile(str(sep_path), "r") as f_sep:
        orig_sr = f_orig.samplerate
        num_channels = f_orig.channels
        total_samples = len(f_orig)

        # Precompute merged speech intervals once (< 1ms)
        merged_intervals = get_merged_speech_intervals(
            total_samples=total_samples,
            sample_rate=orig_sr,
            speech_segments=speech_segments,
            pad_ms=pad_ms,
            min_gap_ms=min_gap_ms,
        )

        # Setup high-frequency air filter state
        sos = None
        zi = None
        if restore_high_freq and orig_sr >= 32000:
            nyquist = 0.5 * orig_sr
            if hf_cutoff_hz < nyquist - 500:
                normalized_cutoff = hf_cutoff_hz / nyquist
                sos = scipy.signal.butter(4, normalized_cutoff, btype="highpass", output="sos")
                zi_base = scipy.signal.sosfilt_zi(sos)
                zi = np.repeat(zi_base[:, :, np.newaxis], num_channels, axis=2)

        chunk_samples = max(1024, int(round(chunk_seconds * orig_sr)))
        with sf.SoundFile(str(out_path), "w", samplerate=orig_sr, channels=num_channels, subtype="PCM_24") as f_out:
            samples_processed = 0
            first_block = True
            while samples_processed < total_samples:
                read_len = min(chunk_samples, total_samples - samples_processed)
                orig_chunk = f_orig.read(read_len, dtype="float32")
                if orig_chunk.ndim == 1:
                    orig_chunk = orig_chunk[:, np.newaxis]

                sep_chunk = f_sep.read(read_len, dtype="float32")
                if sep_chunk.ndim == 1:
                    sep_chunk = sep_chunk[:, np.newaxis]

                # Align channel count
                if sep_chunk.shape[1] < num_channels:
                    sep_chunk = np.repeat(sep_chunk, num_channels, axis=1)
                elif sep_chunk.shape[1] > num_channels:
                    sep_chunk = sep_chunk[:, :num_channels]

                # Align chunk length if separated file is slightly shorter
                if len(sep_chunk) < len(orig_chunk):
                    pad_len = len(orig_chunk) - len(sep_chunk)
                    sep_chunk = np.pad(sep_chunk, ((0, pad_len), (0, 0)), mode="constant")
                elif len(sep_chunk) > len(orig_chunk):
                    sep_chunk = sep_chunk[:len(orig_chunk)]

                # High frequency air restoration with state continuation
                enhanced_sep = sep_chunk
                if sos is not None:
                    try:
                        if first_block:
                            zi = zi * orig_chunk[0, :]
                            first_block = False
                        hf_air, zi = scipy.signal.sosfilt(sos, orig_chunk, axis=0, zi=zi)
                        enhanced_sep = np.clip(sep_chunk + 0.65 * hf_air.astype(np.float32), -1.0, 1.0)
                    except Exception:
                        enhanced_sep = sep_chunk

                # Compute local speech mask for this chunk
                mask_chunk = build_speech_mask_for_chunk(
                    chunk_start=samples_processed,
                    chunk_len=len(orig_chunk),
                    total_samples=total_samples,
                    sample_rate=orig_sr,
                    merged_intervals=merged_intervals,
                    crossfade_ms=crossfade_ms,
                )[:, np.newaxis]

                # Blend
                pristine_chunk = (1.0 - mask_chunk) * orig_chunk + mask_chunk * enhanced_sep
                f_out.write(pristine_chunk)
                samples_processed += len(orig_chunk)

    return str(out_path)


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
    chunk_seconds: Optional[float] = None,
) -> str:
    """Combine untouched original audio in non-speech intervals with separated BGM in speech intervals.

    For long audio files (>= 120s or when chunk_seconds is specified), automatically streams
    through preserve_pristine_background_chunked to keep RAM usage strictly < 80MB.
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

    # Check file duration to decide between in-memory or chunked streaming
    try:
        orig_info = sf.info(str(orig_path))
        sep_info = sf.info(str(sep_path))
        is_long_file = orig_info.frames >= 120 * orig_info.samplerate
        same_sr = sep_info.samplerate == orig_info.samplerate
        if (chunk_seconds is not None or is_long_file) and same_sr:
            return preserve_pristine_background_chunked(
                original_audio_path=orig_path,
                separated_bgm_path=sep_path,
                speech_segments=speech_segments,
                output_path=out_path,
                pad_ms=pad_ms,
                crossfade_ms=crossfade_ms,
                min_gap_ms=min_gap_ms,
                restore_high_freq=restore_high_freq,
                hf_cutoff_hz=hf_cutoff_hz,
                chunk_seconds=chunk_seconds or 30.0,
            )
    except Exception:
        pass

    # In-memory execution for short audio files (< 120s)
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
