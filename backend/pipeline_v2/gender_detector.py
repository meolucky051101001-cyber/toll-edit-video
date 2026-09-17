"""Speaker gender detection and multi-speaker grouping using acoustic F0 pitch estimation.

NOTE: This module uses an acoustic fundamental frequency (F0) heuristic based on
librosa.yin pitch tracking rather than a deep neural diarization model (such as PyAnnote).
Heuristic design:
- Segments with median F0 < 190.0 Hz are classified as male, >= 190.0 Hz as female.
- Male sub-clustering: < 130.0 Hz -> SPEAKER_MALE_0 (deep), >= 130.0 Hz -> SPEAKER_MALE_1.
- Female sub-clustering: < 230.0 Hz -> SPEAKER_FEMALE_0, >= 230.0 Hz -> SPEAKER_FEMALE_1.
- Provides extremely fast, lightweight execution with zero extra model weights and low RAM/VRAM.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional, Sequence
import numpy as np

logger = logging.getLogger(__name__)

def detect_segment_gender(
    vocals_path: str | Path,
    start_sec: float,
    end_sec: float,
    fallback_gender: str = "female",
) -> str:
    """Classify speaker gender for an audio segment based on fundamental pitch (F0).
    
    Male human pitch range: ~85 Hz to 160 Hz (mean ~120 Hz).
    Female human pitch range: ~165 Hz to 260 Hz (mean ~210 Hz).
    Boundary threshold: 165.0 Hz.
    """
    try:
        import librosa
        
        path_str = str(vocals_path)
        if not Path(path_str).exists():
            return fallback_gender

        duration = max(float(end_sec) - float(start_sec), 0.15)
        # Load small segment slice at 16kHz
        y, sr = librosa.load(path_str, sr=16000, offset=max(0.0, float(start_sec)), duration=duration)
        if len(y) < sr * 0.1:
            return fallback_gender
            
        # librosa.yin with fmin=65, fmax=350
        f0 = librosa.yin(y, fmin=65, fmax=350, sr=sr)
        valid_f0 = f0[(f0 >= 70) & (f0 <= 340)]
        if len(valid_f0) == 0:
            return fallback_gender
            
        median_f0 = float(np.median(valid_f0))
        # Pitch < 190 Hz is male (confident young male & adult male), >= 190 Hz is female
        return "male" if median_f0 < 190.0 else "female"
    except Exception as exc:
        logger.debug("Gender detection fallback for %.2f-%.2f: %s", start_sec, end_sec, exc)
        return fallback_gender


def detect_segment_pitch(
    vocals_path: str | Path,
    start_sec: float,
    end_sec: float,
) -> Optional[float]:
    """Return median fundamental frequency (F0 in Hz) for an audio slice, or None if unvoiced/silent."""
    try:
        import librosa

        path_str = str(vocals_path)
        if not Path(path_str).exists():
            return None

        duration = max(float(end_sec) - float(start_sec), 0.15)
        y, sr = librosa.load(
            path_str, sr=16000, offset=max(0.0, float(start_sec)), duration=duration
        )
        if len(y) < sr * 0.1:
            return None

        f0 = librosa.yin(y, fmin=65, fmax=350, sr=sr)
        valid_f0 = f0[(f0 >= 70) & (f0 <= 340)]
        if len(valid_f0) == 0:
            return None

        return float(np.median(valid_f0))
    except Exception as exc:
        logger.debug("Pitch detection fallback for %.2f-%.2f: %s", start_sec, end_sec, exc)
        return None


def enrich_segments_with_speaker_and_gender(
    segments: Sequence[Any],
    vocals_path: str | Path,
    default_gender: str = "female",
) -> List[Any]:
    """Enrich subtitle runtime segments with detected speaker IDs and gender."""
    import concurrent.futures

    path_obj = Path(vocals_path)
    if not path_obj.exists():
        for seg in segments:
            if not getattr(seg, "gender", None):
                seg.gender = default_gender
            if not getattr(seg, "speaker_id", None):
                seg.speaker_id = f"speaker_{seg.gender}"
        return list(segments)

    def _detect_one(seg: Any):
        start_s = (
            seg.start.total_seconds()
            if hasattr(seg.start, "total_seconds")
            else float(seg.start)
        )
        end_s = (
            seg.end.total_seconds()
            if hasattr(seg.end, "total_seconds")
            else float(seg.end)
        )
        pitch = detect_segment_pitch(path_obj, start_s, end_s)
        gender = "male" if (pitch is not None and pitch < 190.0) else default_gender
        return pitch, gender

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(_detect_one, segments))

    for seg, (pitch, g) in zip(segments, results):
        if not getattr(seg, "gender", None) or seg.gender == default_gender:
            seg.gender = g
        if not getattr(seg, "speaker_id", None):
            if g == "male":
                seg.speaker_id = (
                    "SPEAKER_MALE_0"
                    if (pitch is not None and pitch < 130.0)
                    else "SPEAKER_MALE_1"
                )
            else:
                seg.speaker_id = (
                    "SPEAKER_FEMALE_0"
                    if (pitch is not None and pitch < 230.0)
                    else "SPEAKER_FEMALE_1"
                )

    male_count = sum(1 for seg in segments if getattr(seg, "gender", None) == "male")
    female_count = sum(1 for seg in segments if getattr(seg, "gender", None) == "female")
    logger.info(
        "Speaker & gender detection complete: %d male, %d female segments",
        male_count,
        female_count,
    )
    return list(segments)


def enrich_segments_with_gender(
    segments: Sequence[Any],
    vocals_path: str | Path,
    default_gender: str = "female",
) -> List[Any]:
    return enrich_segments_with_speaker_and_gender(
        segments, vocals_path, default_gender=default_gender
    )
