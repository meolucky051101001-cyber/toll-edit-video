"""Cloud lifecycle tests: isolated SQLite/files and fake WSL/provider only."""
import importlib.util
import json
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from unittest.mock import Mock

import pytest


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1] / "backend" / "cloud"
    package = "_isolated_cloud_review"
    module = types.ModuleType(package)
    module.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package, module)
    config = types.ModuleType(package + ".cloud_config")
    config.BACKEND_DIR = tmp_path / "backend"
    config.WORKSPACE_DIR = tmp_path / "workspace"
    config.DEFAULT_CONFIG = {
        "db_path": str(tmp_path / "jobs.db"), "results_dir": str(tmp_path / "results"),
        "output_prefix": "cloud_", "cu_budget_daily": 10.0,
        "exec_timeout_seconds": 60, "stop_timeout_seconds": 30,
        "wsl_distro": "TEST", "wsl_user": "TEST",
    }
    config.get_wsl_env_prefix = lambda: ""
    config.to_wsl_path = str
    monkeypatch.setitem(sys.modules, config.__name__, config)
    modules = {}
    for name in ("colab_bridge", "cloud_job_store", "cloud_orchestrator"):
        spec = importlib.util.spec_from_file_location(package + "." + name, root / (name + ".py"))
        imported = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, imported)
        spec.loader.exec_module(imported)
        modules[name] = imported
    now = [time.time()]
    fake_time = types.SimpleNamespace(time=lambda: now[0], strftime=time.strftime)
    monkeypatch.setattr(modules["cloud_job_store"], "time", fake_time)
    monkeypatch.setattr(modules["cloud_orchestrator"], "time", fake_time)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setenv("AUTODUB_OUTPUT_DIR", str(output))
    store = modules["cloud_job_store"].CloudJobStore(rollout_controlled=False)
    return types.SimpleNamespace(**modules, store=store, now=now, output=output, config=config)


@pytest.mark.parametrize("response", [(1, "", "denied"), (-1, "", "timeout"),
                                     (0, "not verified", ""), (0, "AUTODUB_STOP_VERIFIED:{bad}", "")])
def test_stop_all_never_claims_success_without_verified_inventory(cloud, monkeypatch, response):
    calls = []
    def fake(program, args, **kwargs):
        assert program == "/usr/bin/python3"
        assert args == ["-"]
        compile(kwargs["stdin_text"], "cleanup", "exec")
        calls.append((program, args))
        return response
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_argv", fake)
    assert cloud.colab_bridge.stop_all_sessions()[0] is False
    assert len(calls) == 1


def test_stop_all_checks_remaining_before_deleting_local_inventory(cloud, monkeypatch):
    assignments = [types.SimpleNamespace(endpoint="vm")]
    removed = []
    state = types.SimpleNamespace(
        client=types.SimpleNamespace(list_assignments=lambda: assignments,
                                     unassign=lambda endpoint: assignments.clear()),
        store=types.SimpleNamespace(list=lambda: {"session": {}}, remove=removed.append),
    )
    common = types.ModuleType("colab_cli.common")
    common.state = state
    monkeypatch.setitem(sys.modules, "colab_cli.common", common)
    def fake(program, args, **kwargs):
        assert program == "/usr/bin/python3"
        assert args == ["-"]
        code = kwargs["stdin_text"]
        original_path = list(sys.path)
        lines = []
        try:
            exec(compile(code, "cleanup", "exec"), {"print": lines.append})
        except Exception as exc:
            return 1, "", str(exc)
        finally:
            sys.path[:] = original_path
        return 0, "\n".join(lines), ""
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_argv", fake)
    assert cloud.colab_bridge.stop_all_sessions()[0] is True
    assert removed == ["session"]
    assignments.append(types.SimpleNamespace(endpoint="still-running"))
    state.client.unassign = lambda endpoint: None
    removed.clear()
    assert cloud.colab_bridge.stop_all_sessions()[0] is False
    assert removed == []


def test_stopping_one_session_does_not_stop_other_assignments(cloud, monkeypatch):
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_colab_argv", Mock(return_value=(1, "not terminated", "denied")))
    unrelated = Mock(side_effect=AssertionError("global cleanup must not run"))
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_argv", unrelated)
    assert cloud.colab_bridge.stop_session("one")[0] is False
    unrelated.assert_not_called()


