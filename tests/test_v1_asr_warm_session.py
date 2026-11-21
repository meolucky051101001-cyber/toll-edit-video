import io
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from backend import v1_gpu_gatekeeper
from backend.ai import v1_asr_isolated, v1_asr_session
from backend.ai import v1_asr_cache, transcription

WORKER_PATH = Path(__file__).resolve().parents[1] / "backend" / "model_workers" / "v1_asr_worker.py"

FAKE_WORKER_SOURCE = r'''
import json
import os
import sys
import uuid
import time

instance_id = uuid.uuid4().hex
for line in sys.stdin:
    envelope = json.loads(line)
    with open(envelope["request"], encoding="utf-8") as request_file:
        payload = json.load(request_file)
    mode = os.environ.get("V1_ASR_FAKE_MODE", "success")
    if mode == "crash":
        os._exit(17)
    if mode == "timeout":
        while True:
            time.sleep(1)
    response = {
        "success": mode != "error",
        "pid": os.getpid(),
        "instance_id": instance_id,
        "reuse": os.environ.get("V1_ASR_REUSE"),
        "options": payload.get("options"),
    }
    if mode == "error":
        response["error"] = "fake worker error"
    temp_path = envelope["response"] + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as response_file:
        json.dump(response, response_file)
    os.replace(temp_path, envelope["response"])
'''


@pytest.fixture(autouse=True)
def clean_asr_session(monkeypatch):
    monkeypatch.setattr(v1_asr_session, "_below_ram_threshold", lambda: False)
    monkeypatch.setattr(
        v1_asr_session.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0),
    )
    v1_asr_session.close_session()
    yield
    v1_asr_session.close_session()


def _fake_worker(tmp_path):
    worker = tmp_path / "fake_asr_worker.py"
    worker.write_text(FAKE_WORKER_SOURCE, encoding="utf-8")
    return worker


def _isolated_test_backend(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    ai_dir = backend / "ai"
    model_workers = backend / "model_workers"
    python = backend / "venv" / "Scripts" / "python.exe"
    ai_dir.mkdir(parents=True)
    model_workers.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    for relative in (
        "model_workers/v1_asr_worker.py",
        "ai/v1_conditional_asr.py",
        "ai/v1_sensevoice_adapter.py",
        "ai/v1_paraformer_adapter.py",
        "ai/transcription.py",
        "ai/v1_model_policy.py",
    ):
        path = backend / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(v1_asr_isolated, "__file__", str(ai_dir / "v1_asr_isolated.py"))
    return backend, python


def _write_srt(path):
    Path(path).write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nhello\n",
        encoding="utf-8",
    )


def _run_worker_serve(tmp_path, monkeypatch, extract):
    request_path = tmp_path / "request.json"
    response_path = tmp_path / "response.json"
    output_path = tmp_path / "result.srt"
    request_path.write_text(
        json.dumps({
            "audio": "input.wav",
            "output": str(output_path),
            "original_audio": "original.wav",
            "options": {"model_id": "test-model", "chunking": False},
        }),
        encoding="utf-8",
    )
    fake_ai = ModuleType("ai")
    fake_ai.__path__ = []
    pyav_compat = ModuleType("ai.pyav_compat")
    pyav_compat.install_pyav_compat = lambda: None
    transcription = ModuleType("ai.transcription")
    transcription.extract_subtitles_whisper = extract
    stage_runtime = ModuleType("v1_stage_runtime")

    def save_payload(path, **values):
        Path(path).write_text(json.dumps(values), encoding="utf-8")

    stage_runtime.save_payload = save_payload
    protocol = json.dumps({"request": str(request_path), "response": str(response_path)}) + "\n"
    modules = {
        "ai": fake_ai,
        "ai.pyav_compat": pyav_compat,
        "ai.transcription": transcription,
        "v1_stage_runtime": stage_runtime,
    }
    with patch.dict(sys.modules, modules), \
         patch("sys.stdin", io.StringIO(protocol)), \
         patch("sys.argv", [str(WORKER_PATH), "--serve"]), \
         patch.dict(os.environ, {"V1_ASR_REUSE": "0", "V1_ASR_JOB_OPTIONS": "not-json"}), \
         pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(WORKER_PATH), run_name="__main__")
    assert exit_info.value.code == 0
    return json.loads(response_path.read_text(encoding="utf-8")), output_path


