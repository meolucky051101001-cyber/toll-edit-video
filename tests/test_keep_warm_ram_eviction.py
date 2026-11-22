"""Idle warm model workers give RAM back to the next GPU stage and expire when idle."""
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil
import pytest

from backend import v1_gpu_gatekeeper as gk
from backend import v1_vram_monitor as vram_mon
from backend import worker_settings
from backend.ai import v1_asr_session, v1_ocr_session, v1_separator_session


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(gk, "GPU_LOCK_FILE", tmp_path / "gpu.lock")
    monkeypatch.setattr(gk, "_CURRENT_HOLDER", None)
    monkeypatch.setattr(gk, "_RECURSION_DEPTH", 0)
    monkeypatch.setattr(gk, "_HELD_HANDLE", None)
    monkeypatch.setattr(worker_settings, "get_worker_settings", lambda: {"auto_vram_guard": True})
    monkeypatch.setattr(vram_mon, "is_vram_safe_for_gpu_step", lambda min_free_mb=1800.0: (True, "ok"))
    closed = []
    for name, module in (("ocr", v1_ocr_session), ("separator", v1_separator_session), ("asr", v1_asr_session)):
        monkeypatch.setattr(module, "_process", object())

        def close(name=name, module=module):
            closed.append(name)
            module._process = None

        monkeypatch.setattr(module, "close_session", close)

    def run(stage, available_mb):
        monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=available_mb * 1024 * 1024))
        with gk.gpu_gatekeeper(stage, timeout_seconds=5.0):
            pass
        return closed

    return run


def test_low_ram_releases_idle_workers_of_other_stages(gate):
    assert gate("speech_asr", available_mb=1500) == ["ocr", "separator"]


def test_ocr_stage_releases_idle_separator_and_asr(gate):
    assert gate("visual_ocr", available_mb=900) == ["separator", "asr"]


def test_ample_ram_keeps_workers_warm(gate):
    assert gate("speech_asr", available_mb=8000) == []


def test_eviction_floor_can_be_disabled(gate, monkeypatch):
    monkeypatch.setenv("V1_KEEP_WARM_EVICT_BELOW_MB", "0")
    assert gate("visual_ocr", available_mb=500) == []


def fake_worker_process():
    process = Mock()
    process.poll.return_value = None
    process.stderr = io.StringIO("")

    def reply(line):
        Path(json.loads(line)["response"]).write_text('{"success": true}', encoding="utf-8")

    process.stdin.write.side_effect = reply
    return process


def wait_until_closed(module, timeout=5.0):
    deadline = time.monotonic() + timeout
    while module._process is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    return module._process is None


def test_idle_separator_worker_expires(monkeypatch):
    monkeypatch.setenv("V1_SEPARATOR_KEEP_WARM_IDLE_S", "0.05")
    monkeypatch.setenv("V1_SEPARATOR_KEEP_WARM_MIN_RAM_MB", "0")
    process = fake_worker_process()
    try:
        with patch.object(v1_separator_session.subprocess, "Popen", return_value=process):
            result = v1_separator_session.run_separator_request(
                "python", "worker.py", {"input_audio": "a.wav"}, timeout=5)
        assert result == {"success": True}
        assert v1_separator_session._idle_timer is not None and v1_separator_session._idle_timer.daemon
        assert wait_until_closed(v1_separator_session)
        process.terminate.assert_called_once()
    finally:
        v1_separator_session.close_session()


def test_idle_ocr_worker_expires(monkeypatch, tmp_path):
    monkeypatch.setenv("V1_OCR_KEEP_WARM_IDLE_S", "0.05")
    process = fake_worker_process()
    args = ["python", "worker.py", "--request", str(tmp_path / "request.json"),
            "--response", str(tmp_path / "response.json")]
    try:
        with patch.object(v1_ocr_session.subprocess, "Popen", return_value=process):
            v1_ocr_session.run_request(args, timeout=5, env={})
        assert wait_until_closed(v1_ocr_session)
        process.terminate.assert_called_once()
    finally:
        v1_ocr_session.close_session()


def test_busy_separator_worker_is_not_expired(monkeypatch):
    monkeypatch.setenv("V1_SEPARATOR_KEEP_WARM_IDLE_S", "300")
    monkeypatch.setenv("V1_SEPARATOR_KEEP_WARM_MIN_RAM_MB", "0")
    process = fake_worker_process()
    try:
        with patch.object(v1_separator_session.subprocess, "Popen", return_value=process):
            v1_separator_session.run_separator_request("python", "worker.py", {}, timeout=5)
        assert v1_separator_session.close_if_idle(300) is False
        assert v1_separator_session._process is process
    finally:
        v1_separator_session.close_session()
