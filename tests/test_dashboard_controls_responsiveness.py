"""Dashboard control requests must remain responsive while model/disk work waits."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

import httpx
import pytest


async def _request_while_blocked(main, entered, release, expired, method, path, **kwargs):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        headers={"X-Local-Control-Token": main.LOCAL_TOKEN},
    ) as client:
        pending = asyncio.create_task(client.request(method, path, **kwargs))
        try:
            assert await asyncio.to_thread(entered.wait, 3), "Slow operation never started"
            # Both requests share one event loop. A synchronous wait in an async
            # route would prevent this unrelated dashboard request from finishing.
            other = await asyncio.wait_for(client.get("/api/v1/router-modes"), timeout=1)
            assert other.status_code == 200, other.text
            assert not expired.is_set(), "Slow control work blocked the dashboard event loop"
            assert not pending.done(), "Control completed before its durable operation was released"
        finally:
            release.set()
            response = await asyncio.wait_for(pending, timeout=3)
    return response


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_sensevoice_readiness_does_not_block_dashboard_requests(tmp_path, monkeypatch, method):
    import main
    from ai import v1_sensevoice_adapter

    monkeypatch.setattr(main, "WORKSPACE", str(tmp_path))
    entered, release, expired = threading.Event(), threading.Event(), threading.Event()

    def slow_readiness(ignore_flag=True):
        entered.set()
        if not release.wait(3):
            expired.set()
        return True, "Fixture runtime ready"

    monkeypatch.setattr(v1_sensevoice_adapter, "check_sensevoice_readiness", slow_readiness)
    kwargs = {"json": {"V1_ASR_MODEL": "sensevoice_small"}} if method == "POST" else {}
    response = asyncio.run(_request_while_blocked(
        main, entered, release, expired, method, "/api/v1/feature-flags", **kwargs))
    assert response.status_code == 200, response.text
    if method == "POST":
        assert response.json()["flags"]["V1_ASR_MODEL"] == "sensevoice_small"
    else:
        assert response.json()["SENSEVOICE_ASR_READY"] is True


@pytest.mark.parametrize("slow_operation", ["get_status", "request_stop"])
def test_stop_tracker_io_does_not_block_dashboard_requests(monkeypatch, slow_operation):
    import main

    monkeypatch.setattr(main, "BATCH_TASK", None)
    entered, release, expired = threading.Event(), threading.Event(), threading.Event()
    active = {"active": True, "occupies_slot": True, "execution_id": "slow-stop", "job_id": "slow-stop"}

    def get_status():
        return dict(active)

    def request_stop():
        return None

    original = get_status if slow_operation == "get_status" else request_stop

    def blocked_operation():
        entered.set()
        if not release.wait(3):
            expired.set()
        return original()

    monkeypatch.setattr(main.job_tracker, "get_status", get_status)
    monkeypatch.setattr(main.job_tracker, "request_stop", request_stop)
    monkeypatch.setattr(main.job_tracker, slow_operation, blocked_operation)
    response = asyncio.run(_request_while_blocked(
        main, entered, release, expired, "POST", "/api/stop-batch"))
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "stopping"
    assert response.json()["occupies_slot"] is True


def test_stop_shared_state_io_does_not_block_dashboard_requests(monkeypatch):
    import main

    entered, release, expired = threading.Event(), threading.Event(), threading.Event()

    class SlowSharedState:
        def __init__(self):
            self._stop_requested = False

        @property
        def stop_requested(self):
            return self._stop_requested

        @stop_requested.setter
        def stop_requested(self, value):
            entered.set()
            if not release.wait(3):
                expired.set()
            self._stop_requested = value

    shared = SlowSharedState()
    monkeypatch.setattr(main, "shared_state", shared)
    monkeypatch.setattr(main.job_tracker, "get_status", lambda: {
        "active": True, "occupies_slot": True, "execution_id": "shared-stop", "job_id": "shared-stop"})
    stop_calls = []
    monkeypatch.setattr(main.job_tracker, "request_stop", lambda: stop_calls.append(True))
    response = asyncio.run(_request_while_blocked(
        main, entered, release, expired, "POST", "/api/stop-batch"))
    assert response.status_code == 202, response.text
    assert shared.stop_requested is True
    assert stop_calls == [True]


def test_idle_tracker_does_not_stop_a_lingering_batch_task(monkeypatch):
    import main

    monkeypatch.setattr(main, "BATCH_TASK", SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(main.job_tracker, "get_status", lambda: {
        "status": "idle", "active": False, "occupies_slot": False})
    monkeypatch.setattr(main.job_tracker, "request_stop", lambda: pytest.fail("Idle tracker must not receive stop"))
    shared = SimpleNamespace(stop_requested=False)
    monkeypatch.setattr(main, "shared_state", shared)

    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1",
            headers={"X-Local-Control-Token": main.LOCAL_TOKEN},
        ) as client:
            return await client.post("/api/stop-batch")

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "idle"
    assert shared.stop_requested is False


def test_concurrent_partial_feature_saves_preserve_each_setting(tmp_path, monkeypatch):
    import v1_feature_flags

    updates = {
        "V1_VIDEO_MODE": "LONG", "V1_ASR_MODEL": "whisper_turbo",
        "V1_SHORT_ASR_MODEL": "sensevoice_small", "V1_LONG_ASR_MODEL": "whisper_turbo",
        "V1_SHORT_SEPARATOR": "demucs", "V1_LONG_SEPARATOR": "roformer",
        "V1_ASR_CHUNKING": False, "V1_SMART_SKIP_OCR": False,
        "V1_QC_POLICY": "WARN", "V1_QC_VISUAL_PIXEL_CHECK": False,
        "V1_TTS_WORKERS": 2, "V1_MAX_NATURAL_SPEED": 1.25,
    }
    read_flags = v1_feature_flags.get_feature_flags
    start = threading.Event()

    def slow_read(*args, **kwargs):
        snapshot = read_flags(*args, **kwargs)
        # Widen the real read/merge/write race: without serialization, writers
        # all read the old file and each replaces the other settings' changes.
        time.sleep(0.03)
        return snapshot

    monkeypatch.setattr(v1_feature_flags, "get_feature_flags", slow_read)

    def save(item):
        assert start.wait(3)
        key, value = item
        return v1_feature_flags.set_feature_flags({key: value}, tmp_path)

    with ThreadPoolExecutor(max_workers=len(updates)) as pool:
        pending = [pool.submit(save, item) for item in updates.items()]
        start.set()
        for future in pending:
            future.result(timeout=5)

    saved = read_flags(tmp_path)
    assert {key: saved[key] for key in updates} == updates
