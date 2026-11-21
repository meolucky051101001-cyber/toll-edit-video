from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.cloud.gpu_scripts import full_pipeline


def test_faster_whisper_result_conversion_preserves_segment_and_word_fields():
    word = SimpleNamespace(word=" hello", start=0.25, end=0.75, probability=0.91)
    segment = SimpleNamespace(
        start=0.25,
        end=0.75,
        text=" hello",
        words=[word],
        tokens=[11, 12],
        temperature=0.0,
        avg_logprob=-0.2,
        compression_ratio=0.8,
        no_speech_prob=0.01,
    )

    converted = full_pipeline._faster_whisper_result_to_dict(iter([segment]))

    assert converted["text"] == " hello"
    assert converted["segments"][0] == {
        "id": 0,
        "seek": 0,
        "start": 0.25,
        "end": 0.75,
        "text": " hello",
        "tokens": [11, 12],
        "temperature": 0.0,
        "avg_logprob": -0.2,
        "compression_ratio": 0.8,
        "no_speech_prob": 0.01,
        "words": [{"word": " hello", "start": 0.25, "end": 0.75, "probability": 0.91}],
    }


def test_faster_whisper_engine_uses_cloud_decode_policy_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = {}
    audio = [0.0] * 16000
    original_audio = [1.0] * 16000
    speech_windows = [{"start": 0.0, "end": 1.0}]
    word = SimpleNamespace(word="hello", start=0.2, end=0.8, probability=0.95)

    torch_module = ModuleType("torch")
    torch_module.cuda = SimpleNamespace(is_available=lambda: True)
    whisper_module = ModuleType("whisper")
    whisper_module.load_audio = lambda _path: original_audio

    class FakeWhisperModel:
        def __init__(self, model_name, **kwargs):
            calls["model"] = (model_name, kwargs)

        def transcribe(self, target_audio, **kwargs):
            calls.setdefault("transcribe", []).append((target_audio, kwargs))
            score = -2.0 if len(calls["transcribe"]) == 1 else -0.1
            segment = SimpleNamespace(
                start=0.2,
                end=0.8,
                text="hello",
                words=[word],
                tokens=[101],
                temperature=0.0,
                avg_logprob=score,
                compression_ratio=0.5,
                no_speech_prob=0.0,
            )
            return iter([segment]), SimpleNamespace(language="en")

    faster_whisper_module = ModuleType("faster_whisper")
    faster_whisper_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "torch", torch_module)
    monkeypatch.setitem(sys.modules, "whisper", whisper_module)
    monkeypatch.setitem(sys.modules, "faster_whisper", faster_whisper_module)
    monkeypatch.setenv("AUTODUB_CLOUD_ASR_ENGINE", "faster-whisper")
    monkeypatch.setattr(
        full_pipeline,
        "_asr_speech_audio",
        lambda *_args: (
            audio,
            {
                "speech_windows_seconds": speech_windows,
                "track_windows_seconds": {"original": speech_windows},
            },
        ),
    )
    monkeypatch.setattr(full_pipeline, "_whisper_clip_timestamps", lambda *_args: [0.0, 1.0])
    monkeypatch.setattr(full_pipeline, "validate_asr_source_timeline", lambda *_args: None)

    audit_path = tmp_path / "audit.json"
    output_path = tmp_path / "captions.srt"
    segments = full_pipeline.step3_asr_whisper(
        "vocals.wav",
        str(output_path),
        reference_audio_path="original.wav",
        audit_path=str(audit_path),
    )

    assert calls["model"] == (
        "large-v3-turbo",
        {"device": "cuda", "compute_type": "int8_float16"},
    )
    assert len(calls["transcribe"]) == 2
    assert calls["transcribe"][0][0] is original_audio
    assert calls["transcribe"][1][0] is audio
    assert calls["transcribe"][0][1] == calls["transcribe"][1][1] == {
        "beam_size": 5,
        "temperature": 0.0,
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "clip_timestamps": [0.0, 1.0],
    }
    assert segments == [{"id": 1, "start": 0.2, "end": 0.8, "text": "hello"}]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["engine"] == "faster-whisper"
    assert audit["recognition_track"] == "vocals_retry"
    assert "hello" in output_path.read_text(encoding="utf-8")
