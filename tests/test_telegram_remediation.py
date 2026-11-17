"""Telegram local-file dispatch, durable identities and verified cloud completion."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import AsyncMock
import sys

import pytest

import job_tracker
import telegram_bot as bot
import telegram_queue_monitor as monitor
from batch_plan_service import fingerprint
from telegram_local_source import capture_source
from v1_integrity_errors import OutputVerificationError, StaleExecutionError, CloudRecoveryRequired


@pytest.fixture
def setup(tmp_path, monkeypatch):
    import service_control
    monkeypatch.setattr(service_control, "control_directories", lambda *a, **k: [tmp_path / "control"])
    source = tmp_path / "Nguồn có khoảng trắng.mp4"
    source.write_bytes(b"local-source")
    monkeypatch.setattr(monitor, "SNAPSHOT", tmp_path / "queue.json")
    queue = monitor.MonitoredQueue()
    monkeypatch.setattr(bot, "global_queue", queue)
    monkeypatch.setattr(bot, "WORKSPACE", str(tmp_path))
    monkeypatch.setattr(bot, "OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setattr(bot, "safe_edit_status", AsyncMock())
    job = {"job_id": "local_fixture", "type": "local_file", "filename": source.name,
           "video_path": str(source), "force_cloud": True, "cloud_gpu": "T4", "voice": "fixture",
           "voice_mode": "manual", "job_overrides": {"voice_mode": "manual"}, "resume_state": {}}
    capture_source(job)
    return SimpleNamespace(source=source, queue=queue, job=job)


def test_local_file_worker_dispatches_once_before_queue_ack(setup, monkeypatch):
    import dashboard_media
    monkeypatch.setattr(dashboard_media, "media_status", lambda *a, **k: {"status": "valid"})
    calls = []
    async def scenario():
        completed = asyncio.Event()
        async def process(job):
            assert setup.queue.active_job["job_id"] == job["job_id"]
            calls.append(job["video_path"])
            job_tracker.start_video(job["filename"])
            receipt = job_tracker.finish_video(job["filename"], "verified.mp4")
            assert receipt["execution_id"] == job_tracker.current_execution.get()
            return {"status": "success", "tracker_finalized": True}
        monkeypatch.setattr(bot, "_process_telegram_job", process)
        done = setup.queue.task_done
        def acknowledged(*args, **kwargs):
            assert job_tracker.get_status()["video_status"] == "completed"
            done(*args, **kwargs)
            completed.set()
        monkeypatch.setattr(setup.queue, "task_done", acknowledged)
        await setup.queue.put(setup.job)
        task = asyncio.create_task(bot.video_worker())
        try:
            await asyncio.wait_for(completed.wait(), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
    assert calls == [str(setup.source)]
    assert not setup.queue.active_jobs and setup.queue.empty()


def test_local_source_survives_pending_and_active_restore(setup):
    setup.queue.put_nowait(setup.job)
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored._queue[0]["video_path"] == str(setup.source)
    assert restored._queue[0]["resume_state"] == setup.job["resume_state"]
    restored.get_nowait()
    active = monitor.MonitoredQueue()
    active.restore()
    assert active.recovery_state == "needs_review"
    assert active.active_job["video_path"] == str(setup.source)
    assert active.active_job["voice"] == "fixture"


@pytest.mark.parametrize("kind,reason", [("missing", "missing_local_source"), ("conflict", "conflicting_local_source"), ("no_hash", "unverified_local_source")])
def test_legacy_local_source_hold_cannot_be_blindly_released(setup, kind, reason):
    from queue_control_service import submit_command
    job = setup.job
    if kind == "missing":
        job.pop("video_path")
        job["resume_state"].pop("source_path")
    elif kind == "conflict":
        job["resume_state"]["source_path"] = str(setup.source.with_name("other.mp4"))
    else:
        job["resume_state"].pop("source_sha256")
    setup.queue.put_nowait(job)
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored._queue[0]["hold"]["reason_code"] == reason
    submit_command(restored.snapshot, "release_manual", [job["job_id"]], expected_revision=restored.revision, operation_id="blind-release")
    restored.apply_commands()
    assert restored._queue[0]["hold"]["reason_code"] == reason
    with pytest.raises(asyncio.QueueEmpty):
        restored.get_nowait()


def test_legacy_exact_resume_path_can_restore(setup):
    setup.job.pop("video_path")
    setup.queue.put_nowait(setup.job)
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored._queue[0]["video_path"] == str(setup.source)
    assert not restored._queue[0].get("hold")


def test_changed_source_fails_before_cloud(setup, monkeypatch):
    setup.source.write_bytes(b"different")
    cloud = AsyncMock()
    monkeypatch.setattr(bot, "run_pipeline_cloud_for_telegram", cloud)
    result = asyncio.run(bot._process_telegram_job(setup.job))
    assert result["status"] == "needs_review" and "thay đổi" in result["error"]
    cloud.assert_not_awaited()


@pytest.mark.parametrize("change,reason", [("delete", "missing_local_source"), ("replace", "changed_local_source")])
def test_restored_source_is_revalidated_and_held(setup, change, reason):
    setup.queue.put_nowait(setup.job)
    if change == "delete":
        setup.source.unlink()
    else:
        setup.source.write_bytes(b"replacement-source")
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored._queue[0]["hold"]["reason_code"] == reason
    with pytest.raises(asyncio.QueueEmpty):
        restored.get_nowait()
    assert restored.qsize() == 1


def cloud_stubs(monkeypatch, setup, *, outcome="completed", receipt=True):
    calls = []
    record = SimpleNamespace(job_id="cloud_fixture", attempt_id="attempt1", owner_id="telegram-execution1",
                             external_job_id=setup.job["job_id"], state="creating_session", result_path="cloud.mp4",
                             error="fixture failure", cu_actual=0)
    class Store:
        def enqueue(self, **kwargs):
            calls.append(("enqueue", kwargs))
            return record
        def get_job(self, job_id):
            return record
        def get_receipt(self, job_id):
            calls.append(("verified", job_id))
            return {"job_id": record.job_id, "attempt_id": record.attempt_id,
                    "source_sha256": setup.job["resume_state"]["source_sha256"],
                    "result_path": record.result_path} if receipt else None
    class Orchestrator:
        def __init__(self, store):
            pass
        def process_job(self, job, callback, cancel_check=None):
            calls.append(("process", job.job_id))
            record.state = outcome
            return SimpleNamespace(status=outcome, error="fixture failure")
    for name, member, value in (("cloud.cloud_job_store", "CloudJobStore", Store),
                                ("cloud.cloud_orchestrator", "CloudOrchestrator", Orchestrator)):
        module = ModuleType(name)
        setattr(module, member, value)
        monkeypatch.setitem(sys.modules, name, module)
    return record, calls


def test_cloud_completion_requires_verified_output_and_tracker_receipt(setup, monkeypatch):
    import dashboard_media
    monkeypatch.setattr(dashboard_media, "media_status", lambda *a, **k: {"status": "valid"})
    record, calls = cloud_stubs(monkeypatch, setup)
    job_tracker.start_batch(1, job_id="execution1")
    token = job_tracker.current_execution.set("execution1")
    try:
        result = asyncio.run(bot.run_pipeline_cloud_for_telegram(setup.source, setup.job, None))
        assert result["tracker_finalized"]
        assert job_tracker.get_status()["video_status"] == "completed"
        assert setup.job["resume_state"]["cloud_completion"]["tracker_receipt"]["execution_id"] == "execution1"
        assert [call[0] for call in calls] == ["enqueue", "process", "verified"]
    finally:
        job_tracker.current_execution.reset(token)


@pytest.mark.parametrize("outcome,receipt,error", [("completed", False, OutputVerificationError), ("error", True, OutputVerificationError),
                                                     ("recovery_required", True, CloudRecoveryRequired)])
def test_cloud_unverified_outcome_never_finalizes(setup, monkeypatch, outcome, receipt, error):
    cloud_stubs(monkeypatch, setup, outcome=outcome, receipt=receipt)
    finished = []
    monkeypatch.setattr(job_tracker, "finish_video", lambda *a, **k: finished.append(a))
    job_tracker.start_batch(1, job_id="execution1")
    token = job_tracker.current_execution.set("execution1")
    try:
        with pytest.raises(error):
            asyncio.run(bot.run_pipeline_cloud_for_telegram(setup.source, setup.job, None))
        assert finished == []
    finally:
        job_tracker.current_execution.reset(token)


def test_stale_tracker_does_not_claim_cloud_success(setup, monkeypatch):
    cloud_stubs(monkeypatch, setup)
    monkeypatch.setattr(job_tracker, "finish_video", lambda *a, **k: None)
    job_tracker.start_batch(1, job_id="execution1")
    token = job_tracker.current_execution.set("execution1")
    try:
        with pytest.raises(StaleExecutionError):
            asyncio.run(bot.run_pipeline_cloud_for_telegram(setup.source, setup.job, None))
        assert "cloud_completion" not in setup.job["resume_state"]
    finally:
        job_tracker.current_execution.reset(token)


def test_verification_and_recovery_errors_never_trigger_network_retry():
    from v1_retry_policy import should_retry_job, is_transient_network_error
    for error in (OutputVerificationError("ffprobe timeout"), CloudRecoveryRequired("network timeout")):
        assert not should_retry_job(error)
        assert not is_transient_network_error(error)


@pytest.mark.parametrize("defect", [None, "source", "attempt", "path", "tracker", "marker"])
def test_cloud_restore_requires_matching_local_and_remote_receipts(setup, monkeypatch, defect):
    record, _ = cloud_stubs(monkeypatch, setup)
    state = setup.job["resume_state"]
    state.update(cloud_job_id=record.job_id, cloud_attempt_id=record.attempt_id,
                 cloud_completion={"job_id": record.job_id, "attempt_id": record.attempt_id,
                                   "tracker_receipt": {"output_verified": True, "execution_id": "execution1",
                                                       "generation": 1, "output_path": record.result_path}})
    if defect == "source":
        # The queue source changed independently of the stored Cloud receipt.
        store_class = sys.modules["cloud.cloud_job_store"].CloudJobStore
        original = store_class.get_receipt
        monkeypatch.setattr(store_class, "get_receipt", lambda self, jid: dict(original(self, jid), source_sha256="wrong"))
    elif defect == "attempt":
        state["cloud_attempt_id"] = "other-attempt"
    elif defect == "path":
        state["cloud_completion"]["tracker_receipt"]["output_path"] = "other-output.mp4"
    elif defect == "tracker":
        state["cloud_completion"]["tracker_receipt"].pop("generation")
    elif defect == "marker":
        state.pop("cloud_completion")
    assert bot._delivered_telegram_job(setup.job) is (defect is None)


@pytest.mark.parametrize("defect", ["source_sha256", "attempt_id", "result_path"])
def test_cloud_mismatched_receipt_never_finalizes(setup, monkeypatch, defect):
    cloud_stubs(monkeypatch, setup)
    store_class = sys.modules["cloud.cloud_job_store"].CloudJobStore
    original = store_class.get_receipt
    monkeypatch.setattr(store_class, "get_receipt", lambda self, jid: dict(original(self, jid), **{defect: "other"}))
    finished = []
    monkeypatch.setattr(job_tracker, "finish_video", lambda *a, **k: finished.append(a))
    job_tracker.start_batch(1, job_id="execution1")
    token = job_tracker.current_execution.set("execution1")
    try:
        with pytest.raises(OutputVerificationError):
            asyncio.run(bot.run_pipeline_cloud_for_telegram(setup.source, setup.job, None))
        assert not finished
    finally:
        job_tracker.current_execution.reset(token)


@pytest.mark.parametrize("defect", [None, "hash", "cleanup"])
def test_real_queue_cloud_and_media_finalize_in_order(setup, tmp_path, monkeypatch, defect):
    """Only the provider is fake: queue, SQLite, publication, ffprobe and tracker are real."""
    import hashlib
    import shutil
    import subprocess
    import cloud.cloud_job_store as store_module
    import cloud.cloud_orchestrator as orchestrator_module
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=160x120:r=10:d=1", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=1", "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(setup.source)],
                   check=True, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    setup.job["resume_state"] = {}
    capture_source(setup.job)
    store = store_module.CloudJobStore(tmp_path / "cloud.sqlite")
    real_orchestrator = orchestrator_module.CloudOrchestrator
    events = []
    class Provider:
        def ensure_ready(self): pass
        def create_session(self, *args, **kwargs):
            events.append("provisioned")
            return True, ""
        def exec_argv(self, *args, **kwargs): return True, ""
        def upload_file(self, *args): return True, ""
        def install_secret(self, *args): return "/fixture/secret"
        def cleanup_secret(self, *args): events.append("secret_cleaned")
        def stop_session(self, *args):
            events.append("resource_drained")
            return defect != "cleanup", ""
        def download_file(self, session, remote, target):
            path = Path(target) / Path(remote).name
            if remote.endswith(".json"):
                job = store.list_jobs()[0]
                path.write_text(json.dumps({"job_id": job.job_id, "attempt_id": job.attempt_id,
                        "source_sha256": job.source_sha256, "sha256": "wrong" if defect == "hash" else fingerprint(setup.source)["sha256"],
                        "size_bytes": setup.source.stat().st_size, "duration_seconds": 1.0}), encoding="utf-8")
            else:
                shutil.copyfile(setup.source, path)
            return True, str(path)
    monkeypatch.setattr(store_module, "CloudJobStore", lambda: store)
    monkeypatch.setattr(orchestrator_module, "CloudOrchestrator", lambda store: real_orchestrator(
        store, provider=Provider(), output_dir=tmp_path / "output", secret_supplier=lambda: {"GEMINI_API_KEY": "fixture-key"}))
    finalizer = job_tracker.finish_video
    def finalize(*args, **kwargs):
        assert "resource_drained" in events and store.get_receipt(store.list_jobs()[0].job_id)
        result = finalizer(*args, **kwargs)
        events.append("tracker_verified")
        return result
    monkeypatch.setattr(job_tracker, "finish_video", finalize)
    async def notify(message, text, **kwargs):
        if text.startswith("🎬 Video đã lồng tiếng Việt"):
            assert "tracker_verified" in events
            events.append("success_notification")
    monkeypatch.setattr(bot, "safe_edit_status", notify)
    async def scenario():
        settled = asyncio.Event()
        original_done, original_hold = setup.queue.task_done, setup.queue.retain_for_review
        def done(*args, **kwargs):
            events.append("queue_ack")
            original_done(*args, **kwargs)
            settled.set()
        def hold(*args, **kwargs):
            events.append("queue_retained")
            original_hold(*args, **kwargs)
            settled.set()
        monkeypatch.setattr(setup.queue, "task_done", done)
        monkeypatch.setattr(setup.queue, "retain_for_review", hold)
        await setup.queue.put(setup.job)
        worker = asyncio.create_task(bot.video_worker())
        try:
            await asyncio.wait_for(settled.wait(), 20)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())
    assert events.count("provisioned") == 1
    assert not job_tracker.get_status()["occupies_slot"]
    if defect is None:
        assert events.index("resource_drained") < events.index("tracker_verified") < events.index("success_notification") < events.index("queue_ack")
        assert not setup.queue.active_jobs and bot._delivered_telegram_job(setup.job)
    else:
        assert "tracker_verified" not in events and "success_notification" not in events
        if defect == "cleanup":
            assert setup.queue.active_jobs and setup.queue.recovery_state == "needs_review"
        else:
            assert not list((tmp_path / "output").glob("cloud_*.mp4"))


def test_cloud_enable_is_blocked_until_transport_is_verified(setup, monkeypatch):
    import v1_feature_flags
    changes = []
    monkeypatch.setattr(v1_feature_flags, "set_feature_flags", lambda *args: changes.append(args))
    reply = AsyncMock()
    asyncio.run(bot.cmd_cloud(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), SimpleNamespace(args=["on"])))
    assert not changes and "chưa thể bật" in reply.call_args.args[0]


@pytest.mark.parametrize("change", ["delete", "replace"])
def test_source_change_after_enqueue_retains_claimed_record(setup, monkeypatch, change):
    async def scenario():
        retained = asyncio.Event()
        original = setup.queue.retain_for_review
        def hold(*args, **kwargs):
            original(*args, **kwargs)
            retained.set()
        monkeypatch.setattr(setup.queue, "retain_for_review", hold)
        monkeypatch.setattr(setup.queue, "task_done", lambda *a, **k: pytest.fail("Invalid local source was acknowledged"))
        await setup.queue.put(setup.job)
        if change == "delete":
            setup.source.unlink()
        else:
            setup.source.write_bytes(b"replaced-after-enqueue")
        worker = asyncio.create_task(bot.video_worker())
        try:
            await asyncio.wait_for(retained.wait(), 5)
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())
    assert setup.queue.recovery_state == "needs_review"
    assert setup.queue.active_jobs[0]["job_id"] == setup.job["job_id"]
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored.active_jobs[0]["resume_state"]["source_sha256"] == setup.job["resume_state"]["source_sha256"]
    assert not job_tracker.get_status()["occupies_slot"]


def test_cloud_stop_persists_scoped_intent_without_claiming_cleanup(setup, monkeypatch, tmp_path):
    import cloud.cloud_job_store as module
    import cloud.colab_bridge as bridge
    store = module.CloudJobStore(tmp_path / "cancel.sqlite")
    active = store.enqueue(str(setup.source), voice="fixture", claim_owner="fixture-worker")
    queued = store.enqueue(str(setup.source), voice="fixture")
    monkeypatch.setattr(module, "CloudJobStore", lambda: store)
    monkeypatch.setattr(bridge, "stop_all_sessions", lambda: pytest.fail("Global session cleanup must never run"))
    reply = AsyncMock()
    asyncio.run(bot.cmd_cloud(SimpleNamespace(message=SimpleNamespace(reply_text=reply)), SimpleNamespace(args=["stop"])))
    assert store.get_job(active.job_id).state == "cancel_requested"
    assert store.get_job(queued.job_id).state == "stopped"
    assert "2 job" in reply.call_args.args[0] and "1 job đang chờ" in reply.call_args.args[0]
