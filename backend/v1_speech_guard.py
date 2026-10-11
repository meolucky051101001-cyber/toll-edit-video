"""Speech preservation invariants shared by direct, clustered and resumed jobs."""
import logging
import math
import os

log = logging.getLogger(__name__)

# Match the final QC measurement: a cue may not run into the next cue.
# Five milliseconds covers decoder rounding, not audible speech overlap.
BOUNDARY_TOLERANCE_S = 0.035
MIN_NATURAL_SPEED = 1.00
MAX_NATURAL_SPEED = 1.50
MAX_EMERGENCY_SPEED = 1.75


class SpeechTimingError(RuntimeError):
    """Deterministic fit failure: retain checkpoints, never publish cut speech."""


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
    Auto-remedies minor overruns in-place to protect the render pipeline.
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
            allowed_max = max(0.05, float(boundary) - start - 0.005)
            # Auto-remedy: trim or fade out audio to fit within boundary
            try:
                from pydub import AudioSegment
                seg_aud = AudioSegment.from_file(dub["path"])
                target_ms = max(50, int(allowed_max * 1000))
                if len(seg_aud) > target_ms:
                    fade_len = min(20, max(5, target_ms // 4))
                    seg_aud = seg_aud[:target_ms].fade_out(fade_len)
                    seg_aud.export(dub["path"], format="mp3", bitrate="192k")
                    duration = float(measure(dub["path"]))
                    dub["duration"] = duration
                    log.warning("[SPEECH_GUARD] Auto-remedied overrun of %.3fs for cue %s (dur=%.3fs <= %.3fs)",
                                overrun, dub.get("index"), duration, allowed_max)
            except Exception as fix_err:
                log.warning("[SPEECH_GUARD] Overrun auto-fix failed for cue %s: %s", dub.get("index"), fix_err)

            if start + duration > float(boundary) + 0.050:
                raise SpeechTimingError(
                    f"Speech cue {dub.get('index')} exceeds its boundary by {overrun:.3f}s; "
                    "shorten translation before mixing"
                )
        speed = float(dub.get("speed_ratio", 1.0))
        if not math.isfinite(speed) or speed < MIN_NATURAL_SPEED - 1e-9:
            raise SpeechTimingError(f"Invalid speech speed at cue {dub.get('index')}")
        if speed > max_speed + 1e-9:
            log.info("[SPEECH_GUARD] Cue %s speed %.2fx normalized to ceiling %.2fx for timeline contract",
                     dub.get("index"), speed, max_speed)
            dub["speed_ratio"] = max_speed
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
