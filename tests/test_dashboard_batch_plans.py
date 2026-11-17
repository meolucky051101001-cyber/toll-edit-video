import asyncio
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from batch_plan_service import BatchPlanStore, PlanConflict, validate_source


def freeze(**kwargs):
    return {"snapshot_id": kwargs["job_id"], "revision": 1,
            "effective_config": {"speed": 1.0, **kwargs["overrides"]}}


@pytest.fixture
def setup_plan(tmp_path):
    source, output = tmp_path / "input", tmp_path / "output"
    source.mkdir(); output.mkdir()
    (source / "A.mp4").write_bytes(b"video-a")
    (source / "B.mp4").write_bytes(b"video-b")
    store = BatchPlanStore(tmp_path / "workspace")
    payload = {"items": [{"filename": "A.mp4", "overrides": {"speed": 1.0}}], "trigger": False, "idempotency_key": "select-a"}
    return store, source, output, payload


def test_selection_configuration_and_roots_are_frozen(setup_plan, tmp_path):
    store, source, output, payload = setup_plan
    plan = store.prepare(payload, source, output, freeze)
    (source / "C.mp4").write_bytes(b"later")
    same = store.prepare(payload, tmp_path / "other-input", tmp_path / "other-output", lambda **kw: pytest.fail("must not refreeze"))
    assert same == plan
    assert [i["filename"] for i in plan["items"]] == ["A.mp4"]
    assert plan["items"][0]["effective_config"]["speed"] == 1.0
    assert plan["input_root"] == str(source.resolve())
    assert plan["state"] == "prepared"


def test_same_key_concurrently_creates_one_plan_and_rejects_changed_intent(setup_plan):
    store, source, output, payload = setup_plan
    with ThreadPoolExecutor(2) as pool:
        plans = list(pool.map(lambda _: store.prepare(payload, source, output, freeze), range(2)))
    assert plans[0]["batch_id"] == plans[1]["batch_id"]
    changed = copy.deepcopy(payload)
    changed["items"][0]["filename"] = "B.mp4"
    with pytest.raises(PlanConflict):
        store.prepare(changed, source, output, freeze)


def test_source_replacement_and_duplicate_output_are_rejected(setup_plan):
    store, source, output, payload = setup_plan
    plan = store.prepare(payload, source, output, freeze)
    (source / "A.mp4").write_bytes(b"changed")
    with pytest.raises(PlanConflict):
        validate_source(plan["items"][0], source)
    (source / "A.mkv").write_bytes(b"same-output")
    payload.update(idempotency_key="conflict", items=[{"filename": "A.mp4"}, {"filename": "A.mkv"}])
    with pytest.raises(PlanConflict):
        store.prepare(payload, source, output, freeze)


@pytest.mark.parametrize("name", ["../A.mp4", "..\\A.mp4", "C:\\A.mp4", "A.mp4 ", "./A.mp4"])
def test_source_traversal_rejected(setup_plan, name):
    store, source, output, payload = setup_plan
    payload["items"] = [{"filename": name}]
    with pytest.raises(ValueError):
        store.prepare(payload, source, output, freeze)


def test_selected_pipeline_only_processes_and_archives_frozen_a(setup_plan, monkeypatch):
    import batch_processor as batch
    import job_tracker
    import shared_state
    store, source, output, payload = setup_plan
    plan = store.prepare(payload, source, output, freeze)
    plan = store.update(plan["batch_id"], state="accepted", execution_id="selection-execution")
    monkeypatch.setattr(batch, "WORKSPACE", str(store.path.parent.parent))
    seen = []
    async def process(path, out, *args, **kwargs):
        seen.append((path, kwargs["job_id"], kwargs["config_snapshot"]))
        (source / "C.mp4").write_bytes(b"added-after-accept")
        (output / "Dubbed_A.mp4").write_bytes(b"result")
        from pipeline_v2.atomic_io import atomic_write_json
        from batch_plan_service import fingerprint
        atomic_write_json(batch._receipt_path(output / "Dubbed_A.mp4"), {
            "job_id": kwargs["job_id"], "input_sha256": fingerprint(path)["sha256"],
            "output_sha256": fingerprint(output / "Dubbed_A.mp4")["sha256"]})
        return True
    monkeypatch.setattr(batch, "process_single_local_video", process)
    monkeypatch.setattr(batch, "_stop_requested", lambda: False)
    job_tracker.start_batch(1, str(source), str(output), job_id="selection-execution")
    asyncio.run(batch.process_batch_folder(str(source), str(output), job_id="selection-execution", batch_plan=plan))
    assert [x[0] for x in seen] == [str(source / "A.mp4")]
    assert seen[0][1] == plan["items"][0]["job_id"]
    assert (source / "processed" / "A.mp4").exists()
    assert (source / "B.mp4").read_bytes() == b"video-b"
    assert (source / "C.mp4").exists()
    assert store.get(plan["batch_id"])["state"] == "completed"


