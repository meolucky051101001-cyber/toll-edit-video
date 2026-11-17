"""Ownership handoff and pause regression tests using isolated tracker state."""
import copy
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import job_tracker as tracker
import video_pause
from pipeline_v2.atomic_io import atomic_write_json
from v1_integrity_errors import JobStateReadError, StaleExecutionError, OutputVerificationError


@pytest.fixture(autouse=True)
def isolated_pause(monkeypatch):
    flag = tracker.WORKSPACE / "control" / "video.pause"
    monkeypatch.setattr(video_pause, "FLAG", flag)
    monkeypatch.setattr(video_pause, "ACK", flag.with_name("video.pause.ack"))
    monkeypatch.setattr(video_pause, "_flags", lambda: [flag])


def test_stale_release_never_overwrites_new_owner(monkeypatch):
    tracker.start_batch(1, job_id="A")
    stale = copy.deepcopy(tracker._CURRENT_STATE)
    tracker.release_batch("A")
    tracker.start_batch(1, job_id="B")
    new_bytes = tracker.STATUS_FILE.read_bytes()
    monkeypatch.setattr(tracker, "_CURRENT_STATE", stale)
    tracker.release_batch("A")
    assert tracker.STATUS_FILE.read_bytes() == new_bytes
    assert tracker.get_status(strict=True)["execution_id"] == "B"
    tracker.release_batch("B")


def test_tracker_stop_compatibility_paths_stay_inside_test_fixture(tmp_path):
    import stop_service
    checkout = Path(__file__).resolve().parents[1]
    paths = stop_service.control_directories(checkout, tracker.WORKSPACE)
    assert paths and all(path.is_relative_to(tmp_path) for path in paths)
    tracker.start_batch(1, job_id="fixture-isolation")
    tracker.request_stop()
    assert all((path / "video.stop").is_file() for path in paths)
    tracker.clear_stop_request()
    assert not any((path / "video.stop").exists() for path in paths)


