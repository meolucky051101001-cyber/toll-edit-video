"""Speaker gender detection using fundamental frequency (F0) pitch estimation."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Sequence
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


def enrich_segments_with_gender(
    segments: Sequence[Any],
    vocals_path: str | Path,
    default_gender: str = "female",
) -> List[Any]:
    """Enrich subtitle runtime segments with detected speaker gender in parallel."""
    import concurrent.futures
    
    path_obj = Path(vocals_path)
    if not path_obj.exists():
        for seg in segments:
            if not getattr(seg, "gender", None):
                seg.gender = default_gender
        return list(segments)
        
    def _detect_one(seg: Any) -> str:
        start_s = seg.start.total_seconds() if hasattr(seg.start, "total_seconds") else float(seg.start)
        end_s = seg.end.total_seconds() if hasattr(seg.end, "total_seconds") else float(seg.end)
        return detect_segment_gender(path_obj, start_s, end_s, fallback_gender=default_gender)
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        genders = list(executor.map(_detect_one, segments))
        
    for seg, g in zip(segments, genders):
        seg.gender = g
        
    male_count = sum(1 for g in genders if g == "male")
    female_count = sum(1 for g in genders if g == "female")
    logger.info("Gender detection complete: %d male, %d female segments", male_count, female_count)
    return list(segments)
