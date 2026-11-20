"""Speech preservation invariants shared by direct, clustered and resumed jobs."""
import copy
import logging
import math
import os

log = logging.getLogger(__name__)

# Match the final QC measurement: a cue may not run into the next cue.
# Allow 35 milliseconds for decoder rounding in the shared QC contract.
BOUNDARY_TOLERANCE_S = 0.035
MIN_NATURAL_SPEED = 1.00
MAX_NATURAL_SPEED = 1.50
MAX_EMERGENCY_SPEED = 1.75
SPEECH_GAP_S = 0.05
VI_SECONDS_PER_WORD = 0.28


class SpeechTimingError(RuntimeError):
    """Deterministic fit failure: retain checkpoints, never publish cut speech."""


def calculate_reading_windows(segments, video_duration=None, gap_s=SPEECH_GAP_S):
    """Return the hard speech slots for an ordered sequence of subtitle cues."""
    windows = {}
    for position, segment in enumerate(segments):
        start = segment.start.total_seconds()
        if position + 1 < len(segments):
            boundary = segments[position + 1].start.total_seconds()
            if boundary <= start:
                raise SpeechTimingError(f"Duplicate/reversed speech start at cue {segment.index}")
            available = boundary - start - gap_s
        elif video_duration is not None and video_duration > start + gap_s:
            available = video_duration - start - gap_s
        else:
            available = segment.end.total_seconds() - start + 1.0
        windows[segment.index] = max(SPEECH_GAP_S, available)
    return windows


def vietnamese_spoken_seconds(text):
    """Estimate spoken words after the same pronunciation expansion used by TTS."""
    from ai.v1_tech_pronunciation import normalize_text_for_tts

    spoken = normalize_text_for_tts(text)
    return max(len(text.split()), len(spoken.split())) * VI_SECONDS_PER_WORD


def natural_speed_limit(value=None):
    """Resolve the configured ceiling without exceeding Tool V1's speed range."""
    if value is None:
        value = os.getenv("V1_MAX_NATURAL_SPEED", str(MAX_NATURAL_SPEED))
    try:
        speed = float(value)
    except (TypeError, ValueError) as exc:
        raise SpeechTimingError("Speech speed limit must be between 1.00x and 1.75x") from exc
    if not math.isfinite(speed) or speed < MIN_NATURAL_SPEED:
        raise SpeechTimingError("Speech speed limit must be between 1.00x and 1.75x")
    return min(speed, MAX_EMERGENCY_SPEED)


def load_audio(path):
    """Decode PCM/MP3 in-process; avoid one ffprobe/ffmpeg spawn per short cue."""
    import soundfile as sf
    from pydub import AudioSegment
    try:
        samples, rate = sf.read(str(path), dtype="int16", always_2d=True)
        return AudioSegment(samples.astype("<i2", copy=False).tobytes(), sample_width=2,
                            frame_rate=rate, channels=samples.shape[1])
    except (sf.LibsndfileError, RuntimeError):
        # Rare formats unsupported by libsndfile retain the existing decoder.
        return AudioSegment.from_file(str(path))


def validate_speech_timeline(dubs, total_duration=None, measure=None, max_speed=None,
                             boundary_tolerance=None):
    """Validate speech cue durations against their time boundaries.

    Reject audio whose measured duration crosses the next cue beyond the
    decoder-rounding allowance. The mixer and the final QC use this contract.
    Keep audio artifacts intact so a failed fit can be regenerated for retry.
    """
    max_speed = natural_speed_limit(max_speed)
    if boundary_tolerance is None:
        boundary_tolerance = BOUNDARY_TOLERANCE_S
    if not math.isfinite(boundary_tolerance) or boundary_tolerance < 0:
        raise SpeechTimingError("Invalid speech boundary tolerance")
    if total_duration is not None and (not math.isfinite(float(total_duration)) or total_duration <= 0):
        raise SpeechTimingError("Invalid total speech timeline duration")
    if measure is None:
        import soundfile as sf
        measure = lambda path: sf.info(str(path)).duration
    ordered = sorted((dict(d) for d in dubs if d), key=lambda d: float(d.get("start", 0)))
    for i, dub in enumerate(ordered):
        start = float(dub.get("start", 0))
        duration = float(measure(dub["path"]))
        if not math.isfinite(start) or start < 0 or not math.isfinite(duration) or duration <= 0:
            raise SpeechTimingError(f"Invalid speech timeline at cue {dub.get('index')}")
        dub["duration"] = duration
        boundary = float(ordered[i + 1]["start"]) if i + 1 < len(ordered) else total_duration
        if boundary is not None and start + duration > float(boundary) + boundary_tolerance:
            overrun = start + duration - float(boundary)
            raise SpeechTimingError(
                f"Speech cue {dub.get('index')} exceeds its boundary by {overrun:.3f}s; "
                "shorten translation before mixing"
            )
        speed = float(dub.get("speed_ratio", 1.0))
        if not math.isfinite(speed) or speed < MIN_NATURAL_SPEED - 1e-9 or speed > MAX_EMERGENCY_SPEED + 1e-9:
            raise SpeechTimingError(f"Invalid speech speed at cue {dub.get('index')}")
        if speed > max_speed + 1e-9:
            log.info("[SPEECH_GUARD] Cue %s speed %.2fx normalized to ceiling %.2fx for timeline contract",
                     dub.get("index"), speed, max_speed)
            dub["speed_ratio"] = max_speed
    return ordered