def test_release_holds_os_guard_until_snapshot_is_final(monkeypatch):
    tracker.start_batch(1, job_id="A")
    script = r'''
import json, os, sys
import job_tracker as t
from pipeline_v2.interprocess_lock import CrossProcessLock, PipelineLockTimeout
assert sys.stdin.readline().strip() == 'try'
try:
    with CrossProcessLock(t.BATCH_LOCK_FILE.with_suffix('.guard'), timeout_seconds=0):
        print('UNGUARDED', flush=True)
except PipelineLockTimeout:
    print('GUARDED', flush=True)
assert sys.stdin.readline().strip() == 'claim'
t.start_batch(1, job_id='B')
print(json.dumps(t.get_status(strict=True)), flush=True)
assert sys.stdin.readline().strip() == 'release'
t.release_batch('B')
'''
    env = dict(os.environ, AUTODUB_WORKSPACE=str(tracker.WORKSPACE), AUTODUB_TESTING="1")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "backend") + os.pathsep + env.get("PYTHONPATH", "")
    child = subprocess.Popen([sys.executable, "-B", "-c", script], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    original = tracker._release_batch_lock_guarded
    def handoff(job_id):
        original(job_id)
        child.stdin.write("try\n"); child.stdin.flush()
        assert child.stdout.readline().strip() == "GUARDED"
    monkeypatch.setattr(tracker, "_release_batch_lock_guarded", handoff)
    try:
        tracker.release_batch("A")
        child.stdin.write("claim\n"); child.stdin.flush()
        state = json.loads(child.stdout.readline())
        assert state["execution_id"] == "B" and state["active"] and state["occupies_slot"]
        assert tracker.get_status(strict=True)["execution_id"] == "B"
        child.stdin.write("release\n"); child.stdin.flush()
        _, error = child.communicate(timeout=15)
        assert child.returncode == 0, error
    finally:
        if child.poll() is None:
            child.kill(); child.communicate(timeout=10)


def test_release_keeps_lease_when_status_write_fails(monkeypatch):
    tracker.start_batch(1, job_id="A")
    monkeypatch.setattr(tracker, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(JobStateReadError):
        tracker.release_batch("A")
    assert json.loads(tracker.BATCH_LOCK_FILE.read_text())["job_id"] == "A"


def test_release_reports_retained_lease(monkeypatch):
    tracker.start_batch(1, job_id="A")
    monkeypatch.setattr(tracker, "_release_batch_lock_guarded", lambda *a: None)
    tracker.release_batch("A")
    state = tracker.get_status(strict=True)
    assert state["status"] == "stop_incomplete" and state["occupies_slot"]


def test_completion_receipt_is_identity_bound_and_idempotent(monkeypatch, tmp_path):
    import dashboard_media
    monkeypatch.setattr(dashboard_media, "media_status", lambda *a, **k: {"status": "valid"})
    tracker.start_batch(1, job_id="A")
    token = tracker.current_execution.set("A")
    try:
        receipt = tracker.finish_video("input.mp4", str(tmp_path / "out.mp4"), 2)
        assert receipt["execution_id"] == "A" and receipt["generation"] == 1
        assert tracker.finish_video("input.mp4", str(tmp_path / "out.mp4"), 2) == receipt
        assert len(tracker.get_status()["history"]) == 1
        tracker.release_batch("A")
        tracker.start_batch(1, job_id="B")
        with pytest.raises(StaleExecutionError):
            tracker.finish_video("input.mp4", "old.mp4")
        assert tracker.get_status()["execution_id"] == "B"
    finally:
        tracker.current_execution.reset(token)


def test_completion_rechecks_identity_after_probe(monkeypatch):
    import dashboard_media
    tracker.start_batch(1, job_id="A")
    def probe(*args, **kwargs):
        tracker.release_batch("A")
        tracker.start_batch(1, job_id="B")
        return {"status": "valid"}
    monkeypatch.setattr(dashboard_media, "media_status", probe)
    with pytest.raises(StaleExecutionError):
        tracker.finish_video("A", "out.mp4")
    assert tracker.get_status()["execution_id"] == "B"
    assert not tracker.get_status()["history"]


def test_released_execution_cannot_finalize_or_publish_progress_before_next_claim(monkeypatch):
    import dashboard_media
    monkeypatch.setattr(dashboard_media, "media_status", lambda *a, **k: {"status": "valid"})
    tracker.start_batch(1, job_id="A")
    token = tracker.current_execution.set("A")
    try:
        tracker.release_batch("A")
        before = tracker.STATUS_FILE.read_bytes()
        tracker.update_step(8, "late callback", percent=99)
        with pytest.raises(StaleExecutionError):
            tracker.finish_video("A", "late-output.mp4")
        assert tracker.STATUS_FILE.read_bytes() == before
        assert not tracker.get_status()["history"]
    finally:
        tracker.current_execution.reset(token)


def test_output_verification_error_is_not_ordinary_retry(monkeypatch):
    import dashboard_media
    monkeypatch.setattr(dashboard_media, "media_status", lambda *a, **k: {"status": "invalid", "reason": "missing audio"})
    tracker.start_batch(1, job_id="A")
    with pytest.raises(OutputVerificationError):
        tracker.finish_video("A", "broken.mp4")
    assert tracker.get_status()["video_status"] == "error"


def test_pause_cleanup_is_bound_to_execution():
    tracker.start_batch(1, job_id="A")
    video_pause.request_pause("A", 1)
    video_pause._acknowledge("A", 1)
    assert video_pause.state("A", 1) == "paused"
    tracker.release_batch("A")
    tracker.start_batch(1, job_id="B")
    video_pause.request_pause("B", 2)
    video_pause._acknowledge("B", 2)
    video_pause.clear_execution_pause("A", 1)
    assert video_pause.state("B", 2) == "paused"
    assert video_pause.state("A", 1) == "running"


def test_api_ignores_old_pause_and_rejects_stale_controls(monkeypatch):
    import main
    from fastapi.testclient import TestClient
    tracker.start_batch(1, job_id="B")
    video_pause.request_pause("A", 99)
    video_pause._acknowledge("A", 99)
    headers = {"X-Local-Control-Token": main.LOCAL_TOKEN, "X-Dashboard-Input": "1"}
    with TestClient(main.app, headers=headers) as client:
        state = client.get("/api/status").json()
        assert state["pause_state"] == "running"
        assert state["capabilities"]["can_pause"]
        for endpoint in ("pause-video", "resume-video"):
            response = client.post("/api/" + endpoint, json={"execution_id": "A", "generation": 99})
            assert response.status_code == 409
        response = client.post("/api/pause-video", json={"execution_id": "B", "generation": 1})
        assert response.status_code == 200, response.text
        assert video_pause.state("B", 1) == "pausing"


def test_resume_current_execution_ignores_unrelated_legacy_flags(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy" / "video.pause"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({"job_id": "A", "generation": 1, "token": "old"}))
    monkeypatch.setattr(video_pause, "_flags", lambda: [video_pause.FLAG, legacy])
    video_pause.request_pause("B", 2)
    video_pause._acknowledge("B", 2)
    assert video_pause.state("B", 2) == "paused"
    video_pause.resume("B", 2)
    assert video_pause.state("B", 2) == "running" and legacy.is_file()
    assert json.loads(legacy.read_text())["job_id"] == "A"


def test_stale_checkpoint_cannot_ack_pause_of_new_execution():
    tracker.start_batch(1, job_id="B")
    video_pause.request_pause("B", 1)
    token = tracker.current_execution.set("A")
    try:
        asyncio.run(asyncio.wait_for(video_pause.checkpoint(), 1))
    finally:
        tracker.current_execution.reset(token)
    assert video_pause.state("B", 1) == "pausing"
    assert not video_pause.ACK.exists()
