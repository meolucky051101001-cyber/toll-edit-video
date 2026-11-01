import json

import stop_service


def _read_audit(workspace):
    path = workspace / "bot_system" / "stop_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_request_stop_writes_both_flags_and_audit(tmp_path):
    status_file = tmp_path / "bot_system" / "job_status.json"

    stop_service.request_stop(
        tmp_path, status_file=status_file, source="unit-test", reason="verify", job_id="job-1"
    )

    control = tmp_path / "control" / "video.stop"
    assert control.exists()
    assert float(control.read_text(encoding="utf-8")) > 0

    request = json.loads((tmp_path / "bot_system" / "stop_request.json").read_text(encoding="utf-8"))
    assert request["job_id"] == "job-1"
    assert request["source"] == "unit-test"
    assert request["reason"] == "verify"
    assert request["requested_at"] > 0

    assert stop_service.is_stop_requested(tmp_path) is True
    assert stop_service.is_stop_requested(tmp_path, status_file=status_file) is True
    assert stop_service.stop_details(tmp_path, status_file=status_file)["job_id"] == "job-1"

    events = _read_audit(tmp_path)
    assert events[-1]["event"] == "request"
    assert events[-1]["source"] == "unit-test"


def test_clear_stop_removes_flags_and_audits(tmp_path):
    status_file = tmp_path / "bot_system" / "job_status.json"
    stop_service.request_stop(tmp_path, status_file=status_file, source="unit-test")

    stop_service.clear_stop(tmp_path, status_file=status_file, source="unit-test", reason="done")

    assert not (tmp_path / "control" / "video.stop").exists()
    assert not (tmp_path / "bot_system" / "stop_request.json").exists()
    assert stop_service.is_stop_requested(tmp_path) is False
    events = _read_audit(tmp_path)
    assert [event["event"] for event in events] == ["request", "clear"]


def test_clear_stop_is_safe_when_no_flags_exist(tmp_path):
    stop_service.clear_stop(tmp_path, source="unit-test")
    assert stop_service.is_stop_requested(tmp_path) is False


def test_stop_details_returns_none_for_missing_or_broken_json(tmp_path):
    assert stop_service.stop_details(tmp_path) is None
    (tmp_path / "bot_system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "bot_system" / "stop_request.json").write_text("{broken", encoding="utf-8")
    assert stop_service.stop_details(tmp_path) is None


def test_status_file_outside_workspace_falls_back_to_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "elsewhere" / "job_status.json"
    outside.parent.mkdir(parents=True)

    stop_service.request_stop(workspace, status_file=outside, source="unit-test")

    assert (workspace / "bot_system" / "stop_request.json").exists()
    assert not (outside.parent / "stop_request.json").exists()
