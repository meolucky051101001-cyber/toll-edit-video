"""Last-resort speech truncation tests; no provider or model execution."""
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


class TTSIncompleteError(Exception):
    pass


PER_WORD = 0.5


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
    """Compile only the production orchestration; TTS duration scales with word count."""
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
    monkeypatch.setitem(sys.modules, "_tts_trunc.ai.v1_voice_cache", fake_cache)
    monkeypatch.setitem(sys.modules, "_tts_trunc.ai.translation", translation)
    monkeypatch.setitem(sys.modules, "_tts_trunc.ai.v1_translation_meaning", v1_translation_meaning)
    monkeypatch.setitem(sys.modules, "v1_speech_guard", fake_guard)
    monkeypatch.setitem(sys.modules, "shared_state", NS(stop_requested=False))
    monkeypatch.setitem(sys.modules, "soundfile", NS(
        info=lambda path: NS(duration=measured_duration(path))))
    monkeypatch.setitem(sys.modules, "batch_control", NS(
        run=Mock(side_effect=RuntimeError("Emergency FFmpeg must not run"))))
    namespace = dict(__name__="_tts_trunc.ai.voice_cloning", __package__="_tts_trunc.ai",
                     asyncio=asyncio, os=os, re=re, logger=logging.getLogger("v1_trunc_test"),
                     MAX_NATURAL_SPEED=1.5, TTSIncompleteError=TTSIncompleteError,
                     validate_tts_source=lambda _: None,
                     trim_audio_silence=lambda audio, **kwargs: audio,
                     build_atempo_filter=lambda _: "anull", print=lambda *args, **kwargs: None)
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

    def run(text, *, window=1.5, per_word=PER_WORD, condensation_error=None, can_fit=False):
        path = tmp_path / "1.mp3"
        cue = NS(index=1, start=timedelta(0), end=timedelta(seconds=window), content=text,
                 voice_source="fixture", voice_param="female", gender="female")

        async def generate(*args, **kwargs):
            duration = len(cue.content.split()) * per_word
            write_wave(path, duration)
            return dict(index=1, path=str(path), start=0., end=duration,
                        actual_audio_duration=duration, speed_ratio=1.0,
                        content=cue.content)

        async def fit(*args, **kwargs):
            if can_fit:
                write_wave(path, min(window, measured_duration(path)))
            return str(path), measured_duration(path), 1.0

        if condensation_error is not None:
            def fail_condensation(items, *, failure_causes, **_kwargs):
                failure_causes.update((item["index"], condensation_error) for item in items)
                return {}

            translation.condense_vietnamese_subtitles_batch.side_effect = fail_condensation

        fits = Mock(wraps=fit)
        namespace["generate_single_tts"] = generate
        namespace["fit_audio_file"] = fits
        namespace["calculate_reading_windows"] = lambda segments, **kwargs: {s.index: window for s in segments}
        return NS(path=path, cue=cue, fits=fits, cache_write=cache_write,
                  invoke=lambda: asyncio.run(namespace["generate_dubbing_audio"](
                      [cue], str(tmp_path), voice_source="fixture", video_duration=window + .05)))
    return run


def test_overflowing_cue_is_truncated_and_fits(run_tts, caplog):
    job = run_tts("một hai ba bốn năm sáu", window=1.5)
    with caplog.at_level(logging.WARNING, logger="v1_trunc_test"):
        result = job.invoke()
    assert len(result) == 1
    assert job.cue.content == "một hai ba"
    assert result[0]["truncated_from"] == "một hai ba bốn năm sáu"
    assert result[0]["actual_audio_duration"] <= 1.5 + 0.005
    assert "Last-resort truncation applied to cue 1" in caplog.text


def test_truncation_disabled_raises_speech_timing_error(run_tts, monkeypatch):
    monkeypatch.setenv("V1_ALLOW_SPEECH_TRUNCATION", "0")
    job = run_tts("một hai ba bốn năm sáu", window=1.5)
    with pytest.raises(guard.SpeechTimingError, match="shorten translation before retry"):
        job.invoke()
    assert job.cue.content == "một hai ba bốn năm sáu"


def test_unfittable_cue_still_raises_speech_timing_error(run_tts):
    job = run_tts("một hai ba bốn năm sáu", window=0.3)
    with pytest.raises(guard.SpeechTimingError, match="shorten translation before retry"):
        job.invoke()


def test_transient_condensation_failure_still_raises_incomplete(run_tts):
    timeout = TimeoutError("Temporary provider timeout")
    job = run_tts("một hai ba bốn năm sáu", window=1.5, condensation_error=timeout)
    with pytest.raises(TTSIncompleteError, match="temporary condensation failure") as caught:
        job.invoke()
    assert caught.value.__cause__ is timeout


def test_truncated_cue_is_never_written_to_voice_cache(run_tts):
    job = run_tts("một hai ba bốn năm sáu", window=1.5)
    job.invoke()
    job.cache_write.assert_not_called()
