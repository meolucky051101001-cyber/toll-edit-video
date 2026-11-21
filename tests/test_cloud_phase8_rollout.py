from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.cloud import cloud_routes
from backend.cloud.cloud_contract import admit_and_enqueue_job, get_cloud_status_snapshot
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_security import CloudValidationError
from backend.cloud.cloud_worker import CloudWorker


VIDEO_URL = "https://www.tiktok.com/@sample/video/123456789"
FAKE_USAGE = {"ok": True, "balance": 10.0, "usage_rate": 0.0, "active_assignments": 0}


@pytest.mark.parametrize("probe_returncode", [0, 1])
def test_windows_runtime_fallback_requires_successful_workspace_and_dependency_probe(tmp_path, monkeypatch, probe_returncode):
    import run_cloud_node as entrypoint
    monkeypatch.delenv("AUTODUB_CLOUD_RUNTIME_FALLBACK", raising=False)
    monkeypatch.setattr(entrypoint, "_workspace_write_available", lambda _path: False)
    monkeypatch.setattr(entrypoint.shutil, "which", lambda _name: "py.exe")
    probe = Mock(return_value=SimpleNamespace(returncode=probe_returncode))
    child = Mock(return_value=0)
    monkeypatch.setattr(entrypoint.subprocess, "run", probe)
    monkeypatch.setattr(entrypoint.subprocess, "call", child)
    result = entrypoint._relaunch_with_writable_runtime(tmp_path)
    assert probe.call_args.args[0][:3] == ["py.exe", "-3", "-c"]
    assert probe.call_args.kwargs["env"]["AUTODUB_CLOUD_RUNTIME_FALLBACK"] == "1"
    if probe_returncode == 0:
        assert result == 0
        assert child.call_args.args[0][:2] == ["py.exe", "-3"]
        assert child.call_args.kwargs["env"]["AUTODUB_CLOUD_RUNTIME_FALLBACK"] == "1"
    else:
        assert result is None
        child.assert_not_called()


def test_standalone_cli_help_is_safe_under_windows_cp1252(tmp_path):
    import os
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONIOENCODING="cp1252:strict",
               AUTODUB_WORKSPACE=str(tmp_path / "workspace"),
               AUTODUB_CLOUD_OUTPUT_DIR=str(tmp_path / "output"),
               AUTODUB_LOCAL_TOKEN="offline-cli-placeholder-only")
    result = subprocess.run([sys.executable, str(root / "run_cloud_node.py"), "--help"],
                            cwd=root, env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert "mặc định" in result.stdout.decode("utf-8")
    assert b"--no-worker" in result.stdout
    assert b"UnicodeEncodeError" not in result.stderr


def test_permit_survives_restart_and_selects_one_job_across_worker_and_telegram(tmp_path):
    db_path = tmp_path / "cloud.db"
    legacy_store = CloudJobStore(db_path, rollout_controlled=False)
    legacy_job = legacy_store.enqueue(VIDEO_URL)
    dashboard, telegram = CloudJobStore(db_path), CloudJobStore(db_path)
    assert dashboard.get_rollout() == {"phase": "paused"}
    assert dashboard.claim_next() is None
    assert telegram.claim_job(legacy_job.job_id, "telegram") is None
    with pytest.raises(CloudValidationError, match="thủ công"):
        admit_and_enqueue_job(dashboard, video_path=VIDEO_URL)

    permit = dashboard.set_rollout(arm=True)["permit_id"]
    assert telegram.get_rollout()["permit_id"] == permit
    barrier = Barrier(2)

    def admit(store, channel, immediate):
        barrier.wait(timeout=5)
        try:
            return admit_and_enqueue_job(
                store, video_path=VIDEO_URL, source_channel=channel, rollout_permit=permit,
                claim_immediately=immediate, idempotency_key=f"{channel}:job",
            )
        except CloudValidationError as exc:
            assert exc.http_status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tries = (
            pool.submit(admit, dashboard, "dashboard", False),
            pool.submit(admit, telegram, "telegram", True),
        )
        [(job, contract)] = [r for f in tries if (r := f.result(timeout=10)) is not None]

    assert not contract.deduplicated
    assert telegram.get_rollout() == {"phase": "admitted", "job_id": job.job_id}
    if job.state == "queued":
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(s.claim_next, name)
                      for s, name in ((dashboard, "worker-a"), (telegram, "worker-b"))]
            [job] = [r for f in claims if (r := f.result(timeout=10)) is not None]
    assert job.state == "provisioning"
    assert len(dashboard.list_cost_ledger()) == 1
    assert dashboard.get_job(legacy_job.job_id).state == "queued"
    assert dashboard.claim_next() is None
    assert telegram.claim_job(legacy_job.job_id, "telegram") is None
    with pytest.raises(CloudValidationError, match="hoàn tất job"):
        telegram.set_rollout(arm=True)


