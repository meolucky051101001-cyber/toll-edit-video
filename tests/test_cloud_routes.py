"""
Kiểm thử tích hợp các endpoint Cloud GPU trên FastAPI TestClient.
"""
import sys
import time
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

import pytest
from fastapi.testclient import TestClient
from main import app, LOCAL_TOKEN
from cloud import cloud_routes, cloud_worker, colab_bridge
from cloud.cloud_contract import reset_provider_status_cache
from cloud.cloud_job_store import CloudJobStore

client = TestClient(app)
auth_headers = {"X-Local-Control-Token": LOCAL_TOKEN}


@pytest.fixture(autouse=True)
def _isolate_routes_store_and_provider(tmp_path, monkeypatch):
    """Cô lập hoàn toàn DB SQLite tạm, provider giả và vòng đời CloudWorker cho test_cloud_routes."""
    reset_provider_status_cache()
    old_worker = cloud_worker._WORKER_INSTANCE
    if old_worker is not None and old_worker.is_running():
        old_worker.stop(timeout=2.0)
    monkeypatch.setattr(cloud_worker, "_WORKER_INSTANCE", None)

    temp_store = CloudJobStore(db_path=tmp_path / "cloud_routes_test.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", temp_store)

    fake_usage = lambda: {"ok": True, "balance": 120.0, "usage_rate": 0.0, "active_assignments": 0}
    fake_sessions = lambda: []
    monkeypatch.setattr(cloud_routes, "get_usage", fake_usage)
    monkeypatch.setattr(cloud_routes, "get_sessions", fake_sessions)
    monkeypatch.setattr(colab_bridge, "get_usage", fake_usage)
    monkeypatch.setattr(colab_bridge, "get_sessions", fake_sessions)
    backend_colab = sys.modules.get("backend.cloud.colab_bridge")
    if backend_colab is not None:
        monkeypatch.setattr(backend_colab, "get_usage", fake_usage)
        monkeypatch.setattr(backend_colab, "get_sessions", fake_sessions)

    yield

    w = cloud_worker._WORKER_INSTANCE
    if w is not None and w.is_running():
        w.stop(timeout=2.0)
    reset_provider_status_cache()


def test_cloud_dashboard_html():
    response = client.get("/cloud")
    assert response.status_code == 200
    assert "Cloud GPU Dashboard" in response.text
    print("  -> GET /cloud: 200 OK")


def test_cloud_api_status():
    response = client.get("/api/cloud/status")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "usage" in data
    print("  -> GET /api/cloud/status: 200 OK")


def test_dashboard_stop_of_parked_session_ledgers_warm_idle(monkeypatch):
    store = cloud_routes.store
    session_name = "cjobroutewarm"
    parked_at = time.time() - 600
    store.park_warm_session(
        session_name,
        "T4",
        ttl_seconds=3600,
        source_job_id="route-source",
        now=parked_at,
    )
    monkeypatch.setattr(
        cloud_routes,
        "run_provider_action_nonblocking",
        lambda *_args, **_kwargs: {
            "timed_out": False,
            "completed": True,
            "result": (True, "stopped"),
        },
    )

    response = client.post(
        "/api/cloud/session/stop",
        headers=auth_headers,
        json={"session_name": session_name},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    rows = store.list_cost_ledger(job_id=f"warm:{session_name}")
    assert len(rows) == 1 and rows[0]["cu_observed"] > 0.0
    assert rows[0]["usage_started_at"] == parked_at


def test_cloud_api_jobs():
    response = client.get("/api/cloud/jobs")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert isinstance(data["jobs"], list)
    print("  -> GET /api/cloud/jobs: 200 OK")


def test_cloud_worker_controls():
    # Stop worker
    res_stop = client.post("/api/cloud/worker/stop", headers=auth_headers)
    assert res_stop.status_code == 200
    assert res_stop.json()["worker_running"] is False

    # Start worker
    res_start = client.post("/api/cloud/worker/start", headers=auth_headers)
    assert res_start.status_code == 200
    assert res_start.json()["worker_running"] is True

    # Stop again to keep clean
    client.post("/api/cloud/worker/stop", headers=auth_headers)
    print("  -> Worker start/stop: 200 OK")


def test_cloud_job_lifecycle():
    # Enqueue a dummy video inside allowed workspace root
    import uuid
    from cloud.cloud_config import WORKSPACE_DIR

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    dummy_video = WORKSPACE_DIR / f"test_route_dummy_{uuid.uuid4().hex[:6]}.mp4"
    dummy_video.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
    try:
        res_enq = client.post(
            "/api/cloud/enqueue",
            json={"video_path": str(dummy_video), "gpu_type": "T4", "voice": "BV562_streaming"},
            headers=auth_headers,
        )
        assert res_enq.status_code == 200
        job_id = res_enq.json()["job"]["job_id"]

        # Cancel job
        res_cancel = client.post("/api/cloud/job/cancel", json={"job_id": job_id}, headers=auth_headers)
        assert res_cancel.status_code == 200
        assert res_cancel.json()["status"] == "ok"

        # Delete job
        res_del = client.post("/api/cloud/job/delete", json={"job_id": job_id}, headers=auth_headers)
        assert res_del.status_code == 200
        assert res_del.json()["status"] == "ok"
    finally:
        client.post("/api/cloud/worker/stop", headers=auth_headers)
        dummy_video.unlink(missing_ok=True)
    print("  -> Job lifecycle (enqueue/cancel/delete): 200 OK")


def test_cloud_enqueue_url():
    # Test enqueueing with a direct video link URL
    test_url = "https://www.tiktok.com/@sample/video/123456789"
    res = client.post(
        "/api/cloud/enqueue",
        json={"video_path": test_url, "gpu_type": "T4", "voice": "BV562_streaming"},
        headers=auth_headers,
    )
    assert res.status_code == 200
    job = res.json()["job"]
    try:
        assert job["video_path"] == test_url
        assert "tiktok" in job["video_name"]
    finally:
        # Stop worker before cancel/delete so thread never outlives test DB
        client.post("/api/cloud/worker/stop", headers=auth_headers)
        client.post("/api/cloud/job/cancel", json={"job_id": job["job_id"]}, headers=auth_headers)
        client.post("/api/cloud/job/delete", json={"job_id": job["job_id"]}, headers=auth_headers)
    print("  -> Enqueue URL (TikTok/Douyin/YouTube): 200 OK")


if __name__ == "__main__":
    print("[*] Bắt đầu kiểm thử Cloud API Routes...")
    test_cloud_dashboard_html()
    test_cloud_api_status()
    test_cloud_api_jobs()
    test_cloud_worker_controls()
    print("\n[SUCCESS] Tất cả bài test Cloud API Routes đều đạt!")
