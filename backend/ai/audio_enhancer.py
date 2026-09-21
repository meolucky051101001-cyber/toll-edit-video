"""Audio enhancement engine for AutoDub / Tool V2.

Provides Selective Timeline Splicing to preserve 100% untouched original audio
during non-speech intervals (music interludes, foley, effects, pauses),
combined with High-Frequency Air Restoration (>14kHz) and smooth cosine crossfading.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

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
    """Process bounded blocks, retaining filter state across block boundaries."""
    orig_path = Path(original_audio_path)
    sep_path = Path(separated_bgm_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not orig_path.is_file():
        raise FileNotFoundError(f"Original audio file not found: {orig_path}")

    if not sep_path.is_file() or not speech_segments:
        shutil.copy2(str(orig_path), str(out_path))
        return str(out_path)

    if out_path.resolve() in (orig_path.resolve(), sep_path.resolve()):
        raise ValueError("Enhanced output must differ from both input files")
    if not math.isfinite(chunk_seconds) or chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be finite and positive")
    original_info, separated_info = sf.info(str(orig_path)), sf.info(str(sep_path))
    if separated_info.samplerate != original_info.samplerate:
        # Resample on disk: do not decode a long recording into a NumPy array.
        with tempfile.TemporaryDirectory(prefix="resample-", dir=out_path.parent) as work:
            normalized = Path(work) / "background.wav"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(sep_path),
                 "-ar", str(original_info.samplerate),
                 "-c:a", "pcm_f32le", str(normalized)],
                check=True, capture_output=True, timeout=1800,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return preserve_pristine_background_chunked(
                orig_path, normalized, speech_segments, out_path,
                pad_ms, crossfade_ms, min_gap_ms, restore_high_freq,
                hf_cutoff_hz, chunk_seconds,
            )

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
                zi = np.zeros((len(sos), 2, num_channels), dtype=np.float64)

        chunk_samples = max(1024, int(round(chunk_seconds * orig_sr)))
        with sf.SoundFile(str(out_path), "w", samplerate=orig_sr, channels=num_channels, subtype="PCM_24") as f_out:
            samples_processed = 0
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
                    sep_chunk = np.tile(sep_chunk, (1, math.ceil(num_channels / sep_chunk.shape[1])))[:, :num_channels]
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

    Uses the same bounded streaming implementation for short and long files.
    A failed enhancement never falls back to loading the entire file into RAM.
    Returns the path to the resulting enhanced pristine audio file.
    """
    # Publish only a complete result; failures leave an existing output intact.
    orig_path, sep_path, out_path = map(Path, (
        original_audio_path, separated_bgm_path, output_path))
    if out_path.resolve() in (orig_path.resolve(), sep_path.resolve()):
        raise ValueError("Enhanced output must differ from both input files")
    if not orig_path.is_file():
        raise FileNotFoundError(f"Original audio file not found: {orig_path}")
    block_seconds = 30.0 if chunk_seconds is None else float(chunk_seconds)
    if not math.isfinite(block_seconds) or block_seconds <= 0:
        raise ValueError("chunk_seconds must be finite and positive")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="enhance-", dir=out_path.parent) as work:
        staged = Path(work) / "output.wav"
        preserve_pristine_background_chunked(
            orig_path, sep_path, speech_segments, staged,
            pad_ms=pad_ms, crossfade_ms=crossfade_ms, min_gap_ms=min_gap_ms,
            restore_high_freq=restore_high_freq, hf_cutoff_hz=hf_cutoff_hz,
            chunk_seconds=block_seconds,
        )
        os.replace(staged, out_path)
    return str(out_path)