@pytest.mark.parametrize("terminal", ["stopped", "error", "completed"])
def test_terminal_state_rejects_late_progress_and_publication(cloud, terminal):
    job = cloud.store.enqueue("video.mp4")
    claimed = cloud.store.claim_job(job.job_id, "terminal-test", enforce_budget=False)
    assert claimed is not None
    if terminal == "completed":
        for state in ("uploading", "preparing", "processing", "downloading", "validating", "completed"):
            assert cloud.store.update_progress(job.job_id, state=state, owner_id=claimed.owner_id,
                                               attempt=claimed.attempt, cleanup_status="verified")
        terminal = "completed"
    elif terminal == "stopped":
        assert cloud.store.update_progress(job.job_id, state="cancel_requested", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, allow_from_cancel=True)
        assert cloud.store.update_progress(job.job_id, state="cancelling", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, allow_from_cancel=True)
        assert cloud.store.update_progress(job.job_id, state="cancelled", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, cleanup_status="verified",
                                           allow_from_cancel=True)
        terminal = "cancelled"
    else:
        assert cloud.store.update_progress(job.job_id, state="failed", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, cleanup_status="verified")
        terminal = "failed"
    assert not cloud.store.update_progress(job.job_id, state="downloading", percent=85)
    assert not cloud.store.update_progress(job.job_id, state="downloading", result_path="final.mp4",
                                           owner_id=claimed.owner_id, attempt=claimed.attempt)
    assert cloud.store.get_job(job.job_id).state == terminal
    assert cloud.store.get_job(job.job_id).result_path is None


def _finish_claimed_job(cloud, job, terminal, cu_actual=1.0, cleanup_status="verified", claimed=None):
    claimed = claimed or cloud.store.claim_job(job.job_id, "billing-test", enforce_budget=False)
    assert claimed is not None
    if terminal == "completed":
        for state in ("uploading", "preparing", "processing", "downloading", "validating"):
            assert cloud.store.update_progress(job.job_id, state=state, owner_id=claimed.owner_id,
                                               attempt=claimed.attempt)
        final_state = "completed"
    elif terminal == "cancelled":
        assert cloud.store.update_progress(job.job_id, state="cancel_requested", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, allow_from_cancel=True)
        assert cloud.store.update_progress(job.job_id, state="cancelling", owner_id=claimed.owner_id,
                                           attempt=claimed.attempt, allow_from_cancel=True)
        final_state = "cancelled"
    else:
        final_state = "failed"
    assert cloud.store.update_progress(
        job.job_id,
        state=final_state,
        cu_actual=cu_actual,
        cleanup_status=cleanup_status,
        owner_id=claimed.owner_id,
        attempt=claimed.attempt,
        allow_from_cancel=True,
    )
    return claimed


@pytest.mark.parametrize("terminal", ["failed", "cancelled", "completed"])
def test_billing_survives_failure_cancel_history_delete_and_restart(cloud, terminal):
    job = cloud.store.enqueue("video.mp4")
    claimed = cloud.store.claim_job(job.job_id, "billing-test", enforce_budget=False)
    assert claimed is not None
    cloud.now[0] += 1800
    _finish_claimed_job(cloud, job, terminal, cu_actual=1.0, claimed=claimed)
    assert cloud.store.get_cu_spent_today(now=cloud.now[0]) == pytest.approx(1.0)
    assert cloud.store.delete_job(job.job_id)
    restarted = cloud.cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
    assert restarted.get_cu_spent_today(now=cloud.now[0]) == pytest.approx(1.0)


