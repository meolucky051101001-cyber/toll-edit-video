"""Offline runtime regressions; no production processes, GPU or providers."""
import ast
import asyncio
import copy
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import api_pipeline_guard as guard
import job_tracker
import v1_gpu_gatekeeper as gate
from fastapi import HTTPException
from scripts import verify_runtime_health as health


async def wait_for_work_started(task, entered):
    """Surface an admission failure instead of hanging on an unset event."""
    waiter = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait({task, waiter}, timeout=3,
                                     return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            task.result()
        assert waiter in done, "API work did not start within the test deadline"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.fixture
def isolated_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(job_tracker, "WORKSPACE", tmp_path)
    monkeypatch.setattr(job_tracker, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(job_tracker, "BATCH_LOCK_FILE", tmp_path / "batch.lock")
    monkeypatch.setattr(job_tracker, "_CURRENT_STATE", copy.deepcopy(job_tracker._DEFAULT_STATE))
    monkeypatch.setattr(guard, "begin_video", lambda _: None)
    monkeypatch.setattr(guard, "end_video", lambda: None)
    return tmp_path


def test_api_publishes_active_lease_and_keeps_it_after_disconnect(isolated_tracker):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()

        @guard.guarded_media_api
        async def work(video_path):
            entered.set()
            await finish.wait()
            return {"status": "success"}

        caller = asyncio.create_task(work("sample.mp4"))
        await wait_for_work_started(caller, entered)
        state = job_tracker.get_status(strict=True)
        assert state["active"] is True
        assert state["video_name"] == "sample.mp4"
        with patch("worker_settings.get_worker_settings", return_value={"concurrency": 2}):
            with pytest.raises(job_tracker.JobAlreadyRunningError):
                job_tracker.start_batch(1, job_id="telegram-job")
        with pytest.raises(HTTPException) as conflict:
            await work("second.mp4")
        assert conflict.value.status_code == 409
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert job_tracker.BATCH_LOCK_FILE.is_file()
        finish.set()
        await asyncio.gather(*list(guard._tasks))
        assert not job_tracker.BATCH_LOCK_FILE.exists()
        assert job_tracker.get_status()["active"] is False

    asyncio.run(scenario())


def test_api_error_result_releases_lease_and_marks_failure(isolated_tracker):
    @guard.guarded_media_api
    async def work():
        return {"status": "error", "message": "render rejected"}

    assert asyncio.run(work())["status"] == "error"
    state = job_tracker.get_status()
    assert state["status"] == "error"
    assert state["active"] is False
    assert not job_tracker.BATCH_LOCK_FILE.exists()


def test_api_stop_keeps_lease_until_work_drains_and_new_job_clears_old_stop(isolated_tracker):
    import main
    import shared_state

    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()

        @guard.guarded_media_api
        async def work():
            entered.set()
            await finish.wait()
            return {"status": "success"}

        task = asyncio.create_task(work())
        await wait_for_work_started(task, entered)
        result = await main.api_stop_batch()
        assert result["status"] == "stopping"
        assert job_tracker.BATCH_LOCK_FILE.exists()
        assert job_tracker.get_status()["active"] is True
        with pytest.raises(job_tracker.JobAlreadyRunningError):
            job_tracker.start_batch(1, job_id="next-batch")
        finish.set()
        await task
        assert not job_tracker.BATCH_LOCK_FILE.exists()
        assert job_tracker.get_status()["status"] == "stopped"

        @guard.guarded_media_api
        async def fresh():
            assert job_tracker.is_stop_requested() is False
            assert shared_state._local_stop is False
        await fresh()
    asyncio.run(scenario())


def test_every_direct_media_alias_is_registered_after_lease_guard():
    tree = ast.parse((Path(__file__).parents[1] / "backend/main.py").read_text(encoding="utf-8"))
    names = {"api_generate_subtitles", "api_process_video", "api_process_url"}
    found = set()
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
            assert isinstance(node.decorator_list[-1], ast.Name)
            assert node.decorator_list[-1].id == "guarded_media_api"
            found.add(node.name)
    assert found == names


def test_dashboard_bootstrap_keeps_explicit_environment(tmp_path, monkeypatch):
    # Execute the real bootstrap statements through PATHS only: no AI imports,
    # local token writes, lifespan, or service starts.
    root = tmp_path / "checkout"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text("AUTODUB_WORKSPACE=wrong\nPIPELINE_MODE=v2\n", encoding="utf-8")
    expected = tmp_path / "explicit-workspace"
    monkeypatch.setenv("AUTODUB_WORKSPACE", str(expected))
    source = Path(__file__).parents[1] / "backend/main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    statements = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(m in {"environment", "config.paths"} for m in modules):
                statements.append(node)
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id in {"BASE_DIR", "PATHS"}
                   or isinstance(t, ast.Subscript) and ast.unparse(t) == "os.environ['PIPELINE_MODE']"
                   for t in node.targets):
                statements.append(node)
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
              and isinstance(node.value.func, ast.Name) and node.value.func.id == "load_environment"):
            statements.append(node)
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PATHS" for t in node.targets):
            break
    import os
    namespace = {"__file__": str(backend / "main.py"), "Path": Path, "os": os}
    exec(compile(ast.Module(body=statements, type_ignores=[]), str(source), "exec"), namespace)
    assert namespace["PATHS"].workspace == expected
    assert os.environ["AUTODUB_WORKSPACE"] == str(expected)
    assert os.environ["PIPELINE_MODE"] == "legacy"


