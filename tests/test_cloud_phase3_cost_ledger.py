"""
tests/test_cloud_phase3_cost_ledger.py - Phase 3 audit-grade cost ledger & budget tests (0 CU / offline).
"""

from __future__ import annotations

import datetime as dt
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.cloud import cloud_routes
from backend.cloud.cloud_config import DEFAULT_CONFIG
from backend.cloud.cloud_contract import (
    CloudCostMeasurementSource,
    CloudErrorCode,
    CloudLedgerStatus,
    GPU_CU_RATES,
    admit_and_enqueue_job,
)
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.cloud_security import CloudValidationError, resolve_local_control_token


def _make_sample_video(tmp_path: Path, name: str = "sample.mp4") -> Path:
    video = tmp_path / name
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    return video


def _advance_states(
    store: CloudJobStore,
    job_id: str,
    owner_id: str,
    attempt: int,
    states: tuple[tuple[str, int], ...],
) -> None:
    for st, pct in states:
        assert (
            store.update_progress(
                job_id,
                state=st,
                percent=pct,
                owner_id=owner_id,
                attempt=attempt,
            )
            is True
        )


def test_failed_job_retains_spent_cu_in_ledger_and_daily_budget(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(3.0)
    video = _make_sample_video(tmp_path)

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.5)
    claimed = store.claim_job(job.job_id, owner_id="worker-1")
    assert claimed is not None

    assert store.register_session(claimed.job_id, claimed.attempt, "worker-1", "cjobfail3cu") is True
    _advance_states(
        store,
        claimed.job_id,
        "worker-1",
        claimed.attempt,
        (("uploading", 20), ("preparing", 40), ("processing", 60)),
    )
    assert (
        store.update_progress(
            claimed.job_id,
            state="failed",
            percent=60,
            cu_actual=3.0,
            cleanup_status="verified",
            error="OOM crash after partial billing",
            error_code=CloudErrorCode.EXEC_FAILED.value,
            owner_id="worker-1",
            attempt=claimed.attempt,
        )
        is True
    )

    assert store.get_cu_spent_today() == pytest.approx(3.0, abs=1e-3)
    budget = store.get_cu_budget_info()
    assert budget["spent_today"] == pytest.approx(3.0, abs=1e-3)
    assert budget["committed_today"] == pytest.approx(3.0, abs=1e-3)
    assert budget["remaining_today"] == 0.0
    assert budget["is_exceeded"] is True

    ledger = store.list_cost_ledger(job_id=claimed.job_id)
    assert len(ledger) == 1
    assert ledger[0]["status"] == CloudLedgerStatus.RECONCILED.value
    assert ledger[0]["job_state"] == "failed"
    assert ledger[0]["cu_observed"] == pytest.approx(3.0, abs=1e-6)
    assert ledger[0]["cu_reconciled"] == pytest.approx(3.0, abs=1e-6)

    with pytest.raises(CloudValidationError) as exc_info:
        admit_and_enqueue_job(
            store,
            video_path=str(video),
            allowed_roots=[tmp_path],
        )
    assert exc_info.value.code == CloudErrorCode.BUDGET_EXCEEDED.value