def test_asr_worker_serve_uses_payload_options_and_saves_result(tmp_path, monkeypatch):
    calls = []

    def extract(audio, output, **kwargs):
        calls.append((audio, output, kwargs, os.environ.get("V1_ASR_REUSE")))
        _write_srt(output)
        return ["segment"]

    response, output = _run_worker_serve(tmp_path, monkeypatch, extract)

    assert response == {"success": True}
    assert output.is_file()
    assert json.loads(output.with_suffix(".json").read_text(encoding="utf-8")) == {
        "segments": ["segment"],
    }
    assert calls == [(
        "input.wav",
        str(output),
        {
            "num_workers": 1,
            "original_audio_path": "original.wav",
            "model_id": "test-model",
            "chunking": False,
        },
        "1",
    )]


def test_asr_worker_serve_returns_errors(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("transcription failed")

    response, _ = _run_worker_serve(tmp_path, monkeypatch, fail)
    assert response == {"success": False, "error": "transcription failed"}


@pytest.mark.parametrize("reuse, expected_constructions", [(True, 1), (False, 2)])
def test_transcription_reuses_whisper_model_only_when_enabled(
    monkeypatch, reuse, expected_constructions
):
    v1_asr_cache.cache.close()
    monkeypatch.setattr(v1_asr_cache.cache, "memory_ok", lambda: True)
    monkeypatch.setenv("V1_ASR_REUSE", "1" if reuse else "0")
    constructions = []

    class FakeModel:
        def __init__(self):
            self.model = SimpleNamespace(
                unload_model=lambda **_kwargs: None,
                load_model=lambda: None,
            )

    def make_model(*_args, **_kwargs):
        constructions.append(True)
        return FakeModel()

    fake_torch = ModuleType("torch")
    fake_torch.cuda = SimpleNamespace(
        is_available=lambda: True,
        get_device_name=lambda _index: "test GPU",
        empty_cache=lambda: None,
    )
    with patch.dict(sys.modules, {"torch": fake_torch}), \
         patch.object(transcription, "_cached_model_path", return_value="cached-model"), \
         patch.object(transcription, "WhisperModel", side_effect=make_model), \
         patch.object(transcription, "_transcribe_raw_segments", return_value=[]):
        transcription._transcribe_once("audio.wav", "model", 1)
        transcription._transcribe_once("audio.wav", "model", 1)

    assert len(constructions) == expected_constructions
    v1_asr_cache.cache.close()


def test_asr_session_reuses_process_and_restarts_when_key_changes(tmp_path):
    worker = _fake_worker(tmp_path)
    payload = {"audio": "audio.wav", "output": "output.srt", "options": {"chunking": True}}
    base_env = dict(os.environ, V1_ASR_MODEL="model-a", V1_ASR_JOB_OPTIONS="first")
    first = v1_asr_session.run_asr_request(
        sys.executable, worker, payload, timeout=5, env=base_env,
    )
    changed_excluded = dict(base_env, V1_ASR_JOB_OPTIONS="second", V1_ASR_REUSE="0")
    second = v1_asr_session.run_asr_request(
        sys.executable, worker, payload, timeout=5, env=changed_excluded,
    )

    assert first["pid"] == second["pid"]
    assert first["instance_id"] == second["instance_id"]
    assert first["reuse"] == second["reuse"] == "1"
    assert second["options"] == payload["options"]

    changed_key = dict(changed_excluded, V1_ASR_MODEL="model-b")
    third = v1_asr_session.run_asr_request(
        sys.executable, worker, payload, timeout=5, env=changed_key,
    )
    assert third["instance_id"] != second["instance_id"]
    assert v1_asr_session._key[2] != ()


def test_asr_session_closes_when_idle(tmp_path):
    worker = _fake_worker(tmp_path)
    v1_asr_session.run_asr_request(
        sys.executable,
        worker,
        {"audio": "audio.wav", "output": "output.srt"},
        timeout=5,
    )
    assert v1_asr_session.close_if_idle(300) is False
    v1_asr_session._last_used_at = time.monotonic() - 301
    assert v1_asr_session.close_if_idle(300) is True
    assert v1_asr_session._process is None


def test_asr_session_expires_automatically_when_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("V1_ASR_KEEP_WARM_IDLE_S", "0.1")
    worker = _fake_worker(tmp_path)
    v1_asr_session.run_asr_request(
        sys.executable,
        worker,
        {"audio": "audio.wav", "output": "output.srt"},
        timeout=5,
        env=dict(os.environ),
    )

    timer = v1_asr_session._idle_timer
    assert timer is not None and timer.daemon
    timer.join(timeout=2)
    assert v1_asr_session._process is None


@pytest.mark.parametrize("failure_mode", ["error", "crash", "timeout", "error-retry"])
def test_asr_warm_failure_falls_back_to_one_shot(tmp_path, monkeypatch, failure_mode):
    backend, _python = _isolated_test_backend(tmp_path, monkeypatch)
    fake_worker = _fake_worker(tmp_path)
    monkeypatch.setenv("V1_ASR_KEEP_WARM", "1")
    monkeypatch.setenv(
        "V1_ASR_FAKE_MODE",
        "error" if failure_mode == "error-retry" else failure_mode,
    )
    monkeypatch.setenv("V1_ASR_MODEL", "test-model")
    real_run_asr_request = v1_asr_session.run_asr_request

    def run_fake_worker(_python, _worker, payload, **kwargs):
        return real_run_asr_request(
            sys.executable,
            fake_worker,
            payload,
            **kwargs,
        )

    monkeypatch.setattr(v1_asr_session, "run_asr_request", run_fake_worker)
    one_shot_calls = []

    def fake_run(args, **kwargs):
        one_shot_calls.append((args, kwargs))
        assert kwargs["env"]["V1_ASR_REUSE"] == "0"
        assert json.loads(kwargs["env"]["V1_ASR_JOB_OPTIONS"])["model_id"] == "test-model"
        if failure_mode == "error-retry" and len(one_shot_calls) == 1:
            raise subprocess.CalledProcessError(3221226505, args)
        _write_srt(args[-1])

    source = tmp_path / "input.wav"
    source.write_bytes(b"audio")
    output = tmp_path / "out.srt"
    if failure_mode == "error-retry":
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    with patch("backend.batch_control.run", side_effect=fake_run):
        segments = v1_asr_isolated.extract_subtitles_isolated(
            source,
            output,
            model_id="test-model",
            timeout_s=0.5 if failure_mode == "timeout" else 600,
        )

    assert segments[0].content == "hello"
    assert len(one_shot_calls) == (2 if failure_mode == "error-retry" else 1)
    assert output.is_file()
    assert v1_asr_session._process is None
    assert one_shot_calls[0][1]["cwd"] == str(backend)


def test_keep_warm_disabled_uses_one_shot(tmp_path, monkeypatch):
    _isolated_test_backend(tmp_path, monkeypatch)
    monkeypatch.setenv("V1_ASR_KEEP_WARM", "0")
    monkeypatch.setattr(
        v1_asr_session,
        "run_asr_request",
        lambda *args, **kwargs: pytest.fail("warm session should be disabled"),
    )
    calls = []

    def fake_run(args, **kwargs):
        calls.append(kwargs)
        assert kwargs["env"]["V1_ASR_REUSE"] == "0"
        _write_srt(args[-1])

    source = tmp_path / "input.wav"
    source.write_bytes(b"audio")
    with patch("backend.batch_control.run", side_effect=fake_run):
        segments = v1_asr_isolated.extract_subtitles_isolated(source, tmp_path / "out.srt")

    assert segments[0].content == "hello"
    assert len(calls) == 1


def test_gatekeeper_evicts_asr_session_outside_speech_stage(monkeypatch):
    from backend.ai import v1_ocr_session, v1_separator_session

    closed = []
    monkeypatch.setattr(v1_ocr_session, "_process", None)
    monkeypatch.setattr(v1_separator_session, "_process", None)
    monkeypatch.setattr(v1_asr_session, "_process", object())
    monkeypatch.setattr(v1_asr_session, "close_session", lambda: closed.append(True))

    assert v1_gpu_gatekeeper._evict_warm_sessions("speech_asr") is False
    assert closed == []
    assert v1_gpu_gatekeeper._evict_warm_sessions("video_rendering") is True
    assert closed == [True]
