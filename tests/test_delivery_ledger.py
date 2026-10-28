import json

import delivery_ledger


def _events(workspace):
    path = workspace / "bot_system" / "delivery_ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_captures_file_metadata(tmp_path):
    workspace = tmp_path / "workspace"
    output = tmp_path / "out.mp4"
    output.write_bytes(b"x" * 128)

    assert delivery_ledger.record(
        "output_verified", path=output, job_id="job-1", workspace=workspace,
        extra={"video_name": "a.mp4"},
    )

    entry = _events(workspace)[-1]
    assert entry["event"] == "output_verified"
    assert entry["job_id"] == "job-1"
    assert entry["video_name"] == "a.mp4"
    assert entry["size"] == 128
    assert entry["path"] == str(output)


def test_record_marks_missing_file(tmp_path):
    workspace = tmp_path / "workspace"

    delivery_ledger.record("output_verified", path=tmp_path / "gone.mp4", workspace=workspace)

    assert _events(workspace)[-1]["missing"] is True


def test_safe_delete_removes_file_inside_root(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    target = root / "scratch.wav"
    target.write_bytes(b"0" * 10)

    assert delivery_ledger.safe_delete(
        target, roots=[root], reason="cleanup", job_id="job-1", workspace=tmp_path / "workspace"
    )

    assert not target.exists()
    events = _events(tmp_path / "workspace")
    assert [event["event"] for event in events] == ["delete", "deleted"]
    assert events[0]["size"] == 10


def test_safe_delete_refuses_outside_root(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    outside = tmp_path / "banve" / "final.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"keep me")

    assert not delivery_ledger.safe_delete(
        outside, roots=[root], reason="cleanup", workspace=tmp_path / "workspace"
    )

    assert outside.exists()
    assert _events(tmp_path / "workspace")[-1]["event"] == "delete_refused"


def test_safe_delete_refuses_size_mismatch(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    target = root / "clip.bin"
    target.write_bytes(b"123")

    assert not delivery_ledger.safe_delete(
        target, roots=[root], reason="cleanup",
        workspace=tmp_path / "workspace", expected_size=999,
    )

    assert target.exists()
    assert _events(tmp_path / "workspace")[-1]["event"] == "delete_refused"