def test_cancelled_and_deleted_job_preserves_cost_ledger(tmp_path: Path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=1.0)
    claimed = store.claim_job(job.job_id, owner_id="w1")
    assert claimed is not None
    assert store.register_session(claimed.job_id, claimed.attempt, "w1", "cjobcandela1") is True

    # Active jobs cannot be hidden/deleted from UI
    assert store.delete_job(claimed.job_id) is False

    store.record_runtime_usage(claimed.job_id, claimed.attempt, 1.25)
    cancel_req = store.request_cancel(claimed.job_id)
    assert cancel_req["cancelled"] is True

    # Uncleaned job cannot be hidden/deleted from UI
    store.record_cleanup_result(
        claimed.job_id,
        claimed.attempt,
        "cjobcandela1",
        verified_stopped=False,
        cleanup_status="failed_retry_needed",
        attempts=1,
        error="still running",
    )
    assert store.delete_job(claimed.job_id) is False

    # Verify session stopped -> mark cancelled -> now soft-hide succeeds while keeping ledger
    store.record_cleanup_result(
        claimed.job_id,
        claimed.attempt,
        "cjobcandela1",
        verified_stopped=True,
        cleanup_status="verified",
        attempts=1,
    )
    assert (
        store.update_progress(
            claimed.job_id,
            state="cancelled",
            percent=0,
            cleanup_status="verified",
            owner_id="w1",
            attempt=claimed.attempt,
            allow_from_cancel=True,
        )
        is True
    )

    monkeypatch.setattr(cloud_routes, "store", store)
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app)
    headers = {"X-Local-Control-Token": resolve_local_control_token()}

    del_resp = client.post("/api/cloud/job/delete", json={"job_id": claimed.job_id}, headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Hidden from dashboard job listing
    assert store.list_jobs() == []
    jobs_resp = client.get("/api/cloud/jobs")
    assert jobs_resp.status_code == 200
    assert jobs_resp.json()["jobs"] == []

    # Cost ledger and daily spend remain intact!
    assert store.get_cu_spent_today() == pytest.approx(1.25, abs=1e-3)
    ledger_resp = client.get("/api/cloud/ledger")
    assert ledger_resp.status_code == 200
    payload = ledger_resp.json()
    assert payload["budget"]["spent_today"] == pytest.approx(1.25, abs=1e-3)
    assert len(payload["ledger"]) == 1
    assert payload["ledger"][0]["job_id"] == claimed.job_id
    assert payload["ledger"][0]["cu_reconciled"] == pytest.approx(1.25, abs=1e-6)
    assert payload["ledger"][0]["is_estimated"] is True


def test_concurrent_workers_and_admissions_cannot_overspend_remaining_budget(tmp_path: Path):
    # Part A: Two workers racing claim_next() for last 1.0 CU budget
    store_a = CloudJobStore(db_path=tmp_path / "race_workers.db", rollout_controlled=False)
    store_a.set_cu_budget_daily(1.0)
    video = _make_sample_video(tmp_path)

    store_a.enqueue(str(video), video_name="j1.mp4", cu_estimated=0.5, cu_buffer=0.04)
    store_a.enqueue(str(video), video_name="j2.mp4", cu_estimated=0.5, cu_buffer=0.04)

    barrier_a = threading.Barrier(2)

    def _claim_worker(name: str):
        barrier_a.wait(timeout=5.0)
        return store_a.claim_next(owner_id=name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_claim_worker, "worker-a")
        f2 = pool.submit(_claim_worker, "worker-b")
        r1, r2 = f1.result(timeout=5.0), f2.result(timeout=5.0)

    winners = [r for r in (r1, r2) if r is not None]
    assert len(winners) == 1
    assert store_a.get_cu_budget_info()["committed_today"] <= 1.0 + 1e-6

    # Part B: Two direct admissions racing admit_and_enqueue_job(claim_immediately=True)
    store_b = CloudJobStore(db_path=tmp_path / "race_admit.db", rollout_controlled=False)
    store_b.set_cu_budget_daily(1.0)
    barrier_b = threading.Barrier(2)

    def _admit_direct(idx: int):
        barrier_b.wait(timeout=5.0)
        return admit_and_enqueue_job(
            store_b,
            video_path=str(video),
            source_channel="telegram",
            source_job_id=f"tg-{idx}",
            cu_estimated=0.5,
            cu_buffer=0.04,
            allowed_roots=[tmp_path],
            claim_immediately=True,
            owner_id=f"telegram:tg-{idx}",
        )

    successes = []
    rejections = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_admit_direct, 1), pool.submit(_admit_direct, 2)]
        for fut in futures:
            try:
                successes.append(fut.result(timeout=5.0))
            except CloudValidationError as exc:
                rejections.append(exc)

    assert len(successes) == 1
    assert len(rejections) == 1
    assert rejections[0].code in (
        CloudErrorCode.BUDGET_EXCEEDED.value,
        CloudErrorCode.CONFLICT_ALREADY_CLAIMED.value,
    )
    assert store_b.get_cu_budget_info()["committed_today"] <= 1.0 + 1e-6


