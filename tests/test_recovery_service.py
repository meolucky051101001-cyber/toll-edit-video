"""Isolated regression tests for RecoveryService and Phase D recovery endpoints (Gói C6)."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import phase_d_routes as routes
from backend.recovery_service import RecoveryService, _ACTIVE_PLAN_TOKENS


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

    try:
        yield {
            "root": root,
            "workspace": workspace,
            "input_dir": input_dir,
            "output_dir": output_dir,
        }
    finally:
        _ACTIVE_PLAN_TOKENS.clear()
        shutil.rmtree(td, ignore_errors=True)


def test_resume_plan_nonexistent_job(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    plan = service.build_resume_plan(
        "nonexistent_video.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert plan["job_id"] == "nonexistent_video"
    assert plan["source_file"]["fingerprint"] is None
    assert plan["summary"]["reusable_stages"] == 0
    assert plan["summary"]["rerun_stages"] == 8
    assert plan["plan_token"] != ""
    assert plan["can_unpause"] is False
    assert plan["can_restart"] is False


def record_audio_stage(service, source, workspace, job_id):
    record = service.manifests.get_or_create_manifest(
        job_id, source, effective_config={"output_dir": str(workspace)},
    )
    audio = workspace / "original.wav"
    audio.write_bytes(b"RIFF fixture")
    service.manifests.record_stage_complete(job_id, "extract_audio", {"original_audio": str(audio)})
    return record


def test_resume_plan_with_partial_artifacts(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "test_vid.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"X" * 2000)

    ws = temp_env["workspace"] / "test_vid"
    ws.mkdir(parents=True, exist_ok=True)

    (ws / "original.wav").write_bytes(b"RIFF" + b"\x00" * 500)
    (ws / "vocals.wav").write_bytes(b"RIFF" + b"\x00" * 500)
    (ws / "original.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n", encoding="utf-8")

    legacy = service.build_resume_plan("test_vid.mp4", temp_env["input_dir"], temp_env["output_dir"])
    assert legacy["summary"]["reusable_stages"] == 0
    assert legacy["can_restart"]

    record_audio_stage(service, video_file, ws, "test_vid")

    plan = service.build_resume_plan(
        "test_vid.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert plan["job_id"] == "test_vid"
    assert plan["source_file"]["path"] != "Không tìm thấy trên đĩa"
    assert plan["summary"]["reusable_stages"] == 1
    assert plan["summary"]["first_rerun_stage"] == "separate_vocals"

    stages = {s["stage_id"]: s for s in plan["stages"]}
    assert stages["extract_audio"]["status"] == "reusable"
    assert stages["separate_vocals"]["status"] == "rerun"
    assert stages["speech_asr"]["status"] == "rerun"
    assert stages["visual_ocr"]["status"] == "rerun"
    assert stages["ai_translation"]["status"] == "rerun"


def test_source_change_invalidates_checkpoints(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "mod_vid.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"X" * 2000)

    ws = temp_env["workspace"] / "mod_vid"
    ws.mkdir(parents=True, exist_ok=True)
    record_audio_stage(service, video_file, ws, "mod_vid")
    video_file.write_bytes(b"new content")

    states = {
        "mod_vid": {
            "source_fingerprint": "completely_different_hash_999",
            "retry_count": 1,
        }
    }
    service.state_file.write_text(json.dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "mod_vid.mp4",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert any("thay đổi" in w for w in plan["warnings"])
    assert plan["summary"]["reusable_stages"] == 0
    assert plan["stages"][0]["status"] == "rerun"


def test_token_validation(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "token_test.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"Y" * 2000)

    ws = temp_env["workspace"] / "token_test"
    ws.mkdir(parents=True, exist_ok=True)
    record_audio_stage(service, video_file, ws, "token_test")

    plan = service.build_resume_plan(
        "token_test",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )

    with pytest.raises(ValueError, match="không hợp lệ hoặc đã hết hạn"):
        service.execute_resume_action(
            job_id="token_test",
            mode="recover",
            plan_token="bogus_token_xyz",
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"],
        )

    res = service.execute_resume_action(
        job_id="token_test",
        mode="recover",
        plan_token=plan["plan_token"],
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert res["status"] == "success"
    assert res["mode"] == "recover"
    assert res["retry_count"] == 1
    assert res["pipeline_job_id"] == "token_test"
    with pytest.raises(ValueError, match="không hợp lệ hoặc đã hết hạn"):
        service.execute_resume_action("token_test", "recover", plan["plan_token"], input_dir=temp_env["input_dir"])


def test_unpause_action():
    runtime = SimpleNamespace(job_tracker=Mock(get_status=Mock(return_value={
        "video_name": "expected.mp4", "execution_id": "execution", "active": True, "generation": 2,
    })))
    with patch("video_pause.state", return_value="paused"), patch("video_pause.resume") as resume:
        with pytest.raises(routes.HTTPException) as error:
            routes._unpause(runtime, "wrong")
        assert error.value.status_code == 409
        resume.assert_not_called()
        assert routes._unpause(runtime, "expected", "execution", 2)["status"] == "success"
        resume.assert_called_once_with("execution", 2)


def test_restart_action_uses_new_workspace(temp_env):
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
            "last_mode": "recover",
        }
    }
    service.state_file.write_text(json.dumps(states), encoding="utf-8")

    plan = service.build_resume_plan("restart_test", temp_env["input_dir"], temp_env["output_dir"])
    res = service.execute_resume_action(
        job_id="restart_test",
        mode="restart",
        plan_token=plan["plan_token"],
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert res["status"] == "success"
    assert res["mode"] == "restart"
    assert art.read_bytes() == b"audio data"
    assert sub.read_text() == "subtitles"
    assert Path(res["work_dir"]).parent == ws.parent
    assert Path(res["work_dir"]) != ws

    new_states = service._read_states()
    assert new_states["restart_test"]["retry_count"] == 0


def test_recovery_matches_exact_names_and_archived_source(temp_env):
    service = RecoveryService(str(temp_env["workspace"]))
    source_root, output = temp_env["input_dir"], temp_env["output_dir"]
    (source_root / "clip_backup.mp4").write_bytes(b"unrelated")
    assert not service.build_resume_plan("clip.mp4", source_root, output)["can_restart"]

    source = source_root / "clip.mp4"
    source.write_bytes(b"actual source")
    ws = temp_env["workspace"] / "owned"
    ws.mkdir()
    record_audio_stage(service, source, ws, "known-job")
    archive = source_root / "processed"
    archive.mkdir()
    moved = source.rename(archive / source.name)
    plan = service.build_resume_plan("known-job", source_root, output)
    assert plan["can_recover"]
    assert Path(plan["source_file"]["path"]) == moved
    moved.write_bytes(b"other content")
    with pytest.raises(RuntimeError, match="đã thay đổi"):
        service.execute_resume_action("known-job", "recover", plan["plan_token"],
                                      input_dir=source_root, output_dir=output)


def test_recovery_blocks_ambiguous_destination_and_artifacts_outside_job(temp_env):
    service = RecoveryService(str(temp_env["workspace"]))
    source_root, output = temp_env["input_dir"], temp_env["output_dir"]
    source = source_root / "same.mp4"
    source.write_bytes(b"actual source")
    ws = temp_env["workspace"] / "owned"
    ws.mkdir()
    record_audio_stage(service, source, ws, "known-job")
    other = temp_env["workspace"] / "unrelated.wav"
    other.write_bytes(b"other job")
    service.manifests.record_stage_complete("known-job", "extract_audio", {"original_audio": str(other)})
    plan = service.build_resume_plan("known-job", source_root, output)
    assert not plan["can_recover"]
    assert plan["can_restart"]

    delivered = output / "Dubbed_same.mp4"
    delivered.write_bytes(b"unrelated output")
    plan = service.build_resume_plan("known-job", source_root, output)
    assert not plan["can_restart"]
    with pytest.raises(RuntimeError):
        service.execute_resume_action("known-job", "restart", plan["plan_token"], input_dir=source_root, output_dir=output)
    assert other.read_bytes() == b"other job"
    assert delivered.read_bytes() == b"unrelated output"


def test_max_retries_limit(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    video_file = temp_env["input_dir"] / "limit_test.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"L" * 2000)

    ws = temp_env["workspace"] / "limit_test"
    ws.mkdir(parents=True, exist_ok=True)

    states = {
        "limit_test": {
            "retry_count": 3,
        }
    }
    service.state_file.write_text(json.dumps(states), encoding="utf-8")

    plan = service.build_resume_plan(
        "limit_test",
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert plan["retry_count"] == 3
    assert any("đã được thử lại 3 lần" in w for w in plan["warnings"])

    with pytest.raises(RuntimeError, match="vượt quá giới hạn thử lại tối đa"):
        service.execute_resume_action(
            job_id="limit_test",
            mode="recover",
            plan_token=plan["plan_token"],
            input_dir=temp_env["input_dir"],
            output_dir=temp_env["output_dir"],
        )

    res = service.execute_resume_action(
        job_id="limit_test",
        mode="restart",
        plan_token=plan["plan_token"],
        input_dir=temp_env["input_dir"],
        output_dir=temp_env["output_dir"],
    )
    assert res["status"] == "success"
    assert res["mode"] == "restart"
    assert service._read_states()["limit_test"]["retry_count"] == 0


def test_api_resume_plan_and_unpause(temp_env):
    service = RecoveryService(workspace_path=str(temp_env["workspace"]))
    app = FastAPI()
    app.include_router(routes.phase_d_router)
    app.state.pipeline_runtime = SimpleNamespace(
        UNIFIED_PIPELINE_LOCK=asyncio.Lock(),
        BATCH_TASK=None,
        single_video_runner=Mock(),
        get_input_dir=lambda: temp_env["input_dir"],
        get_output_dir=lambda: temp_env["output_dir"],
        job_tracker=Mock(get_status=Mock(return_value={"active": False})),
        shared_state=SimpleNamespace(stop_requested=False),
    )

    with patch.object(routes, "_service", service):
        client = TestClient(app)
        response = client.get("/api/jobs/test_api_video/resume-plan")
        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == "test_api_video"
        assert "plan_token" in data
        assert "stages" in data
        assert "summary" in data

        assert client.post("/api/jobs/test_api_video/unpause").status_code == 409
        app.state.pipeline_runtime.job_tracker.get_status.return_value = {
            "active": True, "video_name": "test_api_video.mp4", "execution_id": "paused-id", "generation": 8,
        }
        with patch("video_pause.state", return_value="paused"), patch("video_pause.resume") as resume:
            paused_plan = client.get("/api/jobs/test_api_video/resume-plan").json()
            assert paused_plan["paused_execution"] == {"execution_id": "paused-id", "generation": 8}
            assert client.post("/api/jobs/test_api_video/unpause").status_code == 409
            assert client.post("/api/jobs/test_api_video/unpause", json={"execution_id": "previous-id", "generation": 7}).status_code == 409
            assert client.post("/api/jobs/test_api_video/resume", json={"mode": "unpause", "execution_id": "paused-id", "generation": 7}).status_code == 409
            resume.assert_not_called()
            res_unpause = client.post("/api/jobs/test_api_video/unpause", json=paused_plan["paused_execution"])
        assert res_unpause.status_code == 200
        resume.assert_called_once_with("paused-id", 8)
