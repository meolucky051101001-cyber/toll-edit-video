"""Timeline validation rejects unfitted speech without rewriting cached audio."""
from copy import deepcopy
import math
import struct
import wave

import pytest
from pydub import AudioSegment

from backend.v1_speech_guard import (
    BOUNDARY_TOLERANCE_S,
    SpeechTimingError,
    validate_speech_timeline,
)


def pcm_wave(path, duration, rate=16000):
    # A real non-silent tail makes accidental truncation observable in bytes.
    samples = [round(6000 * math.sin(2 * math.pi * 270 * index / rate))
               for index in range(round(duration * rate))]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def wave_duration(path):
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


@pytest.mark.parametrize("boundary_kind", ["next_cue", "video_end"])
def test_overrun_rejects_without_exporting_or_changing_real_audio(tmp_path, monkeypatch,
                                                               boundary_kind):
    audio = pcm_wave(tmp_path / "complete-speech.wav", 1.2)
    original_bytes = audio.read_bytes()
    dubs = [dict(index=17, path=str(audio), start=0., duration=.05, speed_ratio=1.)]
    total_duration = 1.
    if boundary_kind == "next_cue":
        following = pcm_wave(tmp_path / "following.wav", .2)
        dubs.append(dict(index=18, path=str(following), start=1., duration=.2))
        total_duration = 2.
    original_dubs = deepcopy(dubs)
    exports = []
    original_export = AudioSegment.export

    def record_export(self, *args, **kwargs):
        exports.append((args, kwargs))
        return original_export(self, *args, **kwargs)

    monkeypatch.setattr(AudioSegment, "export", record_export)
    error = None
    try:
        validate_speech_timeline(dubs, total_duration=total_duration, measure=wave_duration)
    except SpeechTimingError as caught:
        error = caught

    assert audio.read_bytes() == original_bytes, "Validation overwrote complete source speech"
    assert exports == [], "Validation attempted to export a trimmed or faded audio file"
    assert error is not None, "An unfitted speech cue must fail before mixing"
    assert "17" in str(error) and "boundary" in str(error)
    assert dubs == original_dubs


@pytest.mark.parametrize("boundary_kind", ["next_cue", "video_end"])
def test_configured_boundary_tolerance_is_the_only_overrun_allowance(monkeypatch,
                                                                  boundary_kind):
    # No media decoding or export belongs in this validation path, even when
    # the caller supplies a more precise duration measurement and tolerance.
    reads, exports = [], []
    monkeypatch.setattr(AudioSegment, "from_file", lambda *args, **kwargs: reads.append(args))
    monkeypatch.setattr(AudioSegment, "export", lambda *args, **kwargs: exports.append(args))
    dubs = [dict(index=1, path="first", start=0., duration=.1)]
    total_duration = 1.
    if boundary_kind == "next_cue":
        dubs.append(dict(index=2, path="second", start=1.))
        total_duration = 2.

    accepted = validate_speech_timeline(
        dubs, total_duration=total_duration, boundary_tolerance=.005,
        measure=lambda path: 1.005 if path == "first" else .2)
    assert accepted[0]["duration"] == 1.005
    with pytest.raises(SpeechTimingError, match="boundary"):
        validate_speech_timeline(
            dubs, total_duration=total_duration, boundary_tolerance=.005,
            measure=lambda path: 1.006 if path == "first" else .2)
    assert reads == [] and exports == []


def test_default_rounding_tolerance_retains_measured_duration_and_input_metadata():
    dubs = [dict(index=1, path="first", start=0., duration=.1)]
    original = deepcopy(dubs)
    measured_duration = 1. + BOUNDARY_TOLERANCE_S

    accepted = validate_speech_timeline(
        dubs, total_duration=1., measure=lambda _: measured_duration)

    assert accepted[0]["duration"] == measured_duration
    assert dubs == original