def test_retry_accumulates_ledger_across_attempts(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)
    job = store.enqueue(str(video), video_name=video.name, cu_estimated=0.4, cu_buffer=0.04)

    class OkCleanupAdapter:
        def verify_session_stopped(self, session_name):
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    c1 = store.claim_job(job.job_id, owner_id="w1", lease_seconds=5.0)
    assert c1 is not None and c1.attempt == 1
    assert store.register_session(c1.job_id, 1, "w1", "cjobretry1a1") is True
    store.record_runtime_usage(c1.job_id, 1, 0.30)

    # Simulate lease expiry
    with store._connect() as db:
        db.execute(
            "UPDATE cloud_jobs SET lease_expires_at = ? WHERE job_id = ?",
            (time.time() - 10.0, c1.job_id),
        )

    assert store.release_expired_lease_for_retry(c1.job_id, adapter=OkCleanupAdapter()) is True
    assert store.get_cu_spent_today() == pytest.approx(0.30, abs=1e-3)

    c2 = store.claim_next(owner_id="w2")
    assert c2 is not None and c2.attempt == 2
    _advance_states(
        store,
        c2.job_id,
        "w2",
        2,
        (
            ("uploading", 20),
            ("preparing", 40),
            ("processing", 60),
            ("downloading", 80),
            ("validating", 95),
        ),
    )
    assert (
        store.update_progress(
            c2.job_id,
            state="completed",
            percent=100,
            cu_actual=0.50,
            cleanup_status="verified",
            owner_id="w2",
            attempt=2,
        )
        is True
    )

    ledger = store.list_cost_ledger(job_id=job.job_id)
    assert len(ledger) == 2
    assert [row["attempt"] for row in ledger] == [1, 2]
    assert ledger[0]["cu_reconciled"] == pytest.approx(0.30, abs=1e-6)
    assert ledger[1]["cu_reconciled"] == pytest.approx(0.50, abs=1e-6)
    assert store.get_cu_spent_today() == pytest.approx(0.80, abs=1e-3)


def test_midnight_crossing_allocates_usage_pro_rata_by_active_window(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)

    tz_ict = dt.timezone(dt.timedelta(hours=7))
    created_earlier = dt.datetime(2026, 11, 13, 10, 0, 0, tzinfo=tz_ict).timestamp()
    start_ts = dt.datetime(2026, 11, 14, 23, 30, 0, tzinfo=tz_ict).timestamp()
    end_ts = dt.datetime(2026, 11, 15, 0, 30, 0, tzinfo=tz_ict).timestamp()
    day1_query = dt.datetime(2026, 11, 14, 12, 0, 0, tzinfo=tz_ict).timestamp()
    day2_query = dt.datetime(2026, 11, 15, 12, 0, 0, tzinfo=tz_ict).timestamp()

    job = store.enqueue(str(video), video_name=video.name, cu_estimated=2.0)
    claimed = store.claim_job(job.job_id, owner_id="w1")
    assert claimed is not None

    with store._connect() as db:
        db.execute(
            "UPDATE cloud_jobs SET created_at = ?, started_at = ? WHERE job_id = ?",
            (created_earlier, start_ts, claimed.job_id),
        )
        db.execute(
            """
            UPDATE cloud_cost_ledger
            SET status = 'reconciled',
                job_state = 'completed',
                cleanup_verified = 1,
                cu_observed = 2.0,
                cu_reconciled = 2.0,
                usage_started_at = ?,
                usage_ended_at = ?
            WHERE job_id = ? AND attempt = ?
            """,
            (start_ts, end_ts, claimed.job_id, claimed.attempt),
        )

    assert store.get_cu_spent_today(now=day1_query) == pytest.approx(1.0, abs=1e-3)
    assert store.get_cu_spent_today(now=day2_query) == pytest.approx(1.0, abs=1e-3)


def test_stale_or_disconnected_provider_blocks_new_admission_and_orchestrator(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    video = _make_sample_video(tmp_path)

    # 1. Disconnected snapshot blocks admission
    store.record_provider_usage_snapshot({"ok": False, "error": "WSL offline"})
    with pytest.raises(CloudValidationError) as exc1:
        admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path])
    assert exc1.value.code == CloudErrorCode.BUDGET_UNCERTAIN.value

    # 2. Stale snapshot blocks admission
    store.record_provider_usage_snapshot(
        {"ok": True, "balance": 50.0, "observed_at": time.time() - 900.0}
    )
    with pytest.raises(CloudValidationError) as exc2:
        admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path])
    assert exc2.value.code == CloudErrorCode.BUDGET_UNCERTAIN.value

    # 3. Orchestrator fails fast before create_session if adapter returns stale/disconnected
    fresh_store = CloudJobStore(db_path=tmp_path / "orch.db", rollout_controlled=False)
    enqueued = fresh_store.enqueue(str(video), video_name=video.name)
    claimed = fresh_store.claim_job(enqueued.job_id, owner_id="w1")
    assert claimed is not None

    calls: list[str] = []

    class StaleAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 80.0, "stale": True}

        def create_session(self, session_name, gpu="T4"):
            calls.append(session_name)
            return True, "created"

    orch = CloudOrchestrator(store=fresh_store, adapter=StaleAdapter(), allowed_roots=[tmp_path])
    assert orch.process_job(claimed, owner_id="w1") is False
    assert calls == []
    failed = fresh_store.get_job(claimed.job_id)
    assert failed.state == "failed"
    assert failed.error_code == CloudErrorCode.BUDGET_UNCERTAIN.value


