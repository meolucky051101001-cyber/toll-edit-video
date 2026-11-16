"""Cloud lifecycle tests: isolated SQLite/files and fake WSL/provider only."""
import importlib.util
import json
import shlex
import sys
import time
import types
from datetime import datetime
from pathlib import Path
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
    store = modules["cloud_job_store"].CloudJobStore()
    return types.SimpleNamespace(**modules, store=store, now=now, output=output, config=config)


@pytest.mark.parametrize("response", [(1, "", "denied"), (-1, "", "timeout"),
                                     (0, "not verified", ""), (0, "AUTODUB_STOP_VERIFIED:{bad}", "")])
def test_stop_all_never_claims_success_without_verified_inventory(cloud, monkeypatch, response):
    calls = []
    def fake(command, **kwargs):
        compile(shlex.split(command)[-1], "cleanup", "exec")
        calls.append(command)
        return response
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_bash", fake)
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
    def fake(command, **kwargs):
        code = shlex.split(command)[-1]
        original_path = list(sys.path)
        lines = []
        try:
            exec(compile(code, "cleanup", "exec"), {"print": lines.append})
        except Exception as exc:
            return 1, "", str(exc)
        finally:
            sys.path[:] = original_path
        return 0, "\n".join(lines), ""
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_bash", fake)
    assert cloud.colab_bridge.stop_all_sessions()[0] is True
    assert removed == ["session"]
    assignments.append(types.SimpleNamespace(endpoint="still-running"))
    state.client.unassign = lambda endpoint: None
    removed.clear()
    assert cloud.colab_bridge.stop_all_sessions()[0] is False
    assert removed == []


def test_stopping_one_session_does_not_stop_other_assignments(cloud, monkeypatch):
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_colab", Mock(return_value=(1, "not terminated", "denied")))
    unrelated = Mock(side_effect=AssertionError("global cleanup must not run"))
    monkeypatch.setattr(cloud.colab_bridge, "_run_wsl_bash", unrelated)
    assert cloud.colab_bridge.stop_session("one")[0] is False
    unrelated.assert_not_called()


@pytest.mark.parametrize("terminal", ["stopped", "error", "completed"])
def test_terminal_state_rejects_late_progress_and_publication(cloud, terminal):
    job = cloud.store.enqueue("video.mp4")
    assert cloud.store.update_progress(job.job_id, state=terminal)
    assert not cloud.store.update_progress(job.job_id, state="downloading", percent=85)
    publish = Mock()
    assert not cloud.store.publish_if_active(job.job_id, "final.mp4", publish)
    publish.assert_not_called()
    assert cloud.store.get_job(job.job_id).state == terminal


@pytest.mark.parametrize("terminal", ["error", "stopped", "completed"])
def test_billing_survives_failure_cancel_history_delete_and_restart(cloud, terminal):
    job = cloud.store.enqueue("video.mp4")
    cloud.store.start_billing(job.job_id, 2.0)
    cloud.now[0] += 1800
    cloud.store.update_progress(job.job_id, state=terminal)
    assert cloud.store.finish_billing(job.job_id) == pytest.approx(1.0)
    assert cloud.store.delete_job(job.job_id)
    restarted = cloud.cloud_job_store.CloudJobStore(cloud.store.db_path)
    assert restarted.get_cu_spent_today() == 1.0


def test_billing_splits_cross_midnight_and_blocks_unknown_cleanup(cloud):
    cloud.now[0] = datetime(2026, 11, 15, 23, 30).timestamp()
    job = cloud.store.enqueue("old.mp4")
    cloud.store.start_billing(job.job_id, 2.0)
    cloud.store.cancel_job(job.job_id)
    cloud.now[0] += 3600
    assert cloud.store.get_cu_spent_today() == 1.0
    assert not cloud.store.delete_job(job.job_id)
    next_job = cloud.store.enqueue("next.mp4")
    with pytest.raises(RuntimeError, match="chưa xác nhận"):
        cloud.store.start_billing(next_job.job_id, 2.0)
    cloud.store.acknowledge_all_sessions_stopped()
    # A concurrent allocation may not exist remotely yet: stop-all must not release it.
    assert not cloud.store.delete_job(job.job_id)
    cloud.store.note_cleanup_failure(job.job_id, "timeout")
    cloud.store.acknowledge_all_sessions_stopped()
    assert cloud.store.get_cu_spent_today() == 1.0
    assert cloud.store.delete_job(job.job_id)


def test_old_failed_costs_are_migrated_once(cloud):
    job = cloud.store.enqueue("legacy.mp4")
    with cloud.store._connect() as db:
        db.execute("UPDATE cloud_jobs SET state='error', cu_actual=3, completed_at=? WHERE job_id=?",
                   (cloud.now[0], job.job_id))
    migrated = cloud.cloud_job_store.CloudJobStore(cloud.store.db_path)
    assert migrated.get_cu_spent_today() == 3.0
    assert migrated.delete_job(job.job_id)
    assert cloud.cloud_job_store.CloudJobStore(cloud.store.db_path).get_cu_spent_today() == 3.0