def test_billing_splits_cross_midnight_and_blocks_unknown_cleanup(cloud):
    cloud.now[0] = datetime(2026, 11, 15, 23, 30, tzinfo=ZoneInfo("Asia/Ho_Chi_Minh")).timestamp()
    job = cloud.store.enqueue("old.mp4")
    claimed = cloud.store.claim_job(job.job_id, "billing-test", enforce_budget=False)
    assert claimed is not None
    cloud.store.record_runtime_usage(job.job_id, claimed.attempt, 2.0, now=cloud.now[0], enforce_budget=False)
    cloud.now[0] += 3600
    _finish_claimed_job(cloud, job, "failed", cu_actual=2.0,
                        cleanup_status="failed_retry_needed", claimed=claimed)
    assert cloud.store.get_cu_spent_today(now=cloud.now[0]) == pytest.approx(1.0)
    # Soft hiding is allowed when no provider session is registered; the unresolved ledger
    # still blocks admission and remains durable.
    assert cloud.store.delete_job(job.job_id)
    admission = cloud.store.check_admission_allowed(cu_estimated=0.5, now=cloud.now[0])
    assert not admission["allowed"], (admission, cloud.store.list_cost_ledger(job_id=job.job_id))
    assert admission["budget"]["unreconciled_count"] == 1
    cloud.store.record_cleanup_result(
        job.job_id, claimed.attempt, "test-session", verified_stopped=True,
        cleanup_status="verified", attempts=1,
    )
    assert cloud.store.delete_job(job.job_id)
    assert cloud.store.get_cu_spent_today(now=cloud.now[0]) == pytest.approx(1.0)


def test_old_failed_costs_are_migrated_once(cloud):
    job = cloud.store.enqueue("legacy.mp4")
    with cloud.store._connect() as db:
        db.execute("UPDATE cloud_jobs SET state='error', cu_actual=3, completed_at=? WHERE job_id=?",
                   (cloud.now[0], job.job_id))
    migrated = cloud.cloud_job_store.CloudJobStore(cloud.store.db_path)
    assert migrated.get_cu_spent_today() == 3.0
    assert migrated.delete_job(job.job_id)
    assert cloud.cloud_job_store.CloudJobStore(cloud.store.db_path).get_cu_spent_today() == 3.0


@pytest.mark.parametrize("cancel_requested", [False, True])
def test_recover_expired_orphan_settles_ledger_after_lease_expires(cloud, cancel_requested):
    job = cloud.store.enqueue("video.mp4")
    claimed = cloud.store.claim_job(job.job_id, "recover-test", lease_seconds=5, enforce_budget=False)
    assert claimed is not None
    for state in ("uploading", "preparing", "processing"):
        assert cloud.store.update_progress(job.job_id, state=state, owner_id=claimed.owner_id,
                                           attempt=claimed.attempt)
    cloud.store.record_runtime_usage(job.job_id, claimed.attempt, 1.0, now=cloud.now[0], enforce_budget=False)
    if cancel_requested:
        assert cloud.store.request_cancel(job.job_id)["cancelled"]
    cloud.now[0] += 10
    cloud.store.recover(only_expired_leases=True, now=cloud.now[0])
    expected_state = "cancelled" if cancel_requested else "failed"
    assert cloud.store.get_job(job.job_id).state == expected_state
    assert cloud.store.get_cu_spent_today(now=cloud.now[0]) == pytest.approx(1.0)
    assert cloud.store.delete_job(job.job_id)


def fake_provider(cloud, tmp_path, monkeypatch):
    module = cloud.cloud_orchestrator
    adapter = types.SimpleNamespace(
        get_usage=Mock(return_value={"ok": True, "balance": 100}),
        create_session=Mock(return_value=(True, "created")),
        upload_file=Mock(return_value=(True, "ok")),
        exec_argv=Mock(return_value=(True, "ok")),
        stop_session=Mock(return_value=(True, "stopped")),
        get_sessions_status=Mock(return_value={"ok": True, "sessions": []}),
    )
    def download(session, name, directory, **kwargs):
        path = Path(directory) / name
        path.write_bytes(b"fake media" if name.endswith("mp4") else b"{}")
        return True, str(path)
    adapter.download_file = Mock(side_effect=download)
    orchestrator = module.CloudOrchestrator(cloud.store, adapter=adapter)
    orchestrator.gpu_scripts_dir = tmp_path / "scripts"
    orchestrator.gpu_scripts_dir.mkdir()
    orchestrator.verify_local_preflight = Mock(return_value={"env_manifest": {}, "upload_targets": []})
    orchestrator.validate_downloaded_output = Mock(side_effect=lambda path, *_args, **_kwargs: {
        "sha256": __import__("hashlib").sha256(Path(path).read_bytes()).hexdigest(),
        "size_bytes": Path(path).stat().st_size,
        "qc_report": {}, "remote_metadata": {}, "local_probe": {},
    })
    return orchestrator