def test_runtime_budget_exceeded_aborts_job_stops_session_and_records_ledger(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(0.6)
    video = _make_sample_video(tmp_path)

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.4, cu_buffer=0.04)
    claimed = store.claim_job(job.job_id, owner_id="w-rt")
    assert claimed is not None

    stopped: list[str] = []

    class SlowBudgetAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0}

        def create_session(self, session_name, gpu="T4"):
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None):
            return True, "uploaded"

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None, cancel_check=None):
            time.sleep(0.03)
            if cancel_check and cancel_check():
                return False, "aborted_by_budget"
            return True, "ok"

        def verify_session_stopped(self, session_name):
            stopped.append(session_name)
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1, "error": None}

    with patch.dict(DEFAULT_CONFIG, {"runtime_budget_check_interval_seconds": 0.01}), patch.dict(
        GPU_CU_RATES, {"T4": 360000.0}
    ):
        orch = CloudOrchestrator(store=store, adapter=SlowBudgetAdapter(), allowed_roots=[tmp_path])
        ok = orch.process_job(claimed, owner_id="w-rt")

    assert ok is False
    assert len(stopped) == 1
    finished = store.get_job(claimed.job_id)
    assert finished.state == "failed"
    assert finished.error_code == CloudErrorCode.BUDGET_EXCEEDED.value
    assert finished.cleanup_status == "verified"

    ledger = store.list_cost_ledger(job_id=claimed.job_id)
    assert len(ledger) == 1
    assert ledger[0]["status"] == CloudLedgerStatus.RECONCILED.value
    assert ledger[0]["cu_observed"] > 0.0
    assert ledger[0]["measurement_source"] == CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value


def test_reservation_only_released_after_cleanup_verification_and_ledger_route(tmp_path: Path):
    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    store.set_cu_budget_daily(2.0)
    video = _make_sample_video(tmp_path)

    job = store.enqueue(str(video), video_name=video.name, gpu_type="T4", cu_estimated=0.5, cu_buffer=0.2)
    claimed = store.claim_job(job.job_id, owner_id="w1")
    assert claimed is not None
    assert store.register_session(claimed.job_id, claimed.attempt, "w1", "cjobhold1a1") is True
    store.record_runtime_usage(claimed.job_id, claimed.attempt, 0.1)

    # Cleanup fails -> job stays unreconciled & hold remains locked!
    store.request_cancel(claimed.job_id)
    store.record_cleanup_result(
        claimed.job_id,
        claimed.attempt,
        "cjobhold1a1",
        verified_stopped=False,
        cleanup_status="failed_retry_needed",
        attempts=2,
        error="provider refused stop",
    )
    store.update_progress(
        claimed.job_id,
        state="cancelling",
        cleanup_status="failed_retry_needed",
        allow_from_cancel=True,
    )

    unclean_budget = store.get_cu_budget_info()
    assert unclean_budget["unreconciled_count"] == 1
    assert unclean_budget["has_uncertain_budget"] is True
    assert unclean_budget["reserved_today"] == pytest.approx(0.60, abs=1e-3)
    assert unclean_budget["committed_today"] == pytest.approx(0.70, abs=1e-3)

    with pytest.raises(CloudValidationError) as exc_info:
        admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path])
    assert exc_info.value.code == CloudErrorCode.BUDGET_UNCERTAIN.value

    # Once cleanup verified -> unused reservation hold released, spent remains 0.10 CU
    store.record_cleanup_result(
        claimed.job_id,
        claimed.attempt,
        "cjobhold1a1",
        verified_stopped=True,
        cleanup_status="verified",
        attempts=1,
    )
    assert (
        store.update_progress(
            claimed.job_id,
            state="cancelled",
            cleanup_status="verified",
            allow_from_cancel=True,
        )
        is True
    )

    clean_budget = store.get_cu_budget_info()
    assert clean_budget["unreconciled_count"] == 0
    assert clean_budget["reserved_today"] == 0.0
    assert clean_budget["spent_today"] == pytest.approx(0.10, abs=1e-3)
    assert clean_budget["remaining_today"] == pytest.approx(1.90, abs=1e-3)

    # Admission succeeds again now that hold is reconciled
    next_job, _ = admit_and_enqueue_job(store, video_path=str(video), allowed_roots=[tmp_path])
    assert next_job.job_id != claimed.job_id
