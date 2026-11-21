"""
Phase 6 isolated offline tests (0 CU):
- Non-blocking provider status probe & cancel when Colab hangs 30s
- Bounded ThreadPoolExecutor + single-flight TTL cache under concurrent dashboard polling
- Read-only /api/cloud/status never spawns CloudWorker, sessions, or DB rows
- Idempotency key deduplication across REST /api/cloud/enqueue, budget ledger, and Telegram retry
- Per-job execution_mode / gpu_type / options snapshot isolation when global flags toggle later
- Request auth/origin validation, payload type validation, and dashboard Phase 6 UI fields
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import sys
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from cloud import cloud_routes, cloud_worker  # noqa: E402
from cloud.cloud_config import WORKSPACE_DIR  # noqa: E402
from cloud.cloud_contract import (  # noqa: E402
    CloudErrorCode,
    admit_and_enqueue_job,
    get_error_remediation,
    reset_provider_status_cache,
)
from cloud.cloud_job_store import CloudJobStore  # noqa: E402
from main import LOCAL_TOKEN, app  # noqa: E402
import telegram_bot  # noqa: E402
from v1_feature_flags import get_feature_flags, set_feature_flags  # noqa: E402


AUTH_HEADERS = {"X-Local-Control-Token": LOCAL_TOKEN}


@pytest.fixture(autouse=True)
def _reset_cache_and_worker(monkeypatch):
    reset_provider_status_cache()
    old_worker = cloud_worker._WORKER_INSTANCE
    if old_worker is not None and old_worker.is_running():
        old_worker.stop(timeout=2.0)
    monkeypatch.setattr(cloud_worker, "_WORKER_INSTANCE", None)
    yield
    w = cloud_worker._WORKER_INSTANCE
    if w is not None and w.is_running():
        w.stop(timeout=2.0)
    reset_provider_status_cache()


def _make_dummy_video(directory: Path, name: str = "phase6_sample.mp4") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_phase6_test")
    return p


def test_hung_provider_status_and_cancel_stay_nonblocking(tmp_path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)

    hang_event = threading.Event()

    def hung_usage():
        hang_event.wait(timeout=30.0)
        return {"ok": True, "balance": 99.0, "usage_rate": 0.0, "active_assignments": 0}

    def hung_sessions():
        hang_event.wait(timeout=30.0)
        return []

    monkeypatch.setattr(cloud_routes, "get_usage", hung_usage)
    monkeypatch.setattr(cloud_routes, "get_sessions", hung_sessions)

    client = TestClient(app)
    try:
        t0 = time.monotonic()
        res = client.get("/api/cloud/status")
        elapsed = time.monotonic() - t0
        assert res.status_code == 200
        assert elapsed < 1.2, f"/api/cloud/status blocked too long: {elapsed:.2f}s"

        data = res.json()
        assert data["status"] == "ok"
        assert data["worker_running"] is False
        assert cloud_worker.peek_cloud_worker() is None
        assert data["provider_cache"]["stale"] is True
        assert data["provider_cache"]["provider_unreachable"] is True
        assert data["provider_cache"]["in_flight"] is True
        assert store.list_jobs() == []

        # Create an active job with a bound session, then simulate hanging adapter on cancel
        dummy_video = _make_dummy_video(WORKSPACE_DIR, f"p6_hang_{uuid.uuid4().hex[:6]}.mp4")
        try:
            job, _ = admit_and_enqueue_job(
                store,
                video_path=str(dummy_video),
                source_channel="dashboard",
                allowed_roots=[WORKSPACE_DIR],
                claim_immediately=True,
                owner_id="test-owner",
                enforce_budget=False,
            )
            assert store.register_session(job.job_id, attempt=1, owner_id="test-owner", session_name="p6_sess_1")

            class _HungAdapter:
                def stop_session(self, session_name):
                    hang_event.wait(timeout=30.0)
                    return True, "ok"

                def verify_session_stopped(self, session_name):
                    hang_event.wait(timeout=30.0)
                    return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1}

            monkeypatch.setattr(cloud_routes, "ColabProviderAdapter", lambda: _HungAdapter())
            monkeypatch.setattr(
                cloud_routes,
                "abort_session_processes",
                lambda s_name: hang_event.wait(timeout=30.0),
            )

            t1 = time.monotonic()
            res_cancel = client.post(
                "/api/cloud/job/cancel",
                json={"job_id": job.job_id},
                headers=AUTH_HEADERS,
            )
            cancel_elapsed = time.monotonic() - t1
            assert res_cancel.status_code == 200
            assert cancel_elapsed < 1.8, f"/api/cloud/job/cancel blocked too long: {cancel_elapsed:.2f}s"

            c_data = res_cancel.json()
            assert c_data["status"] == "ok"
            assert c_data["cancel_requested"] is True
            assert c_data["cancelled"] is False
            assert c_data["timed_out"] is True
            assert c_data["state"] in ("cancel_requested", "cancelling")
            assert len(store.list_pending_cleanup_events()) >= 1
        finally:
            dummy_video.unlink(missing_ok=True)
    finally:
        hang_event.set()
        time.sleep(0.05)
        reset_provider_status_cache()


def test_concurrent_polling_tabs_bounded_threads_and_single_flight(tmp_path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)

    call_counts = {"usage": 0, "sessions": 0}
    count_lock = threading.Lock()
    gate = threading.Event()

    def slow_usage():
        with count_lock:
            call_counts["usage"] += 1
        gate.wait(timeout=0.35)
        return {"ok": True, "balance": 42.5, "usage_rate": 1.15, "active_assignments": 1}

    def slow_sessions():
        with count_lock:
            call_counts["sessions"] += 1
        gate.wait(timeout=0.35)
        return [{"name": "active_colab_1", "is_active": True}]

    monkeypatch.setattr(cloud_routes, "get_usage", slow_usage)
    monkeypatch.setattr(cloud_routes, "get_sessions", slow_sessions)

    client = TestClient(app)

    def _poll_once():
        return client.get("/api/cloud/status").json()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futs = [pool.submit(_poll_once) for _ in range(16)]
            results = [f.result(timeout=3.0) for f in futs]

        probe_threads = [
            t for t in threading.enumerate() if t.name.startswith("CloudProviderProbe")
        ]
        assert len(probe_threads) <= 4
        assert call_counts["usage"] == 1
        assert call_counts["sessions"] == 1
        assert all(r["status"] == "ok" for r in results)

        # Allow in-flight probe to finish, then confirm fresh cache hit without extra provider calls
        gate.set()
        time.sleep(0.15)
        fresh = client.get("/api/cloud/status").json()
        assert fresh["usage"]["balance"] == 42.5
        assert fresh["provider_cache"]["stale"] is False
        assert fresh["provider_cache"]["cached"] is True
        assert fresh["provider_cache"]["provider_unreachable"] is False
        assert call_counts["usage"] == 1
        assert call_counts["sessions"] == 1
    finally:
        gate.set()


def test_idempotency_key_deduplicates_rest_budget_and_telegram_delivery(tmp_path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(
        cloud_routes,
        "get_usage",
        lambda: {"ok": True, "balance": 50.0, "usage_rate": 0.0, "active_assignments": 0},
    )
    monkeypatch.setattr(cloud_routes, "get_sessions", lambda: [])

    class _NoopWorker:
        def is_running(self):
            return True

        def start(self):
            pass

        def get_current_job(self):
            return None

        def get_worker_state(self):
            return "running"

        def is_accepting_jobs(self):
            return True

        def stop(self, timeout=2.0, cancel_current=False):
            return {"worker_running": False}

    monkeypatch.setattr(cloud_worker, "_WORKER_INSTANCE", _NoopWorker())

    client = TestClient(app)
    dummy_video = _make_dummy_video(WORKSPACE_DIR, f"p6_idem_{uuid.uuid4().hex[:6]}.mp4")
    out_dir = tmp_path / "out_delivery"
    out_dir.mkdir(parents=True, exist_ok=True)
    delivered_file = out_dir / "cloud_phase6_result.mp4"
    delivered_file.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom_delivered")

    try:
        payload = {
            "video_path": str(dummy_video),
            "gpu_type": "T4",
            "voice": "BV562_streaming",
            "idempotency_key": "dash-retry-key-001",
            "options": {"execution_mode": "cloud", "allow_no_speech": True},
        }
        r1 = client.post("/api/cloud/enqueue", json=payload, headers=AUTH_HEADERS)
        r2 = client.post("/api/cloud/enqueue", json=payload, headers=AUTH_HEADERS)
        assert r1.status_code == 200
        assert r2.status_code == 200

        d1 = r1.json()
        d2 = r2.json()
        assert d1["deduplicated"] is False
        assert d2["deduplicated"] is True
        assert d1["job"]["job_id"] == d2["job"]["job_id"]
        assert d1["job"]["idempotency_key"] == "dash-retry-key-001"
        assert d1["job"]["options"]["allow_no_speech"] is True
        assert len(store.list_jobs()) == 1

        # Also test claim + budget reservation + Telegram dedup on retry
        tg_store = CloudJobStore(db_path=tmp_path / "tg_cloud.db", rollout_controlled=False)
        monkeypatch.setattr(telegram_bot, "WORKSPACE", str(tmp_path / "tg_ws"))
        monkeypatch.setattr(telegram_bot, "OUTPUT_DIR", str(out_dir))
        Path(telegram_bot.WORKSPACE).mkdir(parents=True, exist_ok=True)
        set_feature_flags({"V1_CLOUD_MODE": True, "V1_CLOUD_GPU": "T4"}, telegram_bot.WORKSPACE)

        from cloud import cloud_job_store as cjs_mod
        from cloud import cloud_orchestrator as co_mod

        monkeypatch.setattr(cjs_mod, "CloudJobStore", lambda *a, **kw: tg_store)

        orch_runs = {"count": 0}

        class _SpyOrchestrator:
            def __init__(self, s, allowed_roots=None):
                self.store = s

            def process_job(self, cjob, on_progress=None, **kwargs):
                orch_runs["count"] += 1
                self.store.update_progress(cjob.job_id, state="uploading", percent=25, step_name="Upload")
                self.store.update_progress(cjob.job_id, state="processing", percent=60, step_name="Process")
                self.store.update_progress(cjob.job_id, state="downloading", percent=85, step_name="Download")
                self.store.update_progress(cjob.job_id, state="validating", percent=95, step_name="Validate")
                self.store.update_progress(
                    cjob.job_id,
                    state="completed",
                    percent=100,
                    step_name="Hoàn tất & đạt QC",
                    result_path=str(delivered_file),
                    output_manifest={"delivery_path": str(delivered_file), "qc_passed": True},
                    cu_actual=0.25,
                )
                return True

        monkeypatch.setattr(co_mod, "CloudOrchestrator", _SpyOrchestrator)

        tg_job = telegram_bot.prepare_telegram_job(
            {
                "type": "local_file",
                "job_id": "tg_idem_job_1",
                "video_path": str(dummy_video),
                "filename": dummy_video.name,
                "voice": "BV562_streaming",
                "force_cloud": True,
                "idempotency_key": "tg:tg_idem_job_1",
            }
        )

        class _FakeMsg:
            async def edit_text(self, *a, **kw):
                pass

        res1 = asyncio.run(telegram_bot.run_pipeline_cloud_for_telegram(dummy_video, tg_job, _FakeMsg()))
        res2 = asyncio.run(telegram_bot.run_pipeline_cloud_for_telegram(dummy_video, tg_job, _FakeMsg()))

        assert orch_runs["count"] == 1
        assert len(tg_store.list_jobs()) == 1
        assert len(tg_store.list_cost_ledger()) == 1
        assert res1["already_delivered"] is False
        assert res2["deduplicated"] is True
        assert res2["already_delivered"] is True
    finally:
        dummy_video.unlink(missing_ok=True)


def test_execution_mode_and_options_snapshot_isolated_from_flag_toggles(tmp_path, monkeypatch):
    ws = tmp_path / "ws_mode"
    ws.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out_mode"
    out.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(telegram_bot, "WORKSPACE", str(ws))
    monkeypatch.setattr(telegram_bot, "OUTPUT_DIR", str(out))

    vid = _make_dummy_video(ws, "mode_iso.mp4")

    # This test checks snapshotted dispatch, using a header-only media fixture.
    from pipeline_v2 import download_validation
    monkeypatch.setattr(download_validation, "probe_downloaded_video", lambda *a, **kw: {})

    # Save baseline local mode + T4
    set_feature_flags({"V1_CLOUD_MODE": False, "V1_CLOUD_GPU": "T4"}, str(ws))
    local_job = telegram_bot.prepare_telegram_job(
        {
            "type": "local_file",
            "job_id": "tg_local_snap",
            "video_path": str(vid),
            "filename": vid.name,
            "pos": 1,
        }
    )
    assert local_job["execution_mode"] == "local"
    assert local_job["allow_cloud_fallback"] is False

    # A legacy global Cloud flag does not route new ordinary submissions to Cloud.
    set_feature_flags({"V1_CLOUD_MODE": True, "V1_CLOUD_GPU": "L4"}, str(ws))
    assert telegram_bot.prepare_telegram_job({"video_path": str(vid)})["execution_mode"] == "local"
    cloud_job = telegram_bot.prepare_telegram_job(
        {
            "type": "local_file",
            "job_id": "tg_cloud_snap",
            "video_path": str(vid),
            "filename": vid.name,
            "pos": 2,
            "force_cloud": True,
        }
    )
    assert cloud_job["execution_mode"] == "cloud"
    assert cloud_job["cloud_gpu"] == "L4"

    # Flip global flags back to OFF + A100 before execution
    set_feature_flags({"V1_CLOUD_MODE": False, "V1_CLOUD_GPU": "A100"}, str(ws))

    calls = {"local": 0, "cloud": 0}

    async def fake_local(video_path, job, status_msg):
        calls["local"] += 1
        # Use a terminal validation failure; this test covers routing, not Local retries.
        raise ValueError("local pipeline test error")

    async def fake_cloud(video_path, job, status_msg):
        calls["cloud"] += 1
        out_f = out / "Dubbed_cloud.mp4"
        out_f.write_bytes(b"ok")
        return {"status": "success", "final_video": str(out_f), "gpu_type": job.get("cloud_gpu"), "mode": "cloud"}

    monkeypatch.setattr(telegram_bot, "run_pipeline_v1_for_telegram", fake_local)
    monkeypatch.setattr(telegram_bot, "run_pipeline_cloud_for_telegram", fake_cloud)

    # Local snapshotted job MUST run local and fail locally without auto-fallback to cloud
    r_local = asyncio.run(telegram_bot._process_telegram_job(local_job))
    assert calls["local"] == 1
    assert calls["cloud"] == 0
    assert r_local["status"] == "failed"

    # Cloud snapshotted job MUST still run cloud with snapshotted L4 despite global flags now False / A100
    r_cloud = asyncio.run(telegram_bot._process_telegram_job(cloud_job))
    assert calls["cloud"] == 1
    assert r_cloud["gpu_type"] == "L4"
    assert get_feature_flags(str(ws))["V1_CLOUD_MODE"] is False


def test_api_validation_auth_remediation_and_dashboard_surface(tmp_path, monkeypatch):
    store = CloudJobStore(db_path=tmp_path / "cloud_jobs.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)
    client = TestClient(app)
    dummy_video = _make_dummy_video(WORKSPACE_DIR, f"p6_val_{uuid.uuid4().hex[:6]}.mp4")

    try:
        # Missing/wrong token & disallowed Origin must return 403
        r_noauth = client.post("/api/cloud/enqueue", json={"video_path": str(dummy_video)})
        assert r_noauth.status_code == 403

        r_origin = client.post(
            "/api/cloud/enqueue",
            json={"video_path": str(dummy_video)},
            headers={**AUTH_HEADERS, "Origin": "https://evil.example.com"},
        )
        assert r_origin.status_code == 403

        # Invalid options / idempotency_key / execution_mode / non-string gpu must return 400
        bad_payloads = [
            {"video_path": str(dummy_video), "options": ["not-a-dict"]},
            {"video_path": str(dummy_video), "idempotency_key": "bad key; rm -rf /"},
            {"video_path": str(dummy_video), "options": {"execution_mode": "unknown"}},
            {"video_path": str(dummy_video), "gpu_type": 123},
        ]
        for bp in bad_payloads:
            r_bad = client.post("/api/cloud/enqueue", json=bp, headers=AUTH_HEADERS)
            assert r_bad.status_code == 400

        # Remediation mapping covers all canonical error codes
        for code in CloudErrorCode:
            assert get_error_remediation(code)

        # Dashboard HTML exposes Phase 6 status, cancel, remediation, cost, cleanup, and QC elements
        dash = client.get("/cloud")
        assert dash.status_code == 200
        html = dash.text
        for marker in (
            'id="session-note"',
            'data-filter="failed"',
            "cleanup-alert-container",
            "hero-alert-banner",
            "chk-allow-no-speech",
            "X-Idempotency-Key",
            "rollout_permit",
            "error_remediation",
            "Kiểm tra đầu ra đạt",
            "Chờ thu hồi",
        ):
            assert marker in html, f"Dashboard HTML missing Phase 6 marker: {marker}"
    finally:
        dummy_video.unlink(missing_ok=True)
