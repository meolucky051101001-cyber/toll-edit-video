"""Tracker ownership stays shared when control directories appear or migrate."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import job_tracker
from pipeline_v2.atomic_io import atomic_write_json


def bind(monkeypatch, workspace):
    monkeypatch.setattr(job_tracker, "WORKSPACE", workspace)
    status = job_tracker._resolve_bot_system_file("job_status.json")
    lease = job_tracker._resolve_bot_system_file("batch.lock")
    monkeypatch.setattr(job_tracker, "STATUS_FILE", status)
    monkeypatch.setattr(job_tracker, "BATCH_LOCK_FILE", lease)
    return status, lease


def test_fresh_tracker_binding_is_independent_of_existing_directories(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    status, lease = bind(monkeypatch, workspace)
    assert status.parent == lease.parent == workspace / "bot_system"
    (workspace / "control").mkdir()
    assert job_tracker._resolve_bot_system_file("job_status.json") == status
    assert job_tracker._resolve_bot_system_file("batch.lock") == lease


def test_legacy_binding_survives_control_directory_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = b'{"active": false, "updated_at": 1}'
    (workspace / "job_status.json").write_bytes(original)
    status, lease = bind(monkeypatch, workspace)
    (workspace / "bot_system" / "control").mkdir(parents=True)
    assert status.parent == lease.parent == workspace
    assert job_tracker._resolve_bot_system_file("job_status.json") == status
    assert job_tracker._resolve_bot_system_file("batch.lock") == lease
    assert status.read_bytes() == original


def test_new_process_uses_bound_legacy_lease_after_control_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    atomic_write_json(workspace / "job_status.json", {"active": False, "updated_at": 1})
    status, lease = bind(monkeypatch, workspace)
    execution = job_tracker.start_batch(1)
    (workspace / "bot_system" / "control").mkdir(parents=True)
    script = """
import json
import job_tracker
try:
    job_tracker.start_batch(1)
except job_tracker.JobAlreadyRunningError:
    print(json.dumps({'blocked': True, 'status': str(job_tracker.STATUS_FILE), 'lease': str(job_tracker.BATCH_LOCK_FILE)}))
else:
    raise AssertionError('A second process claimed the shared execution slot')
"""
    env = dict(os.environ, AUTODUB_WORKSPACE=str(workspace), AUTODUB_TESTING="1")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[1] / "backend"), env.get("PYTHONPATH")]))
    try:
        child = subprocess.run([sys.executable, "-B", "-c", script], env=env,
                               capture_output=True, text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout)
        assert result == {"blocked": True, "status": str(status), "lease": str(lease)}
        assert json.loads(lease.read_text(encoding="utf-8"))["job_id"] == execution
    finally:
        job_tracker.release_batch(execution)


@pytest.mark.parametrize("content", ['{broken', '{"schema_version": 1, "directory": "../elsewhere"}'])
def test_invalid_binding_fails_closed_and_preserves_original(tmp_path, monkeypatch, content):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    binding = workspace / ".tracker-location.json"
    binding.write_text(content, encoding="utf-8")
    monkeypatch.setattr(job_tracker, "WORKSPACE", workspace)
    with pytest.raises(job_tracker.JobStateReadError):
        job_tracker._resolve_bot_system_file("job_status.json")
    assert binding.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("content", [json.dumps({"job_id": "older-service", "pid": os.getpid()}), '{unfinished'])
def test_existing_foreign_lease_blocks_new_admission(tmp_path, monkeypatch, content):
    workspace = tmp_path / "workspace"
    _, lease = bind(monkeypatch, workspace)
    legacy = workspace / "batch.lock"
    legacy.write_text(content, encoding="utf-8")
    with pytest.raises(job_tracker.JobAlreadyRunningError):
        job_tracker.start_batch(1)
    assert legacy.read_text(encoding="utf-8") == content
    assert not lease.exists()


def test_proven_stale_foreign_lease_is_preserved_without_blocking(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    _, lease = bind(monkeypatch, workspace)
    legacy = workspace / "batch.lock"
    original = '{"job_id": "retired", "pid": 123456789}'
    legacy.write_text(original, encoding="utf-8")
    monkeypatch.setattr(job_tracker, "_owner_is_running", lambda owner: False)
    execution = job_tracker.start_batch(1)
    try:
        assert lease.is_file()
        assert legacy.read_text(encoding="utf-8") == original
    finally:
        job_tracker.release_batch(execution)