def test_stage_timeout_is_preserved_and_not_retried(monkeypatch):
    entries, exits = [], []

    @contextmanager
    def fake_gate(**kwargs):
        entries.append(kwargs)
        try:
            yield
        finally:
            exits.append(True)

    monkeypatch.setattr(gate, "gpu_gatekeeper", fake_gate)
    error = TimeoutError("inference timed out")

    async def scenario():
        with pytest.raises(TimeoutError) as raised:
            async with gate.async_gpu_gatekeeper(timeout_seconds=1):
                raise error
        assert raised.value is error

    asyncio.run(scenario())
    assert len(entries) == len(exits) == 1


def test_stage_stop_drains_native_thread_before_lease_release(isolated_tracker):
    from v1_stage_runtime import await_stage
    from batch_control import stop_check
    entered, finish, observed_stop = threading.Event(), threading.Event(), threading.Event()

    def native():
        check = stop_check.get()
        entered.set()
        while not finish.wait(.01):
            if check():
                observed_stop.set()

    async def scenario():
        stop_requested = False
        def check():
            if stop_requested:
                raise RuntimeError("stop requested")

        @guard.guarded_media_api
        async def work():
            await await_stage(asyncio.to_thread(native), 3, check)

        task = asyncio.create_task(work())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            stop_requested = True
            assert await asyncio.to_thread(observed_stop.wait, 1)
            assert not task.done()
            assert job_tracker.BATCH_LOCK_FILE.exists()
        finally:
            finish.set()
        with pytest.raises(RuntimeError, match="stop requested"):
            await task
        assert not job_tracker.BATCH_LOCK_FILE.exists()
    asyncio.run(scenario())


def test_cancel_during_acquisition_releases_late_lock_without_blocking_loop(monkeypatch):
    entered, allow_acquire = threading.Event(), threading.Event()
    released = []

    @contextmanager
    def fake_gate(**kwargs):
        entered.set()
        assert allow_acquire.wait(2)
        try:
            yield
        finally:
            released.append(True)

    monkeypatch.setattr(gate, "gpu_gatekeeper", fake_gate)

    async def scenario():
        async def work():
            async with gate.async_gpu_gatekeeper(timeout_seconds=1):
                pytest.fail("cancelled caller must not enter the body")

        task = asyncio.create_task(work())
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # a repeated cancel must not detach a late-acquired gate
            await asyncio.sleep(0)
        finally:
            allow_acquire.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert released == [True]

    asyncio.run(scenario())


def test_component_failure_makes_dashboard_health_fail():
    with patch.object(health, "_get_json", return_value={
        "status": "idle", "active": False,
        "service_health": {"ready": False, "components": {"workflow": False}},
    }):
        ok, detail = health._dashboard(8088, 0)
    assert ok is False
    assert "components_ready=false" in detail


def test_health_reports_missing_router_without_starting_services():
    import main
    async def scenario():
        with patch.object(main, "REQUIRED_ROUTER_PATHS", {"missing": "/missing-feature"}):
            response = await main.api_component_health()
            assert response.status_code == 503
            assert json.loads(response.body)["components"] == {"missing": False}
    asyncio.run(scenario())


def test_health_recognizes_effective_nested_routes_even_when_hidden(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    import main

    test_app = FastAPI()
    nested = APIRouter()

    @nested.get("/ready/{job_id}", include_in_schema=False)
    async def ready(job_id: str):
        return {"job_id": job_id}

    @nested.post("/write-only")
    async def write_only():
        raise AssertionError("Health must not execute endpoints")

    parent = APIRouter(prefix="/feature")
    parent.include_router(nested)
    test_app.include_router(parent)
    monkeypatch.setattr(main, "app", test_app)
    monkeypatch.setattr(main, "REQUIRED_ROUTER_PATHS", {"nested": "/feature/ready/{job_id}"})
    assert main.component_health() == {"ready": True, "components": {"nested": True}}
    response = TestClient(test_app).get("/feature/ready/fixture")
    assert response.status_code == 200 and response.json() == {"job_id": "fixture"}
    monkeypatch.setattr(main, "REQUIRED_ROUTER_PATHS", {"missing_get": "/feature/write-only"})
    assert main.component_health() == {"ready": False, "components": {"missing_get": False}}


def test_required_application_routers_are_healthy_without_lifespan():
    import main
    assert main.component_health() == {
        "ready": True, "components": dict.fromkeys(main.REQUIRED_ROUTER_PATHS, True)
    }
