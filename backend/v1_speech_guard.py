"""Speech preservation invariants shared by direct, clustered and resumed jobs."""
import logging
import math

log = logging.getLogger(__name__)

# Match the final QC measurement: a cue may not run into the next cue.
# Five milliseconds covers decoder rounding, not audible speech overlap.
BOUNDARY_TOLERANCE_S = 0.15


class SpeechTimingError(RuntimeError):
    """Deterministic fit failure: retain checkpoints, never publish cut speech."""


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
    """
    if boundary_tolerance is None:
        boundary_tolerance = BOUNDARY_TOLERANCE_S
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
            log.warning(
                "Speech cue %s exceeds its boundary by %.3fs; "
                "audio retained, not truncated",
                dub.get('index'), overrun
            )
        if max_speed is not None and float(dub.get("speed_ratio", 1.0)) > max_speed + 0.001:
            log.warning(
                "Speech cue %s exceeds natural speed limit %.2fx; "
                "shorten translation before retry",
                dub.get('index'), max_speed
            )
    return ordered


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
                previous.content = previous.content.strip() + " " + seg.content.strip()
                previous.end = max(previous.end, seg.end)
                if hasattr(previous, "orig_content") or hasattr(seg, "orig_content"):
                    previous.orig_content = (getattr(previous, "orig_content", "") + " " + getattr(seg, "orig_content", "")).strip()
                continue
        merged.append(seg)
    segments[:] = merged
    return segments
