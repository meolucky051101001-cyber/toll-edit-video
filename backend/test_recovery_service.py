import os
import shutil
import tempfile
import time
import pytest
from pathlib import Path
from fastapi.testclient import TestClient

from main import app
from recovery_service import RecoveryService, _ACTIVE_PLAN_TOKENS

client = TestClient(app)

@pytest.fixture
def temp_env():
    td = tempfile.mkdtemp(prefix="test_recovery_")
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

def test_resume_plan_nonexistent_job(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    plan = service.build_resume_plan(
        "nonexistent_video.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["job_id"] == "nonexistent_video"
    assert plan["source_file"]["fingerprint"] is None
    assert plan["summary"]["reusable_stages"] == 0
    assert plan["summary"]["rerun_stages"] == 8
    assert plan["plan_token"] != ""
    assert plan["can_unpause"] is False

def test_resume_plan_with_partial_artifacts(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "test_vid.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"X" * 2000)

    # Create workspace for this job
    ws = temp_env["workspace"] / "test_vid"
    ws.mkdir(parents=True, exist_ok=True)

    # Populate stage 1 & 2 artifacts
    (ws / "original.wav").write_bytes(b"RIFF" + b"\x00" * 500)
    (ws / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 500)
    (ws / "original.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    plan = service.build_resume_plan(
        "test_vid.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["job_id"] == "test_vid"
    assert plan["source_file"]["path"] != "Không tìm thấy trên đĩa"
    assert plan["summary"]["reusable_stages"] == 3  # extract_audio, separate_vocals, transcribe
    assert plan["summary"]["first_rerun_stage"] == "ocr"

    # Check stage 0, 1, 2 status
    stages = {s["stage_id"]: s for s in plan["stages"]}
    assert stages["extract_audio"]["status"] == "reusable"
    assert stages["separate_vocals"]["status"] == "reusable"
    assert stages["transcribe"]["status"] == "reusable"
    assert stages["ocr"]["status"] == "rerun"
    assert stages["translate"]["status"] == "rerun"  # downstream of ocr

def test_source_change_invalidates_checkpoints(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "mod_vid.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"X" * 2000)

    ws = temp_env["workspace"] / "mod_vid"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "original.wav").write_bytes(b"RIFF" + b"\x00" * 500)

    # Save state with old fingerprint
    states = {
        "mod_vid": {
            "source_fingerprint": "completely_different_hash_999",
            "retry_count": 1
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "mod_vid.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    # Check that warning about source change is present and reusable_stages is 0
    assert any("thay đổi" in w for w in plan["warnings"])
    assert plan["summary"]["reusable_stages"] == 0
    assert plan["stages"][0]["status"] == "rerun"

def test_token_validation(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "token_test.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"Y" * 2000)

    ws = temp_env["workspace"] / "token_test"
    ws.mkdir(parents=True, exist_ok=True)

    plan = service.build_resume_plan(
        "token_test.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )

    # Wrong token should fail
    with pytest.raises(ValueError, match="không hợp lệ hoặc đã hết hạn"):
        service.execute_resume_action(
            job_id="token_test",
            mode="recover",
            plan_token="bogus_token_xyz",
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"]
        )

    # Valid token should succeed
    res = service.execute_resume_action(
        job_id="token_test",
        mode="recover",
        plan_token=plan["plan_token"],
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert res["status"] == "success"
    assert res["mode"] == "recover"
    assert res["retry_count"] == 1

def test_unpause_action(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    pause_file = service.control_dir / "video.pause"
    ack_file = service.control_dir / "video.pause.ack"
    pause_file.write_text("1", encoding="utf-8")
    ack_file.write_text("1", encoding="utf-8")

    assert service.is_job_paused() is True

    res = service.execute_resume_action(
        job_id="any_job",
        mode="unpause"
    )
    assert res["status"] == "success"
    assert res["mode"] == "unpause"
    assert not pause_file.exists()
    assert not ack_file.exists()
    assert service.is_job_paused() is False

def test_restart_action_cleans_artifacts(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "restart_test.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"Z" * 2000)

    ws = temp_env["workspace"] / "restart_test"
    ws.mkdir(parents=True, exist_ok=True)
    art = ws / "original.wav"
    art.write_bytes(b"audio data")
    sub = ws / "translated.srt"
    sub.write_text("subtitles", encoding="utf-8")

    states = {
        "restart_test": {
            "retry_count": 2,
            "last_mode": "recover"
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    res = service.execute_resume_action(
        job_id="restart_test",
        mode="restart",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert res["status"] == "success"
    assert res["mode"] == "restart"
    assert not art.exists()
    assert not sub.exists()

    # Verify retry_count reset to 0 in state file
    new_states = service._read_states()
    assert new_states["restart_test"]["retry_count"] == 0

def test_max_retries_limit(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "limit_test.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"L" * 2000)

    ws = temp_env["workspace"] / "limit_test"
    ws.mkdir(parents=True, exist_ok=True)

    states = {
        "limit_test": {
            "retry_count": 3
        }
    }
    service.state_file.write_text(__import__("json").dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "limit_test.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert plan["retry_count"] == 3
    assert any("đã được thử lại 3 lần" in w for w in plan["warnings"])

    # Recover should fail because retry limit exceeded
    with pytest.raises(RuntimeError, match="vượt quá giới hạn thử lại tối đa"):
        service.execute_resume_action(
            job_id="limit_test",
            mode="recover",
            plan_token=plan["plan_token"],
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"]
        )

    # But restart should succeed and reset counter
    res = service.execute_resume_action(
        job_id="limit_test",
        mode="restart",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"]
    )
    assert res["status"] == "success"
    assert res["mode"] == "restart"
    assert service._read_states()["limit_test"]["retry_count"] == 0

def test_api_resume_plan_and_unpause():
    # Test GET resume-plan endpoint
    response = client.get("/api/jobs/test_api_video/resume-plan")
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert "plan_token" in data
    assert "stages" in data
    assert "summary" in data

    # Test POST unpause shortcut
    res_unpause = client.post("/api/jobs/test_api_video/unpause")
    assert res_unpause.status_code == 200
    assert res_unpause.json()["status"] == "success"
    assert res_unpause.json()["mode"] == "unpause"
