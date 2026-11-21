from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.cloud import cloud_routes
from backend.cloud.cloud_job_store import CloudJobStore


def test_concurrency_endpoint_persists_a_valid_shared_limit(tmp_path, monkeypatch):
    store = CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "cloud-test-token")
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000))
    headers = {"X-Local-Control-Token": "cloud-test-token"}

    assert store.get_concurrency_limit() == 1
    response = client.post("/api/cloud/concurrency", json={"max_concurrent_jobs": 3}, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "max_concurrent_jobs": 3}
    assert CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False).get_concurrency_limit() == 3

    for invalid in (0, 5, True, 2.0, "2"):
        response = client.post(
            "/api/cloud/concurrency", json={"max_concurrent_jobs": invalid}, headers=headers,
        )
        assert response.status_code == 400
    assert client.post("/api/cloud/concurrency", json={}, headers=headers).status_code == 400
    assert store.get_concurrency_limit() == 3


def test_concurrency_endpoint_requires_cloud_local_control_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_routes, "store", CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False))
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "cloud-test-token")
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000))

    response = client.post("/api/cloud/concurrency", json={"max_concurrent_jobs": 2})
    assert response.status_code == 403


def test_dashboard_gpu_settings_return_independent_limits(tmp_path, monkeypatch):
    store = CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False)
    store.set_gpu_concurrency_limits({"T4": 2, "L4": 3})
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "cloud-test-token")
    app = FastAPI()
    app.include_router(cloud_routes.router)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000)) as client:
        current = client.get("/api/cloud/gpu-settings").json()
        assert current["default_gpu"] == "T4"
        assert current["gpu_concurrency_limits"]["T4"] == 2
        assert current["gpu_concurrency_limits"]["L4"] == 3
        saved = client.post("/api/cloud/concurrency", json={"gpu_type": "L4", "max_concurrent_jobs": 4},
                            headers={"X-Local-Control-Token": "cloud-test-token"})
        assert saved.status_code == 200
        assert store.get_concurrency_limit("T4") == 2
        assert store.get_concurrency_limit("L4") == 4


def test_local_resources_reports_host_ram_and_shared_lanes_without_provider(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store = CloudJobStore(tmp_path / "local_resources.db", rollout_controlled=False)
    store.set_concurrency_limit(3)
    video = tmp_path / "sample.mp4"
    video.write_bytes(b"offline fixture")
    store.enqueue(str(video))
    assert store.claim_next("local-resource-test", enforce_budget=False) is not None
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(
        cloud_routes.psutil, "virtual_memory",
        lambda: SimpleNamespace(total=16 * 1024**3, available=6 * 1024**3),
    )

    def provider_forbidden(*args, **kwargs):
        raise AssertionError("Local RAM must not wait for or contact Colab")

    monkeypatch.setattr(cloud_routes, "get_cloud_status_snapshot", provider_forbidden)
    monkeypatch.setattr(cloud_routes, "_read_cloud_session_resources", provider_forbidden)
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000))

    response = client.get("/api/cloud/local-resources")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "host_memory": {
            "used_bytes": 10 * 1024**3,
            "total_bytes": 16 * 1024**3,
            "percent_used": 62.5,
        },
        "active_job_count": 1,
        "max_concurrent_jobs": 3,
    }


def test_job_tracking_includes_all_queued_jobs_and_deduplicates_telegram_intake(tmp_path, monkeypatch):
    from types import SimpleNamespace

    store = CloudJobStore(tmp_path / "tracking.db", rollout_controlled=False)
    monkeypatch.setattr(cloud_routes, "store", store)
    active = store.enqueue("active.mp4", gpu_type="L4")
    assert store.claim_job(active.job_id, "worker")
    telegram = store.enqueue("persisted.mp4", idempotency_key="cloudbot:transition")
    for index in range(30):
        store.enqueue(f"waiting-{index}.mp4")
    intake = [
        {"job_id": "telegram-transition", "queue_key": "cloudbot:transition", "state": "intake"},
        {"job_id": "telegram-new", "queue_key": "cloudbot:new", "state": "queued", "gpu_type": "L4"},
    ]
    monkeypatch.setattr(cloud_routes, "peek_cloud_telegram_bot", lambda: SimpleNamespace(list_intake_jobs=lambda: intake))

    def capture_resources(rows, warm):
        assert [row["job_id"] for row in rows] == [active.job_id]
        return []

    monkeypatch.setattr(cloud_routes, "_read_cloud_session_resources", capture_resources)
    jobs, _ = cloud_routes._read_dashboard_tracking([])
    assert len(jobs) == 33
    assert jobs[0]["job_id"] == active.job_id
    assert jobs[1]["job_id"] == telegram.job_id and jobs[1]["source"] == "Telegram"
    assert jobs[-1]["job_id"] == "telegram-new"
    assert not any("queue_key" in job or "video_path" in job for job in jobs)


def test_dashboard_enqueue_can_refresh_a_stale_untimestamped_provider_sample(tmp_path, monkeypatch):
    from types import SimpleNamespace
    store = CloudJobStore(tmp_path / "refresh.db")
    store.record_provider_usage_snapshot({"ok": True, "balance": 1, "age_seconds": 10000})
    permit = store.set_rollout(arm=True)["permit_id"]
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "get_usage", lambda: {"ok": True, "balance": 50.0})
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "cloud-test-token")
    monkeypatch.setattr(cloud_routes, "get_cloud_worker", lambda: SimpleNamespace(is_running=lambda: True))
    app = FastAPI()
    app.include_router(cloud_routes.router)
    client = TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000))
    response = client.post("/api/cloud/enqueue", headers={"X-Local-Control-Token": "cloud-test-token"}, json={
        "url": "https://www.tiktok.com/@sample/video/123456789", "rollout_permit": permit,
    })
    assert response.status_code == 200
    snapshot = store.get_provider_usage_snapshot()
    assert snapshot["balance"] == 50.0
    assert snapshot["is_stale"] is False
