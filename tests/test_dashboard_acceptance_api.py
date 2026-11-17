import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def test_schedule_holds_exact_plan_and_cancel_does_not_start(api, monkeypatch):
    from batch_plan_service import BatchPlanStore
    called = []
    async def runner(*args, **kwargs):
        called.append(kwargs.get("batch_plan"))
    monkeypatch.setattr(api.main, "batch_runner", runner)
    response = api.client.post("/api/jobs/batch", json={"items": [{"filename": "A.mp4"}],
                               "trigger": False, "idempotency_key": "schedule-a"})
    plan = response.json()
    schedule_payload = {"target_kind": "local", "batch_id": plan["batch_id"], "time": "02:00",
                        "timezone": "Asia/Bangkok", "operation_id": "local-night-a", "expected_revision": plan['revision']}
    scheduled = api.client.post("/api/schedule", json=schedule_payload)
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["target_kind"] == "local"
    assert not called
    store = BatchPlanStore(api.workspace)
    assert store.get(plan["batch_id"])["state"] == "scheduled"
    assert api.client.post(f"/api/batches/{plan['batch_id']}/start").status_code == 409
    replay = api.client.post("/api/schedule", json=schedule_payload)
    assert replay.json()["schedule_id"] == scheduled.json()["schedule_id"]
    identity = {key: scheduled.json()[key] for key in ('schedule_id', 'generation', 'operation_id')}
    assert api.client.request('DELETE', '/api/schedule', json=identity).json()['state'] == 'canceled'
    assert not called
    assert store.get(plan["batch_id"])["state"] == "scheduled"
    # Explicit manual start is the only action that can release a canceled local hold.
    current = api.client.get(f"/api/batches/{plan['batch_id']}").json()
    started = api.client.post(f"/api/batches/{plan['batch_id']}/start", json=current['action_identity'])
    assert started.status_code == 202, started.text


def test_direct_api_uses_shared_lease_before_media_work(api, monkeypatch):
    import job_tracker
    job_tracker.start_batch(1, job_id="other-owner")
    touched = []
    monkeypatch.setattr(api.main, "extract_audio_from_video", lambda *a: touched.append(a))
    result = api.client.post("/api/generate-subtitles", data={"video_path": str(api.source / "A.mp4")})
    assert result.status_code == 409
    assert touched == []
    assert job_tracker.BATCH_LOCK_FILE.exists()


def test_retry_from_archive_does_not_copy_before_admission(api, monkeypatch):
    import job_tracker
    archived = api.source / "processed"
    archived.mkdir()
    (archived / "old.mp4").write_bytes(b"old-source")
    job_tracker.start_batch(1, job_id="other-owner")
    response = api.client.post("/api/retry-video", json={"filename": "old.mp4", "operation_id": "retry-old"})
    assert response.status_code == 409, response.text
    assert not (api.source / "old.mp4").exists()
    assert (archived / "old.mp4").read_bytes() == b"old-source"
    plan = api.client.get("/api/batch-operations/retry-old").json()
    assert plan["state"] == "prepared"
    assert plan["archive_source"] is False


@pytest.fixture
def api(tmp_path, monkeypatch):
    import main
    import batch_processor
    import job_tracker
    import job_config_service
    import preset_service
    workspace = tmp_path / "workspace"
    source, output = tmp_path / "input", tmp_path / "output"
    workspace.mkdir(); source.mkdir(); output.mkdir()
    (source / "A.mp4").write_bytes(b"A")
    (source / "B.mp4").write_bytes(b"B")
    for module in (main, batch_processor):
        monkeypatch.setattr(module, "WORKSPACE", str(workspace))
    monkeypatch.setattr(main, "get_input_dir", lambda: source)
    monkeypatch.setattr(main, "get_output_dir", lambda: output)
    monkeypatch.setattr(main, "BATCH_TASK", None)
    monkeypatch.setattr(main, "BATCH_ADMISSION_LOCK", asyncio.Lock())
    monkeypatch.setattr(main, "UNIFIED_PIPELINE_LOCK", asyncio.Lock())
    for key, value in (("WORKSPACE_DIR", workspace), ("CONTROL_DIR", workspace / "control"),
                       ("FROZEN_FILE", workspace / "control" / "job_frozen_configs.json"),
                       ("DRAFTS_FILE", workspace / "control" / "video_draft_configs.json")):
        monkeypatch.setattr(job_config_service, key, value)
    monkeypatch.setattr(preset_service, "PRESETS_FILE", workspace / "control" / "presets.json")
    monkeypatch.setattr(main.app.state, "pipeline_runtime", main, raising=False)
    client = TestClient(main.app, headers={"X-Local-Control-Token": main.LOCAL_TOKEN})
    return SimpleNamespace(client=client, main=main, source=source, output=output, workspace=workspace)


def test_prepare_is_durable_without_running_and_key_conflict_is_409(api, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Prepared plan must never start work")
    monkeypatch.setattr(api.main, "batch_runner", forbidden)
    payload = {"items": [{"filename": "A.mp4", "overrides": {"speed": 1.0, "bgm_volume_db": -2, "dubbing_volume_db": 1}}],
               "trigger": False, "idempotency_key": "api-select-a"}
    result = api.client.post("/api/jobs/batch", json=payload)
    assert result.status_code == 201, result.text
    data = result.json()
    assert data["state"] == "prepared"
    assert data["items"][0]["effective_config"]["speed"] == 1.0
    replay = api.client.get("/api/batch-operations/api-select-a")
    assert replay.json()["batch_id"] == data["batch_id"]
    payload["items"][0]["filename"] = "B.mp4"
    assert api.client.post("/api/jobs/batch", json=payload).status_code == 409


@pytest.mark.parametrize("value", ["NaN", "Infinity", "oops", 10])
def test_invalid_numeric_override_is_rejected(api, value):
    result = api.client.post("/api/config/resolve", json={"overrides": {"speed": value}})
    assert result.status_code == 422


def test_compare_unicode_percent_and_archive_uses_receipt(api, monkeypatch):
    import batch_processor
    from batch_plan_service import fingerprint
    from pipeline_v2.atomic_io import atomic_write_json
    from urllib.parse import quote
    name = "Việt 中文 %25 # ' clip.mov"
    output_name = "Dubbed_" + Path(name).stem + ".mp4"
    archive = api.source / "processed"
    archive.mkdir()
    original = archive / name
    original.write_bytes(b"unique-source")
    (api.output / output_name).write_bytes(b"output")
    atomic_write_json(batch_processor._receipt_path(api.output / output_name), {
        "source_path": str(original), "source_name": name, "input_sha256": fingerprint(original)["sha256"]})
    result = api.client.get("/api/media/compare", params={"filename": output_name})
    assert result.status_code == 200, result.text
    assert result.json()["original"]["name"] == name
    assert api.client.get(result.json()["original"]["url"]).content == b"unique-source"
    assert api.client.get(result.json()["dubbed"]["url"]).content == b"output"


def test_stop_ack_keeps_tracker_active_and_lock(api):
    import job_tracker
    job_tracker.start_batch(1, job_id="api-stopping")
    try:
        response = api.client.post("/api/stop-batch")
        assert response.status_code == 202
        assert response.json()["status"] == "stopping"
        assert job_tracker.BATCH_LOCK_FILE.exists()
        assert job_tracker.get_status()["active"]
        assert api.client.post("/api/stop-batch").json()["operation_id"] == response.json()["operation_id"]
    finally:
        job_tracker.release_batch("api-stopping")