@pytest.mark.parametrize("state", ["stopped", "error", "processing"])
def test_recover_orphan_billing_requires_dead_owner_before_settlement(cloud, monkeypatch, state):
    job = cloud.store.enqueue("video.mp4")
    cloud.store.start_billing(job.job_id, 1.0)
    cloud.store.update_progress(job.job_id, state=state)
    cloud.now[0] += 3600
    # Current process can still be returning from create_session after a cancel.
    cloud.store.acknowledge_all_sessions_stopped()
    with cloud.store._connect() as db:
        assert db.execute("SELECT ended_at FROM cloud_cu_ledger WHERE job_id=?", (job.job_id,)).fetchone()[0] is None
    monkeypatch.setattr(cloud.store, "_billing_owner_alive", lambda row: False)
    cloud.store.recover()
    assert cloud.store.get_job(job.job_id).state == ("stopped" if state == "stopped" else "error")
    cloud.store.acknowledge_all_sessions_stopped()
    assert cloud.store.get_cu_spent_today() == 1.0
    assert cloud.store.delete_job(job.job_id)


def fake_provider(cloud, tmp_path, monkeypatch):
    module = cloud.cloud_orchestrator
    for name, result in (("get_usage", {"ok": True, "balance": 100}),
                         ("create_session", (True, "created")), ("upload_file", (True, "ok")),
                         ("exec_cmd", (True, "ok")), ("stop_session", (True, "stopped"))):
        monkeypatch.setattr(module, name, Mock(return_value=result))
    def download(session, name, directory):
        path = Path(directory) / name
        path.write_bytes(b"fake media" if name.endswith("mp4") else b"{}")
        return True, str(path)
    monkeypatch.setattr(module, "download_file", Mock(side_effect=download))
    orchestrator = module.CloudOrchestrator(cloud.store)
    orchestrator.gpu_scripts_dir = tmp_path / "scripts"
    orchestrator.gpu_scripts_dir.mkdir()
    return orchestrator


def test_cancel_during_allocation_stops_vm_and_never_uploads(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video")
    def allocate(*args, **kwargs):
        cloud.store.cancel_job(job.job_id)
        cloud.now[0] += 1800
        return True, "created after cancel"
    cloud.cloud_orchestrator.create_session.side_effect = allocate
    assert runner.process_job(job) is False
    cloud.cloud_orchestrator.upload_file.assert_not_called()
    cloud.cloud_orchestrator.stop_session.assert_called_once()
    assert cloud.store.get_job(job.job_id).state == "stopped"
    assert cloud.store.get_cu_spent_today() == 0.575


def test_upload_failure_is_billed_and_cleanup_failure_blocks_admission(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video")
    def upload(*args, **kwargs):
        cloud.now[0] += 1800
        return False, "upload failure"
    cloud.cloud_orchestrator.upload_file.side_effect = upload
    cloud.cloud_orchestrator.stop_session.return_value = (False, "provider timeout")
    assert runner.process_job(job) is False
    assert cloud.store.get_job(job.job_id).state == "error"
    assert "provider timeout" in cloud.store.get_job(job.job_id).error
    assert cloud.store.get_cu_spent_today() == 0.575
    with pytest.raises(RuntimeError, match="chưa xác nhận"):
        cloud.store.start_billing(cloud.store.enqueue("next").job_id, 1.15)


def test_cancel_during_download_never_publishes(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/video", video_name="video.mp4")
    original = cloud.cloud_orchestrator.download_file.side_effect
    def cancel(*args):
        result = original(*args)
        cloud.store.cancel_job(job.job_id)
        return result
    cloud.cloud_orchestrator.download_file.side_effect = cancel
    assert runner.process_job(job) is False
    assert list(cloud.output.iterdir()) == []
    assert cloud.store.get_job(job.job_id).state == "stopped"


def test_success_cleans_up_before_publish_and_quotes_apostrophes(cloud, tmp_path, monkeypatch):
    runner = fake_provider(cloud, tmp_path, monkeypatch)
    job = cloud.store.enqueue("https://example.invalid/a'b", video_name="video.mp4", voice="test'voice")
    def stop(*args):
        assert list(cloud.output.iterdir()) == []
        cloud.now[0] += 60
        return True, "stopped"
    cloud.cloud_orchestrator.stop_session.side_effect = stop
    assert runner.process_job(job) is True
    assert cloud.store.get_job(job.job_id).state == "completed"
    assert cloud.store.get_job(job.job_id).cu_actual > 0
    command = cloud.cloud_orchestrator.exec_cmd.call_args.args[1]
    tokens = shlex.split(command)
    assert tokens[tokens.index("--input") + 1] == job.video_path
    assert tokens[tokens.index("--voice") + 1] == job.voice
    assert (cloud.output / "cloud_video.mp4").is_file()
