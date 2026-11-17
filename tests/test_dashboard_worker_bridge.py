"""Foundation experiments cannot bypass the live single-worker dispatcher."""
import json
import os

import pytest

import job_tracker
import worker_settings
from execution_registry import AdmissionBusy, ExecutionRegistry


@pytest.mark.parametrize("opt_in", ["", "1", "true"])
def test_requested_dual_workers_never_overstate_dispatcher_capability(tmp_path, monkeypatch, opt_in):
    monkeypatch.setenv("AUTODUB_EXPERIMENTAL_DUAL_WORKERS", opt_in)
    monkeypatch.setattr(worker_settings, "SETTINGS_FILE", tmp_path / "workers.json")
    saved = worker_settings.save_worker_settings(2)
    restored = worker_settings.get_worker_settings()
    for settings in (saved, restored):
        assert settings["requested_concurrency"] == 2
        assert settings["effective_concurrency"] == settings["max_supported_workers"] == 1
        assert not settings["experimental_dual_workers"]


def test_worker_settings_get_and_post_agree_with_experimental_flag(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setenv("AUTODUB_EXPERIMENTAL_DUAL_WORKERS", "1")
    monkeypatch.setattr(worker_settings, "SETTINGS_FILE", tmp_path / "workers.json")
    with TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN}) as client:
        post = client.post("/api/worker-settings", json={"concurrency": 2})
        get = client.get("/api/worker-settings")
    assert post.status_code == get.status_code == 200
    for field in ("requested_concurrency", "effective_concurrency", "max_supported_workers", "capability_reason"):
        assert post.json()[field] == get.json()[field]
    assert get.json()["effective_concurrency"] == 1


def test_registry_blocks_legacy_until_actual_owner_release():
    registry = ExecutionRegistry(job_tracker.WORKSPACE, job_tracker.BATCH_LOCK_FILE)
    registry.claim("foundation", {}, capacity=2)
    registry.request_stop("foundation")
    with pytest.raises(job_tracker.JobAlreadyRunningError):
        job_tracker.start_batch(1)
    assert not job_tracker.BATCH_LOCK_FILE.exists()
    assert registry.release("foundation")
    identity = job_tracker.start_batch(1)
    try:
        with pytest.raises(AdmissionBusy):
            registry.claim("another", {}, capacity=2)
    finally:
        job_tracker.release_batch(identity)


def test_corrupt_registry_blocks_legacy_without_overwriting_bytes():
    registry = ExecutionRegistry(job_tracker.WORKSPACE, job_tracker.BATCH_LOCK_FILE)
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_bytes(b"{broken")
    with pytest.raises(job_tracker.JobAlreadyRunningError):
        job_tracker.start_batch(1)
    assert registry.path.read_bytes() == b"{broken"
    assert not job_tracker.BATCH_LOCK_FILE.exists()


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -1, "invalid", True, False])
def test_unknown_live_legacy_process_identity_does_not_allow_reclaim(timestamp):
    assert job_tracker._owner_is_running({"pid": os.getpid(), "owner_create_time": timestamp})


@pytest.mark.parametrize("pid", [True, False, 0, -1, "123", None])
@pytest.mark.parametrize("legacy_location", [False, True])
def test_malformed_legacy_pid_cannot_be_reclaimed(pid, legacy_location):
    path = (job_tracker.WORKSPACE / "batch.lock") if legacy_location else job_tracker.BATCH_LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"job_id": "unknown", "pid": pid}).encode()
    path.write_bytes(original)
    with pytest.raises(job_tracker.JobAlreadyRunningError):
        job_tracker.start_batch(1)
    assert path.read_bytes() == original
