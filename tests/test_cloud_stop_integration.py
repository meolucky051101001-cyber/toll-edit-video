"""Emergency stop must close admission without inventing provider success."""
import asyncio

import pytest

from backend.cloud import cloud_routes
from backend.cloud.cloud_job_store import CloudJobStore


@pytest.mark.parametrize("result, expected", [
    ({"timed_out": False, "completed": True, "result": (True, "verified")}, "ok"),
    ({"timed_out": False, "completed": True, "result": (False, "denied")}, "error"),
    ({"timed_out": True, "completed": False}, "pending"),
    ({"timed_out": False, "completed": False, "error": "offline"}, "error"),
])
def test_emergency_stop_disarms_and_cancels_before_provider(tmp_path, monkeypatch, result, expected):
    store = CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False)
    active = store.enqueue("https://www.tiktok.com/@sample/video/123456789")
    active = store.claim_job(active.job_id, "worker")
    queued = store.enqueue("https://www.tiktok.com/@sample/video/123456790")
    monkeypatch.setattr(cloud_routes, "store", store)

    def provider_action(fn, **kwargs):
        assert fn is cloud_routes.stop_all_sessions
        assert store.get_rollout() == {"phase": "paused"}
        assert store.get_job(queued.job_id).state == "cancelled"
        assert store.get_job(active.job_id).state == "cancel_requested"
        return result

    monkeypatch.setattr(cloud_routes, "run_provider_action_nonblocking", provider_action)
    response = asyncio.run(cloud_routes.api_cloud_stop())
    assert response["status"] == expected
    ledger = store.list_cost_ledger()
    assert len(ledger) == 1
    assert not ledger[0]["cleanup_verified"]
