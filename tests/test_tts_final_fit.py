"""Run the real final TTS orchestration without providers or model imports."""
import ast
import asyncio
from datetime import timedelta
import logging
import os
from pathlib import Path
import re
import struct
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock
import wave

import pytest
from pydub import AudioSegment

from backend import v1_speech_guard as guard
from backend.ai import v1_translation_meaning


def write_wave(path, duration):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(struct.pack("<h", 3000) * round(duration * 16000))


def measured_duration(path):
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / audio.getframerate()
    except wave.Error:
        return len(AudioSegment.from_file(path)) / 1000.


@pytest.fixture
def run_tts(tmp_path, monkeypatch):
    """Compile only the production orchestration; replace its service adapters."""
    source = Path(__file__).resolve().parents[1] / "backend/ai/voice_cloning.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
    function = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "generate_dubbing_audio")
    function.decorator_list = []
    cache_write = Mock()
    translation = NS(_contains_cjk=lambda _: False,
                     translate_subtitles=Mock(side_effect=AssertionError("Unexpected translation")),
                     condense_vietnamese_subtitles_batch=Mock(return_value={}))
    fake_cache = NS(voice_cache_key=lambda *args, **kwargs: "fixture-key",
                    read_voice_cache=lambda *args, **kwargs: None,
                    write_voice_cache=cache_write)
    fake_guard = NS(coalesce_tiny_same_voice_cues=guard.coalesce_tiny_same_voice_cues,
                    SpeechTimingError=guard.SpeechTimingError,
                    vietnamese_spoken_seconds=guard.vietnamese_spoken_seconds,
                    load_audio=lambda path: AudioSegment.from_file(path),
                    validate_speech_timeline=lambda dubs, **kwargs: guard.validate_speech_timeline(
                        dubs, measure=measured_duration, **kwargs))
    monkeypatch.setitem(sys.modules, "_tts_final_fit.ai.v1_voice_cache", fake_cache)
    monkeypatch.setitem(sys.modules, "_tts_final_fit.ai.translation", translation)
    monkeypatch.setitem(sys.modules, "_tts_final_fit.ai.v1_translation_meaning", v1_translation_meaning)
    monkeypatch.setitem(sys.modules, "v1_speech_guard", fake_guard)
    monkeypatch.setitem(sys.modules, "shared_state", NS(stop_requested=False))
    monkeypatch.setitem(sys.modules, "soundfile", NS(
        info=lambda path: NS(duration=measured_duration(path))))
    monkeypatch.setitem(sys.modules, "batch_control", NS(
        run=Mock(side_effect=RuntimeError("Emergency FFmpeg must not run"))))
    namespace = dict(__name__="_tts_final_fit.ai.voice_cloning", __package__="_tts_final_fit.ai",
                     asyncio=asyncio, os=os, re=re, logger=logging.getLogger(__name__),
                     MAX_NATURAL_SPEED=1.5, TTSIncompleteError=RuntimeError,
                     validate_tts_source=lambda _: None,
                     trim_audio_silence=lambda audio, **kwargs: audio,
                     build_atempo_filter=lambda _: "anull", print=lambda *args, **kwargs: None)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

    def run(actual_duration, reported_duration, *, speed=1.5, fitted_duration=None,
            fitted_metadata=None, condensation_error=None, text="xin chào", window=.95):
        path = tmp_path / "1.mp3"
        write_wave(path, actual_duration)
        original = path.read_bytes()
        cue = NS(index=1, start=timedelta(0), end=timedelta(seconds=window), content=text,
                 voice_source="fixture", voice_param="female", gender="female")

        async def generate(*args, **kwargs):
            return dict(index=1, path=str(path), start=0., end=reported_duration,
                        actual_audio_duration=reported_duration, speed_ratio=speed,
                        content=cue.content)

        async def fit(*args, **kwargs):
            if fitted_duration is not None:
                write_wave(path, fitted_duration)
            duration = fitted_metadata if fitted_metadata is not None else measured_duration(path)
            applied_speed = 1.2 if fitted_duration is not None else 1.0
            return str(path), duration, applied_speed

        if condensation_error is not None:
            def fail_condensation(items, *, failure_causes, **_kwargs):
                failure_causes.update((item["index"], condensation_error) for item in items)
                return {}

            translation.condense_vietnamese_subtitles_batch.side_effect = fail_condensation

        fits = Mock(wraps=fit)
        namespace["generate_single_tts"] = generate
        namespace["fit_audio_file"] = fits
        namespace["calculate_reading_windows"] = lambda segments, **kwargs: {s.index: window for s in segments}
        return NS(path=path, original=original, cue=cue, fits=fits, cache_write=cache_write,
                  condense=translation.condense_vietnamese_subtitles_batch,
                  invoke=lambda: asyncio.run(namespace["generate_dubbing_audio"](
                      [cue], str(tmp_path), voice_source="fixture", video_duration=window + .05)))
    return run


def test_unfitted_tts_preserves_complete_wave_instead_of_clamping_tail(run_tts):
    job = run_tts(1.2, 1.2)

    with pytest.raises(guard.SpeechTimingError):
        job.invoke()

    assert job.path.read_bytes() == job.original
    assert measured_duration(job.path) == 1.2
    job.cache_write.assert_not_called()


def test_duration_metadata_cannot_hide_real_overrun(run_tts):
    job = run_tts(1.2, .5)

    with pytest.raises(guard.SpeechTimingError):
        job.invoke()

    assert job.path.read_bytes() == job.original
    assert job.cue.content == "xin chào"


def test_unfitted_speech_is_retryable_when_condensation_timed_out(run_tts):
    from backend.v1_retry_policy import is_transient_network_error

    timeout = TimeoutError("Temporary provider timeout")
    job = run_tts(1.2, 1.2, condensation_error=timeout)
    with pytest.raises(RuntimeError, match="temporary condensation failure") as caught:
        job.invoke()
    assert caught.value.__cause__ is timeout
    assert is_transient_network_error(caught.value)
    assert job.path.read_bytes() == job.original


def test_bounded_fit_that_really_fits_keeps_full_result(run_tts):
    job = run_tts(1.1, 1.1, speed=1., fitted_duration=.9)

    result = job.invoke()

    assert len(result) == 1
    assert result[0]["actual_audio_duration"] == pytest.approx(measured_duration(job.path))
    assert result[0]["actual_audio_duration"] == .9
    assert result[0]["speed_ratio"] == pytest.approx(1.2)
    assert job.cue.content == "xin chào"
    assert job.fits.call_count == 1
    assert job.fits.call_args.kwargs["max_speed"] <= 1.5


def test_fitter_metadata_cannot_hide_unchanged_audio(run_tts):
    job = run_tts(1.2, 1.2, speed=1., fitted_metadata=.9)

    with pytest.raises(guard.SpeechTimingError):
        job.invoke()

    assert job.path.read_bytes() == job.original


@pytest.mark.parametrize("speed, targets", [(1.0, [18, 16]), (1.5, [12, 10])])
def test_measured_rewrite_budget_uses_available_speed(run_tts, speed, targets):
    text = " ".join(["mẫu"] * 25)
    job = run_tts(5.088, 5.088, text=text, window=3.07, speed=speed)

    with pytest.raises(guard.SpeechTimingError):
        job.invoke()

    measured_rounds = {"round2_post_tts", "round3_final_tts"}
    assert [call.args[0][0]["target_words"] for call in job.condense.call_args_list
            if call.kwargs["job_id"] in measured_rounds] == targets
    assert job.path.read_bytes() == job.original
