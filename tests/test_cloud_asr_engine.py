from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.cloud import cloud_orchestrator, colab_bridge
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.cloud_security import _SECRET_ENV_NAMES, redact_secrets
from backend.cloud.gpu_scripts import full_pipeline


def test_cloud_asr_engine_env_is_valid_and_not_a_secret():
    engine_env = {"AUTODUB_CLOUD_ASR_ENGINE": "faster-whisper"}

    assert colab_bridge._validate_env_vars(engine_env) == engine_env
    assert "AUTODUB_CLOUD_ASR_ENGINE" not in _SECRET_ENV_NAMES
    assert redact_secrets("AUTODUB_CLOUD_ASR_ENGINE=faster-whisper") == "AUTODUB_CLOUD_ASR_ENGINE=faster-whisper"


@pytest.mark.parametrize(
    ("configured_engine", "expected_engine"),
    [("faster-whisper", "faster-whisper"), ("openai-whisper", None)],
)
def test_orchestrator_forwards_non_default_asr_engine_to_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_engine: str,
    expected_engine: str | None,
):
    video = tmp_path / "input.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomv1data")
    output_dir = tmp_path / "deliverables"
    output_dir.mkdir()
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(output_dir))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "cloud_asr_engine", configured_engine)
    monkeypatch.setattr(cloud_orchestrator, "_resolve_gemini_key_and_secrets", lambda: ("", ()))
    monkeypatch.setattr(cloud_orchestrator, "_resolve_local_font_files_for_bundle", lambda _font: ())

    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.4)
    claimed = store.claim_job(job.job_id, owner_id="asr-env-test")
    assert claimed is not None
    pipeline_envs = []
    output_bytes = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomrendered"
    remote_metadata = {
        "status": "completed",
        "segments_count": 1,
        "source_caption_evidence": {"version": 1, "passed": True, "cue_count": 1},
        "subtitle_timing": {"passed": True, "cue_count": 1},
        "translated_count": 1,
        "tts_verified_count": 1,
        "output_video_probe": {
            "video_codec": "h264",
            "audio_codec": "aac",
            "size_bytes": len(output_bytes),
        },
    }

    class FakeAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0, "usage_rate": 0.0, "active_assignments": 0}

        def get_sessions_status(self):
            return {"ok": True, "sessions": [], "error": ""}

        def create_session(self, session_name, gpu="T4", cancel_check=None):
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None, cancel_check=None):
            return True, "uploaded"

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None, cancel_check=None):
            if "full_pipeline.py" in argv:
                pipeline_envs.append(env_vars)
            return True, "ok"

        def download_file(self, session_name, remote_name, local_dir, cancel_check=None):
            out_dir = Path(local_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            if remote_name == "output_video.mp4":
                output_path = out_dir / remote_name
                output_path.write_bytes(output_bytes)
                return True, str(output_path)
            if remote_name == "result_metadata.json":
                metadata_path = out_dir / remote_name
                metadata_path.write_text(json.dumps(remote_metadata), encoding="utf-8")
                return True, str(metadata_path)
            return False, ""

        def verify_session_stopped(self, session_name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    orchestrator = CloudOrchestrator(store=store, adapter=FakeAdapter(), allowed_roots=[tmp_path])
    monkeypatch.setattr(
        orchestrator,
        "validate_downloaded_output",
        lambda *_args, **_kwargs: {
            "sha256": hashlib.sha256(output_bytes).hexdigest(),
            "size_bytes": len(output_bytes),
            "remote_metadata": remote_metadata,
            "local_probe": {"duration_seconds": 1.0, "video_codec": "h264", "audio_codec": "aac"},
            "qc_report": {"passed": True},
        },
    )

    assert orchestrator.process_job(claimed, owner_id="asr-env-test") is True
    assert len(pipeline_envs) == 1
    env_vars = pipeline_envs[0] or {}
    if expected_engine:
        assert env_vars["AUTODUB_CLOUD_ASR_ENGINE"] == expected_engine
    else:
        assert "AUTODUB_CLOUD_ASR_ENGINE" not in env_vars


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


@pytest.mark.parametrize(
    ("cuda_available", "expected_device", "expected_compute_type"),
    [(True, "cuda", "int8_float16"), (False, "cpu", "int8")],
)
def test_faster_whisper_engine_uses_cloud_decode_policy_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cuda_available: bool,
    expected_device: str,
    expected_compute_type: str,
):
    calls = {}
    audio = [0.0] * 16000
    original_audio = [1.0] * 16000
    speech_windows = [{"start": 0.0, "end": 1.0}]
    word = SimpleNamespace(word="hello", start=0.2, end=0.8, probability=0.95)

    torch_module = ModuleType("torch")
    torch_module.cuda = SimpleNamespace(is_available=lambda: cuda_available)
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
        {"device": expected_device, "compute_type": expected_compute_type},
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
    assert audit["device"] == expected_device
    assert audit["recognition_track"] == "vocals_retry"
    assert "hello" in output_path.read_text(encoding="utf-8")