def _retain_source_caption_geometry(previous, segment):
    """Join source observations without fitting their bounds to the speech cue."""
    sources = (previous, segment)
    if not any(hasattr(source, field) for source in sources for field in
               ("tracking_blocks", "best_block", "ocr_mask_status", "geometry_verified")):
        return
    tracks, best = [], None
    statuses = [getattr(source, "ocr_mask_status", None) for source in sources]
    verified = all(getattr(source, "geometry_verified", False) for source in sources)
    explicitly_empty = []
    for source, status in zip(sources, statuses):
        blocks = getattr(source, "tracking_blocks", None) or []
        # Match the source-cover contract: verified emptiness and a negative
        # caption observation must never revive a stale best_block.
        empty = status == "no_caption_evidence" or (
            getattr(source, "geometry_verified", False) and not blocks)
        explicitly_empty.append(empty)
        if empty:
            continue
        source_best = getattr(source, "best_block", None)
        if not blocks and source_best is not None:
            blocks = [source_best]
        if blocks:
            tracks.extend(copy.deepcopy(blocks))
            if best is None:
                best = copy.deepcopy(source_best if source_best is not None else blocks[0])
    previous.tracking_blocks = tracks
    previous.best_block = best
    previous.geometry_verified = verified
    if "unresolved" in statuses:
        previous.ocr_mask_status = "unresolved"
    elif tracks:
        previous.ocr_mask_status = "located"
    elif all(explicitly_empty):
        previous.ocr_mask_status = "no_caption_evidence"
    for field, bound in (("visual_start_sec", min), ("visual_end_sec", max)):
        if any(hasattr(source, field) for source in sources):
            fallback = "start" if field == "visual_start_sec" else "end"
            setattr(previous, field, bound(float(getattr(source, field,
                getattr(source, fallback).total_seconds())) for source in sources))
    if any(getattr(source, "visual_sub_to_speech", False) for source in sources):
        previous.visual_sub_to_speech = True


def coalesce_tiny_same_voice_cues(segments, minimum_window=0.15):
    """Keep every word; join unutterable adjacent cues only when locked voices agree."""
    ordered = sorted(segments, key=lambda s: s.start)
    if len({s.index for s in ordered}) != len(ordered):
        raise SpeechTimingError("Duplicate subtitle IDs cannot share a speech cache")
    merged = []
    for seg in ordered:
        if merged:
            previous = merged[-1]
            same_voice = (getattr(previous, "voice_source", None), getattr(previous, "voice_param", None)) == (getattr(seg, "voice_source", None), getattr(seg, "voice_param", None))
            same_gender = getattr(previous, "gender", None) == getattr(seg, "gender", None)
            if (seg.start - previous.start).total_seconds() < minimum_window and same_voice and same_gender:
                _retain_source_caption_geometry(previous, seg)
                previous.content = previous.content.strip() + " " + seg.content.strip()
                previous.end = max(previous.end, seg.end)
                if hasattr(previous, "orig_content") or hasattr(seg, "orig_content"):
                    previous.orig_content = (getattr(previous, "orig_content", "") + " " + getattr(seg, "orig_content", "")).strip()
                continue
        merged.append(seg)
    segments[:] = merged
    return segments
