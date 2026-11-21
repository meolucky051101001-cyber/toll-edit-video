import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.cloud import cloud_routes
from backend.cloud.cloud_contract import DASHBOARD_GPU_SETTING, GPU_CHECK_TASK
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.cloud_telegram_bot import CloudTelegramBotService
from backend.cloud.colab_bridge import ColabProviderAdapter
from tests.test_cloud_phase5_quality_export import _Phase5Adapter
from tests.test_cloud_telegram_bot import _make_update


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    store = CloudJobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(cloud_routes, "store", store)
    monkeypatch.setattr(cloud_routes, "resolve_local_control_token", lambda: "fixture-token")
    worker = Mock()
    worker.is_running.return_value = True
    monkeypatch.setattr(cloud_routes, "get_cloud_worker", lambda: worker)
    app = FastAPI()
    app.include_router(cloud_routes.router)
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 42000)) as client:
        yield store, client


def test_dashboard_gpu_default_is_saved_and_used_for_new_jobs(dashboard):
    store, client = dashboard
    headers = {"X-Local-Control-Token": "fixture-token"}
    path = "/api/cloud/gpu-settings"
    assert client.get(path).json()["default_gpu"] == "T4"
    assert client.post(path, json={"gpu_type": "L4"}).status_code == 403
    assert client.post(path, json={"gpu_type": "V5E1"}, headers=headers).status_code == 400
    assert client.post(path, json={"gpu_type": "L4"}, headers=headers).json()["default_gpu"] == "L4"
    assert CloudJobStore(store.db_path).get_setting(DASHBOARD_GPU_SETTING) == "L4"

    permit = store.set_rollout(arm=True)["permit_id"]
    response = client.post(
        "/api/cloud/enqueue",
        json={"url": "https://www.tiktok.com/@example/video/1234567890", "rollout_permit": permit},
        headers=headers,
    )
    assert response.status_code == 200, response.json()
    assert response.json()["job"]["gpu_type"] == "L4"


def test_dashboard_and_telegram_share_gpu_and_preserve_already_queued_choices(dashboard, monkeypatch):
    store, client = dashboard
    monkeypatch.delenv("CLOUD_BOT_ALLOWED_USER_IDS", raising=False)
    bot = CloudTelegramBotService(workspace_dir=store.db_path.parent, store=CloudJobStore(store.db_path),
                                  adapter_factory=Mock)
    bot.state_files.update_prefs(gpu_type="L4")
    endpoint = "/api/cloud/gpu-settings"
    headers = {"X-Local-Control-Token": "fixture-token"}
    assert client.get(endpoint).json()["default_gpu"] == "L4"

    async def enqueue_twice_and_switch_from_telegram():
        bot._queue = asyncio.Queue()
        context = SimpleNamespace(args=[])
        first, _, _ = _make_update(text="https://example.com/l4.mp4")
        await bot.handle_text_message(first, context)
        assert await asyncio.to_thread(lambda: client.post(endpoint, json={"gpu_type": "T4"}, headers=headers).status_code) == 200
        second, _, _ = _make_update(text="https://example.com/t4.mp4")
        await bot.handle_text_message(second, context)
        assert [item["gpu_type"] for item in bot.list_intake_jobs()] == ["L4", "T4"]
        assert bot.get_status()["default_gpu"] == "T4"
        command, _, _ = _make_update()
        await bot.cmd_gpu(command, SimpleNamespace(args=["L4"]))

    asyncio.run(enqueue_twice_and_switch_from_telegram())
    assert client.get(endpoint).json()["default_gpu"] == "L4"


def test_gpu_check_consumes_one_rollout_and_returns_evidence(dashboard):
    store, client = dashboard
    headers = {"X-Local-Control-Token": "fixture-token"}
    route = "/api/cloud/gpu-check"
    args = {"gpu_type": "A100"}
    assert client.post(route, json=args, headers=headers).status_code == 409
    args["rollout_permit"] = store.set_rollout(arm=True)["permit_id"]
    assert client.post(route, json=args).status_code == 403
    response = client.post(route, json=args, headers=headers)
    assert response.status_code == 200, response.json()
    queued = store.get_job(response.json()["job_id"])
    assert queued.task_type == GPU_CHECK_TASK and queued.video_path == ""
    evidence = {row["gpu_type"]: row["evidence"] for row in client.get("/api/cloud/gpu-settings").json()["gpus"]}
    assert evidence["A100"]["state"] == "queued"
    assert evidence["H100"] is None
    assert client.post(route, json=args, headers=headers).status_code == 409