def test_cancel_during_allocation_stops_vm_and_never_uploads(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video")
    job = cloud.store.claim_job(job.job_id, "worker-test", lease_seconds=7200, enforce_budget=False)
    assert job is not None
    def allocate(*args, **kwargs):
        cloud.store.cancel_job(job.job_id)
        cloud.now[0] += 1800
        return True, "created after cancel"
    runner.adapter.create_session.side_effect = allocate
    assert runner.process_job(job) is False
    runner.adapter.upload_file.assert_not_called()
    runner.adapter.stop_session.assert_called_once()
    assert cloud.store.get_job(job.job_id).state == "cancelled"
    assert cloud.store.get_cu_spent_today() == 0.575


def test_upload_failure_is_billed_and_cleanup_failure_blocks_admission(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video")
    job = cloud.store.claim_job(job.job_id, "worker-test", lease_seconds=7200, enforce_budget=False)
    assert job is not None
    def upload(*args, **kwargs):
        cloud.now[0] += 1800
        return False, "upload failure"
    runner.adapter.upload_file.side_effect = upload
    runner.adapter.stop_session.return_value = (False, "provider timeout")
    assert runner.process_job(job) is False
    assert cloud.store.get_job(job.job_id).state == "failed"
    assert cloud.store.get_job(job.job_id).cleanup_status == "failed_retry_needed"
    assert "provider timeout" in cloud.store.get_job(job.job_id).cleanup_last_error
    assert cloud.store.get_cu_spent_today() == 0.575
    admission = cloud.store.check_admission_allowed(cu_estimated=0.5)
    assert not admission["allowed"], (admission, cloud.store.list_cost_ledger(job_id=job.job_id))
    assert admission["budget"]["unreconciled_count"] == 1


def test_cancel_during_download_never_publishes(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video", video_name="video.mp4")
    job = cloud.store.claim_job(job.job_id, "worker-test", lease_seconds=7200, enforce_budget=False)
    assert job is not None
    original = runner.adapter.download_file.side_effect
    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        cloud.store.cancel_job(job.job_id)
        return result
    runner.adapter.download_file.side_effect = cancel
    assert runner.process_job(job) is False
    assert list(cloud.output.iterdir()) == []
    assert cloud.store.get_job(job.job_id).state == "cancelled"


def test_success_cleans_up_before_publish_and_preserves_argv_arguments(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    video_url = "https://example.invalid/a%27b"
    job = cloud.store.enqueue(video_url, video_name="video.mp4", voice="BV562_streaming")
    job = cloud.store.claim_job(job.job_id, "worker-test", lease_seconds=7200, enforce_budget=False)
    assert job is not None
    def stop(*args):
        assert list(cloud.output.iterdir()) == []
        cloud.now[0] += 60
        return True, "stopped"
    runner.adapter.stop_session.side_effect = stop
    assert runner.process_job(job) is True
    assert cloud.store.get_job(job.job_id).state == "completed"
    assert cloud.store.get_job(job.job_id).cu_actual > 0
    pipeline_call = next(
        call for call in runner.adapter.exec_argv.call_args_list
        if "full_pipeline.py" in call.args[1]
    )
    tokens = pipeline_call.args[1]
    assert tokens[tokens.index("--input") + 1] == video_url
    assert tokens[tokens.index("--voice") + 1] == job.voice
    assert len(list(cloud.output.glob("cloud_video__*.mp4"))) == 1


def test_cleanup_failure_does_not_publish_successful_output(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video", video_name="video.mp4")
    job = cloud.store.claim_job(job.job_id, "worker-test", lease_seconds=7200, enforce_budget=False)
    assert job is not None
    runner.adapter.stop_session.return_value = (False, "provider timeout")

    assert runner.process_job(job) is False
    assert list(cloud.output.iterdir()) == []
    assert cloud.store.get_job(job.job_id).state == "failed"
    assert cloud.store.get_job(job.job_id).cleanup_status == "failed_retry_needed"
    assert cloud.store.get_cu_budget_info()["unreconciled_count"] == 1
