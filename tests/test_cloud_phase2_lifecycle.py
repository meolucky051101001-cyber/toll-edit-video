"""
tests/test_cloud_phase2_lifecycle.py - Phase 2 lifecycle, cancellation, lease & recovery tests (0 CU / offline).
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.cloud import colab_bridge
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.cloud_worker import CloudWorker


def _make_sample_video(tmp_path: Path, name: str = "sample.mp4") -> Path:
    video = tmp_path / name
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return video


def test_state_machine_progression_and_terminal_immutability(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", tmp_path / "results")
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4")
    claimed = store.claim_next(owner_id="worker-a", lease_seconds=60.0)
    assert claimed is not None
    assert claimed.state == "provisioning"
    assert claimed.attempt == 1
    assert claimed.owner_id == "worker-a"

    # Full canonical happy path
    assert store.update_progress(claimed.job_id, state="uploading", percent=20, owner_id="worker-a", attempt=1)
    assert store.update_progress(claimed.job_id, state="preparing", percent=40, owner_id="worker-a", attempt=1)
    assert store.update_progress(claimed.job_id, state="processing", percent=60, owner_id="worker-a", attempt=1)
    assert store.update_progress(claimed.job_id, state="downloading", percent=85, owner_id="worker-a", attempt=1)
    assert store.update_progress(claimed.job_id, state="validating", percent=95, owner_id="worker-a", attempt=1)
    assert store.update_progress(
        claimed.job_id,
        state="completed",
        percent=100,
        result_path=str(tmp_path / "out" / "cloud_sample.mp4"),
        cu_actual=0.25,
        owner_id="worker-a",
        attempt=1,
    )

    # Terminal immutability: late updates or cancel cannot overwrite completed
    assert store.update_progress(claimed.job_id, state="failed", percent=0, error="late fail") is False
    assert store.update_progress(claimed.job_id, state="processing", percent=50, allow_from_cancel=True) is False
    assert store.request_cancel(claimed.job_id)["cancelled"] is False
    assert store.get_job(claimed.job_id).state == "completed"

    # Terminal cancelled / failed immutability too
    j2 = store.enqueue(str(video), video_name="j2.mp4")
    c2 = store.claim_next(owner_id="worker-a")
    assert store.update_progress(c2.job_id, state="failed", error="boom", owner_id="worker-a", attempt=1)
    assert store.update_progress(c2.job_id, state="uploading", percent=10, owner_id="worker-a", attempt=1) is False
    assert store.get_job(c2.job_id).state == "failed"


def test_update_progress_blocked_during_cancel_requested_and_cancelling(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    store.enqueue(str(video), video_name=video.name)
    claimed = store.claim_next(owner_id="w1")
    assert claimed is not None

    assert store.register_session(claimed.job_id, claimed.attempt, "w1", "cjob001a1") is True
    assert store.update_progress(claimed.job_id, state="uploading", percent=20, owner_id="w1", attempt=1) is True

    req = store.request_cancel(claimed.job_id, reason="User pressed cancel")
    assert req["cancelled"] is True
    assert req["immediate"] is False
    assert req["state"] == "cancel_requested"

    # Late progress update must be rejected while cancel_requested
    assert store.update_progress(claimed.job_id, state="processing", percent=60, owner_id="w1", attempt=1) is False
    assert store.get_job(claimed.job_id).state == "cancel_requested"

    # Cannot jump directly to cancelled while session cleanup is unverified
    assert (
        store.update_progress(
            claimed.job_id,
            state="cancelled",
            owner_id="w1",
            attempt=1,
            allow_from_cancel=True,
        )
        is False
    )

    assert (
        store.update_progress(
            claimed.job_id,
            state="cancelling",
            owner_id="w1",
            attempt=1,
            allow_from_cancel=True,
        )
        is True
    )
    assert store.update_progress(claimed.job_id, state="processing", percent=70, owner_id="w1", attempt=1) is False

    store.record_cleanup_result(
        claimed.job_id,
        claimed.attempt,
        "cjob001a1",
        verified_stopped=True,
        cleanup_status="verified",
        attempts=1,
        error=None,
    )
    assert (
        store.update_progress(
            claimed.job_id,
            state="cancelled",
            owner_id="w1",
            attempt=1,
            cleanup_status="verified",
            allow_from_cancel=True,
        )
        is True
    )
    assert store.get_job(claimed.job_id).state == "cancelled"


def test_two_worker_claim_race_and_lease_takeover_rejects_stale_attempt(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    enqueued = store.enqueue(str(video), video_name=video.name)

    barrier = threading.Barrier(2)

    def _compete(worker_name: str):
        barrier.wait(timeout=3.0)
        return store.claim_next(owner_id=worker_name, lease_seconds=5.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1 = pool.submit(_compete, "worker-1")
        r2 = pool.submit(_compete, "worker-2")
        c1, c2 = r1.result(timeout=5.0), r2.result(timeout=5.0)

    winners = [c for c in (c1, c2) if c is not None]
    assert len(winners) == 1
    first = winners[0]
    other_worker = "worker-2" if first.owner_id == "worker-1" else "worker-1"
    assert first.attempt == 1

    # Simulate lease expiration -> released back to queue for retry
    future_ts = (first.lease_expires_at or time.time()) + 10.0
    released = store.release_expired_lease_for_retry(first.job_id, now=future_ts)
    assert released is True

    second = store.claim_next(owner_id=other_worker, lease_seconds=60.0)
    assert second is not None
    assert second.job_id == enqueued.job_id
    assert second.attempt == 2
    assert second.owner_id == other_worker

    # Stale attempt-1 worker cannot heartbeat or overwrite attempt-2 state
    assert store.heartbeat(first.job_id, owner_id=first.owner_id, attempt=1) is False
    assert (
        store.update_progress(
            first.job_id,
            state="uploading",
            percent=30,
            owner_id=first.owner_id,
            attempt=1,
        )
        is False
    )
    current = store.get_job(enqueued.job_id)
    assert current.attempt == 2
    assert current.owner_id == other_worker
    assert current.state == "provisioning"


def test_exec_argv_midflight_subprocess_cancellation():
    terminated_flag = threading.Event()
    started_flag = threading.Event()

    class FakePopen:
        def __init__(self, cmd_args, **kwargs):
            self.cmd_args = cmd_args
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(b"running...\n")
            self.stderr = io.BytesIO(b"")
            self.returncode = None
            started_flag.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15
            terminated_flag.set()

        def kill(self):
            self.returncode = -9
            terminated_flag.set()

        def wait(self, timeout=None):
            return self.returncode if self.returncode is not None else -15

    def _trigger_abort():
        assert started_flag.wait(timeout=2.0)
        terminated_count = colab_bridge.abort_session_processes("cjobmid1a1")
        assert terminated_count >= 1

    t = threading.Thread(target=_trigger_abort, daemon=True)
    t.start()
    start_ts = time.monotonic()
    with patch.object(colab_bridge.subprocess, "Popen", FakePopen):
        ok, msg = colab_bridge.exec_argv(
            "cjobmid1a1",
            ["python3", "full_pipeline.py", "--input", "/content/in.mp4"],
            timeout=60,
            cancel_check=lambda: False,
        )
    elapsed = time.monotonic() - start_ts
    t.join(timeout=2.0)
    colab_bridge.clear_session_abort("cjobmid1a1")

    assert ok is False
    assert terminated_flag.is_set()
    assert elapsed < 3.0
    assert "hủy" in msg.lower()


def test_cancel_during_create_session_revokes_without_upload_or_pipeline(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    job = store.enqueue(str(video), video_name=video.name)
    claimed = store.claim_next(owner_id="worker-race")
    assert claimed is not None

    events: list[tuple] = []
    active_sessions: set[str] = set()

    class RaceAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 50.0}

        def get_sessions_status(self):
            return {
                "ok": True,
                "sessions": [{"name": s, "is_active": True} for s in sorted(active_sessions)],
                "error": "",
            }

        def create_session(self, session_name, gpu="T4", *, cancel_check=None):
            events.append(("create_session", session_name))
            active_sessions.add(session_name)
            # Cancel arrives right while Colab finishes provisioning VM
            store.request_cancel(claimed.job_id, reason="User cancelled mid-provisioning")
            return True, "created"

        def upload_file(self, *args, **kwargs):
            raise AssertionError("upload_file must NOT be called after cancel_requested during create_session")

        def exec_argv(self, *args, **kwargs):
            raise AssertionError("exec_argv must NOT be called after cancel_requested during create_session")

        def download_file(self, *args, **kwargs):
            raise AssertionError("download_file must NOT be called after cancel_requested")

        def stop_session(self, session_name):
            events.append(("stop_session", session_name))
            active_sessions.discard(session_name)
            return True, "stopped"

        def verify_session_stopped(self, session_name):
            self.stop_session(session_name)
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    orch = CloudOrchestrator(store=store, adapter=RaceAdapter(), allowed_roots=[tmp_path])
    ok = orch.process_job(claimed, owner_id="worker-race")

    assert ok is False
    assert any(e[0] == "stop_session" for e in events)
    assert active_sessions == set()

    final_job = store.get_job(claimed.job_id)
    assert final_job.state == "cancelled"
    assert final_job.cleanup_status == "verified"


def test_cleanup_failure_leaves_cancelling_and_records_cleanup_event(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    store.enqueue(str(video), video_name=video.name)
    claimed = store.claim_next(owner_id="w1")
    assert claimed is not None
    assert store.register_session(claimed.job_id, claimed.attempt, "w1", "cjobfailclean1a1") is True
    assert store.update_progress(claimed.job_id, state="uploading", percent=25, owner_id="w1", attempt=1) is True

    class RefusingStopAdapter:
        def verify_session_stopped(self, session_name):
            return {
                "verified_stopped": False,
                "cleanup_status": "failed_retry_needed",
                "attempts": 3,
                "error": "500 Provider refused stop with token AIzaSySecretKey1234567890",
            }

    cancelled = store.cancel_job(claimed.job_id, adapter=RefusingStopAdapter())
    assert cancelled is False

    updated = store.get_job(claimed.job_id)
    assert updated.state == "cancelling"
    assert updated.cleanup_status == "failed_retry_needed"
    assert updated.cleanup_attempts == 3
    assert "AIzaSySecretKey1234567890" not in (updated.cleanup_last_error or "")

    pending = store.list_pending_cleanup_events()
    assert len(pending) == 1
    assert pending[0]["session_name"] == "cjobfailclean1a1"
    assert pending[0]["status"] == "failed_retry_needed"
    assert "AIzaSySecretKey1234567890" not in (pending[0]["last_error"] or "")


def test_scoped_stop_session_never_calls_global_orphan_wipe():
    called_cmds: list[list[str]] = []

    def fake_colab_argv(colab_args, **kwargs):
        called_cmds.append(list(colab_args))
        if colab_args[:1] == ["stop"]:
            return 0, "Session stopped", ""
        if colab_args[:1] == ["sessions"]:
            # Unrelated session cjob2222a1 stays alive; target cjob1111a1 is gone
            return 0, "Active sessions:\n  cjob2222a1 | T4\n", ""
        return 0, "", ""

    with (
        patch.object(colab_bridge, "_run_wsl_colab_argv", side_effect=fake_colab_argv),
        patch.object(
            colab_bridge,
            "_run_wsl_argv",
            side_effect=AssertionError("stop_session must never run a global assignment cleanup script"),
        ),
        patch.object(
            colab_bridge,
            "stop_all_sessions",
            side_effect=AssertionError("stop_session must never call stop_all_sessions"),
        ),
    ):
        res = colab_bridge.verify_session_stopped("cjob1111a1", max_retries=2, retry_delay=0.01)

    assert res["verified_stopped"] is True
    assert res["cleanup_status"] == "verified"
    assert ["stop", "-s", "cjob1111a1"] in called_cmds
    assert all("cjob2222a1" not in cmd for cmd in called_cmds)


def test_stop_verification_recognizes_real_colab_alias_format(monkeypatch):
    output = "[cjob27e4e9f3a1] gpu-t4-s-ass1a0-1r7rpvcy7pcp3 | Hardware: T4 | Shape: Standard | Variant: GPU"
    monkeypatch.setattr(colab_bridge, "stop_session", lambda _name: (True, "stop accepted"))
    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", lambda *a, **k: (0, output, ""))
    sessions = colab_bridge.get_sessions_status()["sessions"]
    assert sessions[0]["name"] == "cjob27e4e9f3a1"
    result = colab_bridge.verify_session_stopped("cjob27e4e9f3a1", max_retries=1)
    assert result["verified_stopped"] is False
    assert result["cleanup_status"] == "failed_retry_needed"


def test_startup_recover_and_lease_reconciliation(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_concurrency_limit(3)
    video = _make_sample_video(tmp_path)

    # Job 1: orphaned session with active session on provider -> stopped and marked failed
    j1 = store.enqueue(str(video), video_name="j1.mp4")
    c1 = store.claim_next(owner_id="dead-w", lease_seconds=5.0)
    assert store.register_session(c1.job_id, c1.attempt, "dead-w", "cjoborph1a1") is True

    # Job 2: was cancelling when crashed -> verified stopped and marked cancelled
    j2 = store.enqueue(str(video), video_name="j2.mp4")
    c2 = store.claim_next(owner_id="dead-w", lease_seconds=5.0)
    assert store.register_session(c2.job_id, c2.attempt, "dead-w", "cjoborph2a1") is True

    # Claim the third lane before cancellation creates real pending cleanup debt.
    # Job 3: no session created yet -> marked failed without touching provider
    j3 = store.enqueue(str(video), video_name="j3.mp4")
    c3 = store.claim_next(owner_id="dead-w", lease_seconds=5.0)
    assert c3 is not None
    store.request_cancel(c2.job_id)

    stopped_sessions: list[str] = []

    class RecoveryAdapter:
        def __init__(self, reachable: bool = True):
            self.reachable = reachable
            self.live = {"cjoborph1a1", "cjoborph2a1"}

        def get_sessions_status(self):
            if not self.reachable:
                return {"ok": False, "sessions": [], "error": "WSL unreachable"}
            return {"ok": True, "sessions": [{"name": s, "is_active": True} for s in self.live], "error": ""}

        def verify_session_stopped(self, session_name):
            if not self.reachable:
                return {
                    "verified_stopped": False,
                    "cleanup_status": "provider_unreachable",
                    "attempts": 1,
                    "error": "WSL unreachable",
                }
            stopped_sessions.append(session_name)
            self.live.discard(session_name)
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    summary = store.recover(adapter=RecoveryAdapter(reachable=True))
    assert len(summary) == 3
    assert set(stopped_sessions) == {"cjoborph1a1", "cjoborph2a1"}
    assert store.get_job(j1.job_id).state == "failed"
    assert store.get_job(j2.job_id).state == "cancelled"
    assert store.get_job(j3.job_id).state == "failed"

    # Lease reconciliation must NOT blindly kill an old (>3h) job with fresh lease
    j4 = store.enqueue(str(video), video_name="j4.mp4")
    c4 = store.claim_next(owner_id="live-w", lease_seconds=120.0)
    assert store.register_session(c4.job_id, c4.attempt, "live-w", "cjobfresh1a1") is True
    with store._connect() as db:
        db.execute(
            "UPDATE cloud_jobs SET created_at = ? WHERE job_id = ?",
            (time.time() - 20000.0, c4.job_id),
        )
    expired_Actions = store.reconcile_expired_leases(adapter=RecoveryAdapter(reachable=True))
    assert expired_Actions == []
    assert store.get_job(c4.job_id).state == "provisioning"

    # Provider unreachable on startup recovery does NOT falsely mark cleanup verified
    offline_actions = store.recover(adapter=RecoveryAdapter(reachable=False))
    assert any(a["cleanup_status"] == "provider_unreachable" for a in offline_actions)
    assert store.get_job(c4.job_id).cleanup_status == "provider_unreachable"
    assert len(store.list_pending_cleanup_events()) >= 1


def test_worker_honest_stop_status_and_drain(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    j1 = store.enqueue(str(video), video_name="first.mp4")

    entered_job = threading.Event()
    release_job = threading.Event()
    processed_ids: list[str] = []

    class SlowOrchestrator:
        def __init__(self, st):
            self.store = st
            self.adapter = None

        def process_job(self, job, **kwargs):
            processed_ids.append(job.job_id)
            entered_job.set()
            release_job.wait(timeout=5.0)
            self.store.update_progress(
                job.job_id,
                state="uploading",
                percent=20,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            self.store.update_progress(
                job.job_id,
                state="preparing",
                percent=40,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            self.store.update_progress(
                job.job_id,
                state="processing",
                percent=60,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            self.store.update_progress(
                job.job_id,
                state="downloading",
                percent=80,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            self.store.update_progress(
                job.job_id,
                state="validating",
                percent=95,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            self.store.update_progress(
                job.job_id,
                state="completed",
                percent=100,
                owner_id=job.owner_id,
                attempt=job.attempt,
            )
            return True

    worker = CloudWorker(
        poll_interval=0.05,
        heartbeat_interval=0.05,
        lease_seconds=10.0,
        store=store,
        orchestrator=SlowOrchestrator(store),
    )
    worker.start()
    try:
        assert entered_job.wait(timeout=3.0)

        # Short timeout while job still blocked must honestly report stopping / still alive
        stop_info_1 = worker.stop(timeout=0.05, cancel_current=False)
        assert stop_info_1["stopped"] is False
        assert stop_info_1["worker_running"] is True
        assert stop_info_1["worker_state"] == "stopping"
        assert stop_info_1["accepting_jobs"] is False

        # Enqueue second job while stopping: worker must NOT claim it
        j2 = store.enqueue(str(video), video_name="second.mp4")
        release_job.set()

        stop_info_2 = worker.stop(timeout=3.0, cancel_current=False)
        assert stop_info_2["stopped"] is True
        assert stop_info_2["worker_running"] is False
        assert stop_info_2["worker_state"] == "stopped"
        assert processed_ids == [j1.job_id]
        assert store.get_job(j2.job_id).state == "queued"
    finally:
        release_job.set()
        worker.stop(timeout=2.0)


def test_preflight_blocks_new_session_when_fresh_balance_cannot_cover_all_holds(tmp_path, monkeypatch):
    store = CloudJobStore(tmp_path / "aggregate_preflight.db", rollout_controlled=False)
    store.set_concurrency_limit(2)
    video = _make_sample_video(tmp_path)
    jobs = [store.enqueue(str(video), cu_estimated=0.4, cu_buffer=0.0) for _ in range(2)]
    claimed = [store.claim_job(job.job_id, f"owner-{index}", provider_usage={"ok": True, "balance": 2.0})
               for index, job in enumerate(jobs)]
    assert all(claimed)

    class NoCreateAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 0.6}

        def create_session(self, *args, **kwargs):
            raise AssertionError("aggregate CU gate must reject before creating a GPU session")

    orchestrator = CloudOrchestrator(store=store, adapter=NoCreateAdapter(), allowed_roots=[tmp_path])
    monkeypatch.setattr(orchestrator, "verify_local_preflight", lambda _input: {})
    assert orchestrator.process_job(claimed[0], owner_id=claimed[0].owner_id) is False
    rejected = store.get_job(jobs[0].job_id)
    assert rejected.state == "failed"
    assert rejected.error_code == "INSUFFICIENT_CU"
    assert store.get_job(jobs[1].job_id).state == "provisioning"