def test_pause_stops_queued_claim_and_stale_permit_and_keeps_idempotency(tmp_path):
    db_path = tmp_path / "cloud.db"
    store = CloudJobStore(db_path)
    old_permit = store.set_rollout(arm=True)["permit_id"]
    job, contract = admit_and_enqueue_job(
        store, video_path=VIDEO_URL, rollout_permit=old_permit,
        idempotency_key="dashboard:retry", deadline_seconds=60,
    )
    assert job.deadline_at == job.created_at + 60
    store.set_rollout(arm=False)
    reopened = CloudJobStore(db_path)
    assert reopened.get_rollout() == {"phase": "paused"}
    assert reopened.claim_next() is None
    assert reopened.claim_job(job.job_id, "telegram") is None
    assert reopened.request_cancel(job.job_id)["immediate"]
    new_permit = reopened.set_rollout(arm=True)["permit_id"]
    assert new_permit != old_permit
    with pytest.raises(CloudValidationError):
        admit_and_enqueue_job(reopened, video_path=VIDEO_URL, rollout_permit=old_permit)
    replay, retry = admit_and_enqueue_job(
        reopened, video_path=VIDEO_URL, rollout_permit=old_permit,
        idempotency_key="dashboard:retry", deadline_seconds=90,
    )
    assert replay.job_id == job.job_id and retry.deduplicated
    assert retry.deadline_utc == contract.deadline_utc
    assert reopened.get_rollout()["permit_id"] == new_permit
    assert reopened.list_rollout_alerts() == [
        {"code": "DUPLICATE_SUBMISSION", "job_id": job.job_id},
    ]


def test_sequential_rollout_permits_allow_two_active_jobs_then_stop_at_limit(tmp_path):
    store = CloudJobStore(tmp_path / "cloud.db")
    store.set_concurrency_limit(2)

    first_permit = store.set_rollout(arm=True)["permit_id"]
    first, _ = admit_and_enqueue_job(
        store, video_path=VIDEO_URL, rollout_permit=first_permit,
        idempotency_key="dashboard:first",
    )
    assert first.state == "queued"
    with pytest.raises(CloudValidationError):  # a queued permit cannot be replaced
        store.set_rollout(arm=True)
    first_claim = store.claim_next(job_id=first.job_id)
    assert first_claim is not None and first_claim.state == "provisioning"

    second_permit = store.set_rollout(arm=True)["permit_id"]
    assert second_permit != first_permit
    second, _ = admit_and_enqueue_job(
        store, video_path=VIDEO_URL, rollout_permit=second_permit,
        idempotency_key="dashboard:second",
    )
    second_claim = store.claim_next(job_id=second.job_id)
    assert second_claim is not None and second_claim.state == "provisioning"
    assert len(store.list_active_jobs()) == 2

    with pytest.raises(CloudValidationError, match="hoàn tất job"):
        store.set_rollout(arm=True)


def test_dashboard_rollback_scopes_cleanup_to_job_and_reports_unverified_resources(tmp_path, monkeypatch):
    store = CloudJobStore(tmp_path / "cloud.db")
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "phase8-local-control")
    monkeypatch.setattr(cloud_routes, "get_usage", lambda: FAKE_USAGE)
    monkeypatch.setattr(cloud_routes, "get_sessions", lambda: [])
    worker = Mock()
    worker.is_running.return_value = False
    monkeypatch.setattr(cloud_routes, "get_cloud_worker", lambda: worker)
    monkeypatch.setattr(cloud_routes, "peek_cloud_worker", lambda: None)
    stopped = []
    provider_sessions = {"independent-session", "owned-session"}

    def verify_stop(session):
        stopped.append(session)
        if len(stopped) == 1:
            return {"verified_stopped": False, "cleanup_status": "failed_retry_needed", "error": "provider unavailable"}
        provider_sessions.remove(session)
        return {"verified_stopped": True, "cleanup_status": "verified"}

    monkeypatch.setattr(cloud_routes, "ColabProviderAdapter", lambda: SimpleNamespace(verify_session_stopped=verify_stop))
    monkeypatch.setattr(cloud_routes, "abort_session_processes", lambda _: None)
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000))
    headers = {"X-Local-Control-Token": "phase8-local-control"}

    def post(route, **body):
        return client.post(f"/api/cloud/{route}", json=body, headers=headers)

    assert client.post("/api/cloud/rollout", json={"action": "arm"}).status_code == 403
    assert post("enqueue", url=VIDEO_URL).status_code == 409
    worker.start.assert_not_called()
    permit = post("rollout", action="arm").json()["rollout"]["permit_id"]
    response = post("enqueue", url=VIDEO_URL, rollout_permit=permit)
    assert response.status_code == 200
    job_id = response.json()["job"]["job_id"]
    worker.start.assert_called_once()
    claim = store.claim_next("worker")
    assert claim.job_id == job_id
    assert store.register_session(job_id, claim.attempt, claim.owner_id, "owned-session")
    assert post("rollout", action="pause").json()["rollout"] == {"phase": "paused"}
    assert post("job/cancel", job_id=job_id).json()["state"] == "cancelling"
    assert post("rollout", action="arm").status_code == 409
    status = client.get("/api/cloud/status").json()
    assert {"code": "CLEANUP_PENDING", "job_id": job_id} in status["rollout_alerts"]
    assert {"code": "LEDGER_UNRECONCILED", "job_id": job_id} in status["rollout_alerts"]
    assert post("job/cancel", job_id=job_id).json()["state"] == "cancelled"
    assert stopped == ["owned-session", "owned-session"]
    assert provider_sessions == {"independent-session"}
    assert store.list_pending_cleanup_events() == []
    assert store.get_cu_budget_info()["unreconciled_count"] == 0
    assert store.list_cost_ledger(job_id=job_id)[0]["cleanup_verified"]


