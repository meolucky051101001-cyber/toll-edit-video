import copy
import json

import pytest

import job_tracker


@pytest.fixture
def tracker_workspace(tmp_path):
    old_paths = (job_tracker.WORKSPACE, job_tracker.STATUS_FILE, job_tracker.BATCH_LOCK_FILE)
    old_state = copy.deepcopy(job_tracker._CURRENT_STATE)
    workspace = tmp_path / "workspace"
    (workspace / "control").mkdir(parents=True)
    (workspace / "bot_system").mkdir(parents=True)
    job_tracker.WORKSPACE = workspace
    job_tracker.STATUS_FILE = workspace / "bot_system" / "job_status.json"
    job_tracker.BATCH_LOCK_FILE = workspace / "bot_system" / "batch.lock"
    with job_tracker._LOCK:
        job_tracker._CURRENT_STATE.clear()
        job_tracker._CURRENT_STATE.update(copy.deepcopy(job_tracker._DEFAULT_STATE))
    yield workspace
    job_tracker.WORKSPACE, job_tracker.STATUS_FILE, job_tracker.BATCH_LOCK_FILE = old_paths
    with job_tracker._LOCK:
        job_tracker._CURRENT_STATE.clear()
        job_tracker._CURRENT_STATE.update(old_state)


def _write_stop_flags(workspace):
    (workspace / "control" / "video.stop").write_text("1", encoding="utf-8")
    (workspace / "bot_system" / "stop_request.json").write_text(
        json.dumps({"job_id": "old-job"}), encoding="utf-8"
    )


def test_clears_stale_stop_flags_when_idle(tracker_workspace):
    _write_stop_flags(tracker_workspace)

    assert job_tracker.clear_stale_stop_request() is True

    assert not (tracker_workspace / "control" / "video.stop").exists()
    assert not (tracker_workspace / "bot_system" / "stop_request.json").exists()
    assert job_tracker.get_status()["stop_requested"] is False


def test_keeps_stop_flags_while_a_job_is_active(tracker_workspace):
    job_tracker.start_batch(1, "in", "out")
    _write_stop_flags(tracker_workspace)

    assert job_tracker.clear_stale_stop_request() is False
    assert (tracker_workspace / "control" / "video.stop").exists()