def test_cancel_keeps_slot_until_real_thread_finishes(monkeypatch):
    import batch_processor as batch
    import job_tracker
    entered, release = threading.Event(), threading.Event()
    async def inner(*args, **kwargs):
        job_tracker.start_batch(1, job_id="drain-test")
        await batch.to_thread_drained(lambda: (entered.set(), release.wait(5)))
    monkeypatch.setattr(batch, "_process_batch_folder", inner)
    monkeypatch.setattr(batch, "_stop_requested", lambda: True)
    async def scenario():
        task = asyncio.create_task(batch.process_batch_folder(job_id="drain-test"))
        while not entered.is_set():
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.03)
        assert not task.done()
        assert job_tracker.BATCH_LOCK_FILE.exists()
        assert job_tracker.get_status()["status"] == "stopping"
        with pytest.raises(job_tracker.JobAlreadyRunningError):
            job_tracker.start_batch(1, job_id="wrong-new-execution")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not job_tracker.BATCH_LOCK_FILE.exists()
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_late_callback_cannot_modify_new_execution():
    import job_tracker
    job_tracker.start_batch(1, job_id="new-execution")
    token = job_tracker.current_execution.set("old-execution")
    try:
        job_tracker.update_step(7, "late old callback", percent=99)
        job_tracker.mark_stopped("late stop")
        assert job_tracker.get_status()["job_id"] == "new-execution"
        assert job_tracker.get_status()["status"] == "running"
        assert job_tracker.get_status()["step"] == 0
        assert job_tracker.BATCH_LOCK_FILE.exists()
    finally:
        job_tracker.current_execution.reset(token)
        job_tracker.release_batch("new-execution")


def test_receipt_mismatch_never_archives_source(setup_plan, monkeypatch):
    import batch_processor as batch
    import job_tracker
    from pipeline_v2.atomic_io import atomic_write_json
    store, source, output, payload = setup_plan
    plan = store.prepare(payload, source, output, freeze)
    plan = store.update(plan["batch_id"], state="accepted", execution_id="receipt-owner")
    monkeypatch.setattr(batch, "WORKSPACE", str(store.path.parent.parent))
    async def wrong_result(*args, **kwargs):
        (output / "Dubbed_A.mp4").write_bytes(b"someone-elses-output")
        atomic_write_json(batch._receipt_path(output / "Dubbed_A.mp4"), {"job_id": "other-job"})
        return True
    monkeypatch.setattr(batch, "process_single_local_video", wrong_result)
    monkeypatch.setattr(batch, "_stop_requested", lambda: False)
    job_tracker.start_batch(1, job_id="receipt-owner")
    asyncio.run(batch.process_batch_folder(job_id="receipt-owner", batch_plan=plan))
    assert (source / "A.mp4").is_file()
    assert not (source / "processed" / "A.mp4").exists()
    assert store.get(plan["batch_id"])["state"] == "failed"


def test_live_recycled_pid_does_not_keep_old_lease(monkeypatch):
    import job_tracker
    import psutil
    import os
    job_tracker.BATCH_LOCK_FILE.write_text(json.dumps({"job_id": "old", "pid": os.getpid(),
        "owner_create_time": psutil.Process().create_time() - 100}), encoding="utf-8")
    job_tracker.start_batch(1, job_id="new-owner")
    assert json.loads(job_tracker.BATCH_LOCK_FILE.read_text())["job_id"] == "new-owner"
    job_tracker.release_batch("new-owner")


def test_conflicting_batch_reservations_commit_atomically(setup_plan):
    store, source, output, payload = setup_plan
    first = store.prepare(payload, source, output, freeze)
    other = store.prepare({**payload, "idempotency_key": "other-key"}, source, output, freeze)
    store.update(first["batch_id"], expected_state="prepared", state="scheduled")
    with pytest.raises(PlanConflict):
        store.update(other["batch_id"], expected_state="prepared", state="accepted")
    assert store.get(other["batch_id"])["state"] == "prepared"