@pytest.mark.parametrize("accelerator", ["CPU", "V5E1", "V6E1"])
def test_dashboard_rejects_video_jobs_without_cuda(dashboard, accelerator):
    store, client = dashboard
    headers = {"X-Local-Control-Token": "fixture-token"}
    permit = store.set_rollout(arm=True)["permit_id"]
    result = client.post(
        "/api/cloud/enqueue",
        json={"url": "https://www.tiktok.com/@example/video/1234567890",
              "gpu_type": accelerator, "rollout_permit": permit},
        headers=headers,
    )
    assert result.status_code == 400
    assert "CUDA" in result.json()["detail"]
    assert store.get_rollout()["permit_id"] == permit
    assert store.list_jobs() == []


@pytest.mark.parametrize(
    ("scenario", "error_code", "stopped"),
    [
        ("success", None, True),
        ("wrong_gpu", "ENV_GPU_MISMATCH", True),
        ("refused", "GPU_ALLOCATION_REFUSED", False),
        ("no_capacity", "PROVIDER_503", False),
        ("cleanup_uncertain", "CLEANUP_FAILED", False),
    ],
)
def test_gpu_check_uses_no_video_and_verifies_teardown(tmp_path, scenario, error_code, stopped):
    class Adapter(_Phase5Adapter):
        def create_session(self, session_name, gpu="T4"):
            if scenario == "refused":
                return False, "[colab] Backend rejected accelerator 'L4'."
            if scenario == "no_capacity":
                return False, "503 Service Unavailable"
            assert gpu == "L4"
            return super().create_session(session_name, gpu=gpu)

        def exec_argv(self, session_name, argv, **kwargs):
            assert argv[0] == "nvidia-smi"
            return True, "NVIDIA A100-SXM4-40GB" if scenario == "wrong_gpu" else "NVIDIA L4"

        def stop_session(self, session_name):
            if scenario == "cleanup_uncertain":
                return False, "stop unconfirmed"
            return super().stop_session(session_name)

        def upload_file(self, *args, **kwargs):
            pytest.fail("GPU checks must not upload video or setup files")

    store = CloudJobStore(tmp_path / "jobs.db", rollout_controlled=False)
    adapter = Adapter()
    orch = CloudOrchestrator(store=store, adapter=adapter, enable_warm_session_reuse=True)
    job = store.enqueue("", video_name="Thử L4", task_type=GPU_CHECK_TASK, gpu_type="L4")
    claimed = store.claim_job(job.job_id, "gpu-test")
    assert not store.update_progress(job.job_id, state="completed", owner_id="gpu-test", attempt=claimed.attempt)
    assert orch.process_job(claimed, owner_id="gpu-test") is (error_code is None)
    saved = store.get_job(job.job_id)
    assert saved.state == ("failed" if error_code else "completed")
    assert saved.error_code == error_code
    assert not saved.result_path
    assert adapter.sessions_stopped == ([saved.session_name] if stopped else [])
    assert not store.list_warm_sessions()
    assert store.get_cu_budget_info()["unreconciled_count"] == int(scenario == "cleanup_uncertain")


def test_gpu_check_accepts_provider_hardware_while_kernel_is_booting(tmp_path):
    class Adapter(_Phase5Adapter, ColabProviderAdapter):
        def get_sessions_status(self):
            sessions = super().get_sessions_status()
            for row in sessions["sessions"]:
                row.update(is_active=True, raw=f"[{row['name']}] test-vm | Hardware: L4 | Shape: High-RAM")
            return sessions

        def exec_argv(self, *args, **kwargs):
            pytest.fail("A confirmed provider assignment does not need a kernel call.")

        def verify_session_stopped(self, session_name):
            self.stop_session(session_name)
            return {"verified_stopped": True, "cleanup_status": "verified", "attempts": 1}

    store = CloudJobStore(tmp_path / "jobs.db", rollout_controlled=False)
    adapter = Adapter()
    orch = CloudOrchestrator(store=store, adapter=adapter)
    job = store.enqueue("", video_name="Thử L4", task_type=GPU_CHECK_TASK, gpu_type="L4")
    claimed = store.claim_job(job.job_id, "gpu-test")
    assert orch.process_job(claimed, owner_id="gpu-test")
    assert store.get_job(job.job_id).state == "completed"
    assert len(adapter.sessions_stopped) == 1
