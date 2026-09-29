import os
import shutil
import tempfile
import time
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from dashboard_monitor import app
from recovery_service import RecoveryService, _ACTIVE_PLAN_TOKENS

client = TestClient(app)

@pytest.fixture
def temp_env():
    td = tempfile.mkdtemp(prefix="test_recovery_v2_")
    root = Path(td)
    workspace = root / "workspace"
    input_dir = root / "input"
    output_dir = root / "output"
    workspace.mkdir(parents=True, exist_ok=True)
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    yield {
        "root": root,
        "workspace": workspace,
        "input_dir": input_dir,
        "output_dir": output_dir
    }
    shutil.rmtree(td, ignore_errors=True)

def test_resume_plan_nonexistent_job_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    plan = service.build_resume_plan(
        "nonexistent_video_v2.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["job_id"] == "nonexistent_video_v2"
    assert plan["source_file"]["fingerprint"] is None
    assert plan["summary"]["reusable_stages"] == 0
    assert plan["summary"]["rerun_stages"] == 8
    assert plan["plan_token"] != ""
    assert plan["can_unpause"] is False

def test_resume_plan_with_v2_artifacts(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "v2_sample.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"V2" * 1500)

    # Create workspace for this job with V2 pipeline artifacts
    ws = temp_env["workspace"] / "v2_sample"
    ws.mkdir(parents=True, exist_ok=True)
    audio_dir = ws / "pipeline_v2" / "artifacts" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    asr_dir = ws / "pipeline_v2" / "artifacts" / "asr"
    asr_dir.mkdir(parents=True, exist_ok=True)

    (audio_dir / "source_audio.wav").write_bytes(b"RIFF" + b"\x00" * 400)
    (audio_dir / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 400)
    (asr_dir / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n", encoding="utf-8")

    plan = service.build_resume_plan(
        "v2_sample.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["job_id"] == "v2_sample"
    assert plan["summary"]["reusable_stages"] == 3
    assert plan["summary"]["first_rerun_stage"] == "ocr"

    stages = {s["stage_id"]: s for s in plan["stages"]}
    assert stages["extract_audio"]["status"] == "reusable"
    assert stages["separate_vocals"]["status"] == "reusable"
    assert stages["transcribe"]["status"] == "reusable"
    assert stages["ocr"]["status"] == "rerun"

def test_source_change_invalidates_checkpoints_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "changed_v2.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"NEW" * 1500)

    ws = temp_env["workspace"] / "changed_v2"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "original.wav").write_bytes(b"RIFF" + b"\x00" * 500)

    states = {
        "changed_v2": {
            "source_fingerprint": "old_hash_v2_999",
            "retry_count": 1
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "changed_v2.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert any("thay đổi" in w for w in plan["warnings"])
    assert plan["summary"]["reusable_stages"] == 0

def test_token_validation_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "token_v2.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"TOK" * 1500)

    ws = temp_env["workspace"] / "token_v2"
    ws.mkdir(parents=True, exist_ok=True)

    plan = service.build_resume_plan(
        "token_v2.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )

    with pytest.raises(ValueError, match="không hợp lệ hoặc đã hết hạn"):
        service.execute_resume_action(
            job_id="token_v2",
            mode="recover",
            plan_token="invalid_token_xyz",
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"]
        )

    res = service.execute_resume_action(
        job_id="token_v2",
        mode="recover",
        plan_token=plan["plan_token"],
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert res["status"] == "success"
    assert res["mode"] == "recover"
    assert res["retry_count"] == 1

def test_unpause_action_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    pause_file = service.control_dir / "video.pause"
    pause_file.write_text("1", encoding="utf-8")

    assert service.is_job_paused() is True

    res = service.execute_resume_action(
        job_id="test_pause_v2",
        mode="unpause"
    )
    assert res["status"] == "success"
    assert not pause_file.exists()
    assert service.is_job_paused() is False

def test_restart_action_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "restart_v2.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"RES" * 1500)

    ws = temp_env["workspace"] / "restart_v2"
    ws.mkdir(parents=True, exist_ok=True)
    pipe_dir = ws / "pipeline_v2"
    pipe_dir.mkdir(parents=True, exist_ok=True)
    (pipe_dir / "temp.wav").write_bytes(b"temp")

    states = {
        "restart_v2": {
            "retry_count": 2,
            "last_mode": "recover"
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    res = service.execute_resume_action(
        job_id="restart_v2",
        mode="restart",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert res["status"] == "success"
    assert res["mode"] == "restart"
    assert not pipe_dir.exists()

    new_states = service._read_states()
    assert new_states["restart_v2"]["retry_count"] == 0

def test_max_retries_limit_v2(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "limit_v2.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"LIM" * 1500)

    ws = temp_env["workspace"] / "limit_v2"
    ws.mkdir(parents=True, exist_ok=True)

    states = {
        "limit_v2": {
            "retry_count": 3
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "limit_v2.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["retry_count"] == 3

    with pytest.raises(RuntimeError, match="vượt quá giới hạn thử lại tối đa"):
        service.execute_resume_action(
            job_id="limit_v2",
            mode="recover",
            plan_token=plan["plan_token"],
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"]
        )

def test_api_endpoints_v2():
    response = client.get("/api/jobs/sample_test_v2/resume-plan")
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert "plan_token" in data
    assert "stages" in data

    res_unpause = client.post("/api/jobs/sample_test_v2/unpause")
    assert res_unpause.status_code == 200
    assert res_unpause.json()["status"] == "success"
