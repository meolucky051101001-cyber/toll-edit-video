"""Speaker gender detection and multi-speaker grouping using acoustic F0 pitch estimation.

Upgraded with:
1. Parselmouth (Praat gold-standard acoustic F0 analysis) with librosa fallback.
2. Global video dominant pitch baseline (overall video F0 median) to distinguish monologue vs dialogue.
3. Accurate phonetic threshold: 165.0 Hz (human male mean ~115 Hz, human female mean ~210 Hz).
4. Hysteresis margin:
   - In predominantly female video: segment requires pitch < 145 Hz to qualify as male.
   - In predominantly male video: segment requires pitch > 185 Hz to qualify as female.
5. Temporal smoothing / hangover filter: eliminates single-segment jitter (e.g. Female -> Male (1 seg) -> Female).
6. Monologue lock: if one gender accounts for >= 80% of segments and there is no sustained multi-segment
   dialogue (>= 3 consecutive segments of opposite gender), locks the entire video to the dominant gender.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, List, Optional, Sequence
import numpy as np

logger = logging.getLogger(__name__)


def extract_pitch_series(audio_path: str | Path, start_sec: float, end_sec: float) -> Optional[float]:
    """Returns median fundamental frequency (F0 in Hz) for an audio slice."""
    path_str = str(audio_path)
    if not Path(path_str).exists():
        return None

    duration = max(float(end_sec) - float(start_sec), 0.15)
    offset = max(0.0, float(start_sec))

    # Try Parselmouth first (most accurate, zero subharmonic errors)
    try:
        import parselmouth
        snd = parselmouth.Sound(path_str)
        part = snd.extract_part(from_time=offset, to_time=offset + duration)
        p = part.to_pitch(pitch_floor=75, pitch_ceiling=450)
        v = p.selected_array["frequency"]
        voiced = v[v > 0]
        if len(voiced) >= 5:
            return float(np.median(voiced))
    except Exception as exc:
        logger.debug("Parselmouth extraction failed for %.2f-%.2f: %s", start_sec, end_sec, exc)

    # Fallback to librosa
    try:
        import librosa
        y, sr = librosa.load(path_str, sr=16000, offset=offset, duration=duration)
        if len(y) >= sr * 0.1:
            f0 = librosa.yin(y, fmin=65, fmax=350, sr=sr)
            valid = f0[(f0 >= 75) & (f0 <= 340)]
            if len(valid) >= 5:
                return float(np.median(valid))
    except Exception as exc:
        logger.debug("Librosa extraction fallback for %.2f-%.2f: %s", start_sec, end_sec, exc)

    return None


def detect_global_pitch(audio_path: str | Path) -> tuple[float, str]:
    """Returns (global_median_f0, global_dominant_gender) for the entire audio file."""
    path_str = str(audio_path)
    if not Path(path_str).exists():
        return 200.0, "female"

    try:
        import parselmouth
        snd = parselmouth.Sound(path_str)
        p = snd.to_pitch(pitch_floor=75, pitch_ceiling=450)
        v = p.selected_array["frequency"]
        voiced = v[v > 0]
        if len(voiced) >= 20:
            med = float(np.median(voiced))
            gender = "female" if med > 165.0 else "male"
            return med, gender
    except Exception:
        pass

    try:
        import librosa
        y, sr = librosa.load(path_str, sr=16000)
        f0 = librosa.yin(y, fmin=65, fmax=350, sr=sr)
        valid = f0[(f0 >= 75) & (f0 <= 340)]
        if len(valid) >= 20:
            med = float(np.median(valid))
            gender = "female" if med > 165.0 else "male"
            return med, gender
    except Exception:
        pass

    return 200.0, "female"


def detect_segment_pitch(
    vocals_path: str | Path,
    start_sec: float,
    end_sec: float,
) -> Optional[float]:
    """Return median fundamental frequency (F0 in Hz) for an audio slice, or None if unvoiced/silent."""
    return extract_pitch_series(vocals_path, start_sec, end_sec)


def detect_segment_gender(
    vocals_path: str | Path,
    start_sec: float,
    end_sec: float,
    fallback_gender: str = "female",
) -> str:
    """Classify speaker gender for an audio segment based on fundamental pitch (F0).
    
    Standard boundary threshold: 165.0 Hz.
    """
    pitch = extract_pitch_series(vocals_path, start_sec, end_sec)
    if pitch is None:
        return fallback_gender
    return "female" if pitch > 165.0 else "male"


def enrich_segments_with_speaker_and_gender(
    segments: Sequence[Any],
    vocals_path: str | Path,
    default_gender: str = "female",
) -> List[Any]:
    """Enrich subtitle runtime segments with detected speaker IDs and gender using global consensus and smoothing."""
    import concurrent.futures

    path_obj = Path(vocals_path)
    if not path_obj.exists() or not segments:
        for seg in segments:
            if not getattr(seg, "gender", None):
                seg.gender = default_gender
            if not getattr(seg, "speaker_id", None):
                seg.speaker_id = f"speaker_{seg.gender}"
        return list(segments)

    # 1. Global Dominant Pitch Analysis
    global_median_f0, global_gender = detect_global_pitch(path_obj)
    logger.info(
        "Global audio pitch: %.1f Hz -> Video dominant gender: %s",
        global_median_f0,
        global_gender,
    )

    # 2. Extract pitch for each segment in parallel
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
        return extract_pitch_series(path_obj, start_s, end_s)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        pitches = list(executor.map(_detect_one, segments))

    # 3. First Pass: Hysteresis classification based on global baseline
    raw_genders = []
    for pitch in pitches:
        if pitch is None:
            raw_genders.append(global_gender)
        else:
            if global_gender == "female":
                # In predominantly female video, pitch must be distinctly male (< 145 Hz) to switch
                raw_genders.append("male" if pitch < 145.0 else "female")
            else:
                # In predominantly male video, pitch must be distinctly female (> 185 Hz) to switch
                raw_genders.append("female" if pitch > 185.0 else "male")

    # 4. Second Pass: Temporal Smoothing (Hangover filter)
    # Remove isolated 1-segment flips (e.g. Female -> Male (1 seg) -> Female)
    smoothed_genders = list(raw_genders)
    n = len(smoothed_genders)
    for i in range(1, n - 1):
        prev_g = smoothed_genders[i - 1]
        curr_g = smoothed_genders[i]
        next_g = raw_genders[i + 1]
        if curr_g != prev_g and prev_g == next_g:
            smoothed_genders[i] = prev_g

    # 5. Third Pass: Monologue consensus
    # If one gender dominates >= 80% and there is no sustained multi-segment dialogue
    # (i.e. fewer than 3 consecutive segments of the opposite gender), lock all segments to dominant gender.
    f_count = sum(1 for g in smoothed_genders if g == "female")
    m_count = sum(1 for g in smoothed_genders if g == "male")
    dom_gender = "female" if f_count >= m_count else "male"
    dom_ratio = max(f_count, m_count) / n if n > 0 else 1.0

    minor_gender = "male" if dom_gender == "female" else "female"
    max_consecutive_minor = 0
    curr_consecutive = 0
    for g in smoothed_genders:
        if g == minor_gender:
            curr_consecutive += 1
            max_consecutive_minor = max(max_consecutive_minor, curr_consecutive)
        else:
            curr_consecutive = 0

    if dom_ratio >= 0.80 and max_consecutive_minor < 3:
        logger.info(
            "Single-speaker monologue detected (dominance %.1f%%, max consecutive minor %d). Locking all segments to %s.",
            dom_ratio * 100,
            max_consecutive_minor,
            dom_gender,
        )
        smoothed_genders = [dom_gender] * n

    # 6. Assign final gender and speaker_id to segments
    for seg, pitch, g in zip(segments, pitches, smoothed_genders):
        seg.gender = g
        if not getattr(seg, "speaker_id", None):
            if g == "male":
                seg.speaker_id = (
                    "SPEAKER_MALE_0"
                    if (pitch is not None and pitch < 125.0)
                    else "SPEAKER_MALE_1"
                )
            else:
                seg.speaker_id = (
                    "SPEAKER_FEMALE_0"
                    if (pitch is not None and pitch < 240.0)
                    else "SPEAKER_FEMALE_1"
                )

    male_final = sum(1 for g in smoothed_genders if g == "male")
    female_final = sum(1 for g in smoothed_genders if g == "female")
    logger.info(
        "Speaker & gender detection complete: %d male, %d female segments (baseline: %s)",
        male_final,
        female_final,
        dom_gender,
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