def test_status_alerts_for_expired_job_qc_and_slow_status(tmp_path, monkeypatch):
    from backend.cloud import cloud_contract

    store = CloudJobStore(tmp_path / "cloud.db")
    permit = store.set_rollout(arm=True)["permit_id"]
    job, _ = admit_and_enqueue_job(
        store, video_path=VIDEO_URL, rollout_permit=permit, deadline_seconds=1,
        claim_immediately=True, owner_id="worker",
    )
    assert store.list_rollout_alerts(now=job.deadline_at + 1) == [
        {"code": "DEADLINE_EXCEEDED", "job_id": job.job_id}
    ]
    assert store.update_progress(job.job_id, state="failed", error_code="QC_FAILED")
    monkeypatch.setattr(cloud_contract, "fetch_provider_status_nonblocking",
                        lambda **_: {"usage": FAKE_USAGE, "sessions": [], "provider_cache": {}})
    ticks = iter((1.0, 2.1))
    monkeypatch.setattr(cloud_contract, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    snapshot = get_cloud_status_snapshot(store)
    assert snapshot["status_latency_ms"] == 1100
    assert snapshot["rollout_alerts"] == [
        {"code": "QC_FAILED", "job_id": job.job_id},
        {"code": "STATUS_SLOW"},
    ]


def test_worker_start_does_not_recover_a_live_telegram_lease(tmp_path):
    store = CloudJobStore(tmp_path / "cloud.db")
    permit = store.set_rollout(arm=True)["permit_id"]
    job, _ = admit_and_enqueue_job(
        store, video_path=VIDEO_URL, rollout_permit=permit, claim_immediately=True, owner_id="telegram",
    )
    adapter = Mock()
    worker = CloudWorker(store=store, adapter=adapter, poll_interval=0.05)
    worker.start()
    try:
        assert store.reconcile_expired_leases(adapter=adapter) == []
        assert store.get_job(job.job_id).owner_id == "telegram"
        assert store.get_job(job.job_id).state == "provisioning"
        assert adapter.mock_calls == []
    finally:
        worker.stop()


def test_crashed_runner_restart_recovers_only_its_registered_session(tmp_path):
    db_path = tmp_path / "cloud.db"
    store = CloudJobStore(db_path)
    permit = store.set_rollout(arm=True)["permit_id"]
    job, _ = admit_and_enqueue_job(store, video_path=VIDEO_URL, rollout_permit=permit)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from backend.cloud.cloud_job_store import CloudJobStore

store = CloudJobStore(sys.argv[1])
job = store.claim_next("crashed-runner", lease_seconds=5)
assert job and store.register_session(job.job_id, job.attempt, job.owner_id, "orphan-session")
os._exit(7)
""",
            str(db_path),
        ],
        check=False, capture_output=True, timeout=15,
    )
    assert child.returncode == 7, child.stderr.decode(errors="replace")

    restarted = CloudJobStore(db_path)
    restarted.set_rollout(arm=False)
    crashed = restarted.get_job(job.job_id)
    sessions = {"orphan-session", "independent-session"}
    stopped = []

    def verify_stop(name):
        stopped.append(name)
        sessions.remove(name)
        return {"verified_stopped": True, "cleanup_status": "verified"}

    adapter = SimpleNamespace(
        get_sessions=lambda: [{"name": name} for name in sessions],
        verify_session_stopped=verify_stop,
    )
    assert restarted.reconcile_expired_leases(adapter=adapter, now=crashed.lease_expires_at - 1) == []
    report = restarted.reconcile_expired_leases(adapter=adapter, now=crashed.lease_expires_at + 1)
    assert [(row["job_id"], row["cleanup_status"]) for row in report] == [(job.job_id, "verified")]
    assert restarted.get_job(job.job_id).state == "failed"
    assert stopped == ["orphan-session"]
    assert sessions == {"independent-session"}
    assert restarted.list_pending_cleanup_events() == []
    assert restarted.list_cost_ledger(job_id=job.job_id)[0]["cleanup_verified"]
    assert restarted.get_rollout() == {"phase": "paused"}
    assert restarted.claim_next() is None
