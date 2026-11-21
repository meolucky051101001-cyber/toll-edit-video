"""Retain the local safety regressions against the phase-8 Cloud lifecycle."""
import sys
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.cloud import cloud_job_store, cloud_orchestrator, colab_bridge
from backend.cloud.cloud_config import DEFAULT_CONFIG
from tests.test_cloud_phase5_quality_export import _Phase5Adapter, _make_sample_video


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    # Fixed midday ICT so +/-1800s billing tests never straddle midnight.
    now = [datetime(2026, 11, 15, 12, 0, tzinfo=timezone(timedelta(hours=7))).timestamp()]
    clock = SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic, strftime=time.strftime)
    monkeypatch.setattr(cloud_job_store, 'time', clock)
    monkeypatch.setattr(cloud_orchestrator, 'time', clock)
    output = tmp_path / 'output'
    output.mkdir()
    monkeypatch.setenv('AUTODUB_OUTPUT_DIR', str(output))
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-fixture')
    monkeypatch.setitem(DEFAULT_CONFIG, 'results_dir', str(tmp_path / 'results'))
    store = cloud_job_store.CloudJobStore(tmp_path / 'jobs.db', rollout_controlled=False)
    source = _make_sample_video(tmp_path)
    return SimpleNamespace(store=store, now=now, output=output, source=source, root=tmp_path)


def claim(cloud, **kwargs):
    job = cloud.store.enqueue(str(cloud.source), **kwargs)
    claimed = cloud.store.claim_job(job.job_id, owner_id='review', lease_seconds=7200)
    assert claimed is not None
    return claimed


def terminal(cloud, job, state, amount=1.):
    for current in ('uploading', 'preparing', 'processing', 'downloading', 'validating'):
        assert cloud.store.update_progress(job.job_id, state=current, owner_id='review', attempt=job.attempt)
    if state == 'cancelled':
        assert cloud.store.request_cancel(job.job_id)['cancelled']
    assert cloud.store.update_progress(job.job_id, state=state, cu_actual=amount,
                                      owner_id='review', attempt=job.attempt, allow_from_cancel=state == 'cancelled')


@pytest.mark.parametrize('response', [(1, '', 'denied'), (-1, '', 'timeout'),
                                    (0, 'not verified', ''), (0, 'AUTODUB_STOP_VERIFIED:{bad}', '')])
def test_stop_all_never_claims_success_without_verified_inventory(monkeypatch, response):
    calls = []
    def fake(command, argv, **kwargs):
        compile(kwargs['stdin_text'], 'cleanup', 'exec')
        calls.append(command)
        return response
    monkeypatch.setattr(colab_bridge, '_run_wsl_argv', fake)
    assert colab_bridge.stop_all_sessions()[0] is False
    assert len(calls) == 1


def test_stop_all_checks_remaining_before_deleting_local_inventory(monkeypatch):
    assignments = [SimpleNamespace(endpoint='vm')]
    removed = []
    state = SimpleNamespace(client=SimpleNamespace(list_assignments=lambda: assignments,
        unassign=lambda _: assignments.clear()), store=SimpleNamespace(list=lambda: {'session': {}}, remove=removed.append))
    monkeypatch.setitem(sys.modules, 'colab_cli.common', SimpleNamespace(state=state))
    def fake(command, argv, **kwargs):
        original_path, lines = list(sys.path), []
        try:
            exec(compile(kwargs['stdin_text'], 'cleanup', 'exec'), {'print': lines.append})
        except Exception as exc:
            return 1, '', str(exc)
        finally:
            sys.path[:] = original_path
        return 0, '\n'.join(lines), ''
    monkeypatch.setattr(colab_bridge, '_run_wsl_argv', fake)
    assert colab_bridge.stop_all_sessions()[0] is True
    assert removed == ['session']
    assignments.append(SimpleNamespace(endpoint='still-running'))
    state.client.unassign = lambda _: None
    removed.clear()
    assert colab_bridge.stop_all_sessions()[0] is False
    assert removed == []


def test_stopping_one_session_does_not_stop_other_assignments(monkeypatch):
    monkeypatch.setattr(colab_bridge, '_run_wsl_colab_argv', Mock(return_value=(1, 'not terminated', 'denied')))
    unrelated = Mock(side_effect=AssertionError('global cleanup must not run'))
    monkeypatch.setattr(colab_bridge, '_run_wsl_argv', unrelated)
    assert colab_bridge.stop_session('one')[0] is False
    unrelated.assert_not_called()


@pytest.mark.parametrize("corrupt_part", [False, True])
def test_colab_rejected_mp4_recovers_by_verified_parts(tmp_path, monkeypatch, corrupt_part):
    source = tmp_path / "source.mp4"
    original = bytes(range(37))
    source.write_bytes(original)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    destination = runtime / "clip.mp4"
    uploads = []

    def upload(argv, **kwargs):
        name = argv[-1].removeprefix("content/")
        data = Path(argv[-2]).read_bytes()
        uploads.append((name, len(data)))
        if name == destination.name:
            return 1, "", "400 Client Error: Bad Request"
        if corrupt_part and len(uploads) == 3:
            data = bytes([data[0] ^ 1]) + data[1:]
        (runtime / name).write_bytes(data)
        return 0, "", ""

    def assemble(session_name, argv, **kwargs):
        checked = colab_bridge._validate_argv(argv)
        result = subprocess.run([sys.executable, *checked[1:]], cwd=runtime,
                                capture_output=True, text=True, timeout=10)
        return result.returncode == 0, result.stderr

    monkeypatch.setattr(colab_bridge, "_UPLOAD_CHUNK_BYTES", 8)
    monkeypatch.setattr(colab_bridge, "to_wsl_path", str)
    monkeypatch.setattr(colab_bridge, "_run_wsl_colab_argv", upload)
    monkeypatch.setattr(colab_bridge, "exec_argv", assemble)
    ok, detail = colab_bridge.upload_file("cjob_upload", source, destination.name)

    assert uploads[0] == (destination.name, len(original))
    assert [size for _, size in uploads[1:]] == [8, 8, 8, 8, 5]
    assert not list(runtime.glob(".autodub_upload_*"))
    assert ok is not corrupt_part
    if corrupt_part:
        assert "SHA256 does not match" in detail
        assert not destination.exists()
    else:
        assert destination.read_bytes() == original


@pytest.mark.parametrize('state', ['cancelled', 'failed', 'completed'])
def test_terminal_state_rejects_late_progress_and_publication(cloud, state):
    job = claim(cloud)
    terminal(cloud, job, state)
    assert not cloud.store.update_progress(job.job_id, state='downloading', percent=85)
    assert not cloud.store.update_progress(job.job_id, state='completed', result_path='late.mp4')
    assert cloud.store.get_job(job.job_id).state == state
    assert cloud.store.get_job(job.job_id).result_path is None


@pytest.mark.parametrize('state', ['failed', 'cancelled', 'completed'])
def test_billing_survives_failure_cancel_history_delete_and_restart(cloud, state):
    job = claim(cloud)
    cloud.now[0] += 1800
    terminal(cloud, job, state)
    assert cloud.store.delete_job(job.job_id)
    restarted = cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
    assert restarted.get_cu_spent_today() == pytest.approx(1.)
    assert len(restarted.list_cost_ledger(job_id=job.job_id)) == 1


def seed_legacy_ledger(store, entries):
    with store._connect() as db:
        db.execute('''CREATE TABLE cloud_cu_ledger (job_id TEXT PRIMARY KEY, started_at REAL NOT NULL,
            ended_at REAL, hourly_rate REAL NOT NULL DEFAULT 0, cu_actual REAL NOT NULL DEFAULT 0,
            cleanup_pending INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER, owner_created_at REAL)''')
        db.executemany('INSERT INTO cloud_cu_ledger(job_id,started_at,ended_at,hourly_rate,cu_actual) VALUES (?,?,?,?,?)', entries)


def test_deleted_legacy_billing_is_migrated_once_and_split_at_midnight(cloud):
    start = datetime(2026, 11, 15, 23, 30, tzinfo=timezone(timedelta(hours=7))).timestamp()
    cloud.now[0] = start + 3600
    seed_legacy_ledger(cloud.store, [('deleted-history', start, start + 3600, 2., 2.)])
    for _ in range(2):
        migrated = cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
        assert migrated.get_cu_spent_today(now=start + 1800) == pytest.approx(1.)
        assert migrated.get_cu_spent_today(now=start + 3600) == pytest.approx(1.)
        assert len(migrated.list_cost_ledger()) == 1


def test_old_failed_costs_are_migrated_once_without_changing_job_state(cloud):
    job = cloud.store.enqueue(str(cloud.source))
    with cloud.store._connect() as db:
        db.execute("UPDATE cloud_jobs SET state='error', cu_actual=3, completed_at=? WHERE job_id=?", (cloud.now[0], job.job_id))
    migrated = cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
    assert migrated.get_cu_spent_today() == pytest.approx(3.)
    assert migrated.get_job(job.job_id).state == 'error'
    assert migrated.delete_job(job.job_id)
    restarted = cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
    assert restarted.get_cu_spent_today() == pytest.approx(3.)
    assert len(restarted.list_cost_ledger()) == 1


def test_original_legacy_schema_migrates_without_resetting_status(tmp_path):
    path = tmp_path / 'old.db'
    now = datetime(2026, 11, 15, 12, 0, tzinfo=timezone(timedelta(hours=7))).timestamp()
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE cloud_jobs (
            id INTEGER PRIMARY KEY, job_id TEXT UNIQUE, video_path TEXT, video_name TEXT,
            gpu_type TEXT DEFAULT 'T4', task_type TEXT DEFAULT 'full_pipeline', state TEXT,
            percent INTEGER DEFAULT 0, step_name TEXT, session_name TEXT,
            cu_estimated REAL DEFAULT 0, cu_actual REAL DEFAULT 0, created_at REAL,
            started_at REAL, completed_at REAL, result_path TEXT, error TEXT)''')
        db.execute("INSERT INTO cloud_jobs(job_id,video_path,video_name,state,cu_actual,created_at,completed_at) VALUES ('legacy','input.mp4','input.mp4','stopped',2,?,?)", (now, now))
    for _ in range(2):
        migrated = cloud_job_store.CloudJobStore(path, rollout_controlled=False)
        assert migrated.get_job('legacy').state == 'stopped'
        assert migrated.get_job('legacy').attempt == 1
        assert migrated.get_cu_spent_today(now=now) == pytest.approx(2.)
        assert len(migrated.list_cost_ledger()) == 1


@pytest.mark.parametrize('has_session', [True, False])
def test_pending_legacy_billing_blocks_admission_and_never_claims_cleanup(cloud, has_session):
    job = cloud.store.enqueue(str(cloud.source))
    with cloud.store._connect() as db:
        db.execute("UPDATE cloud_jobs SET state='processing', session_name=? WHERE job_id=?", ('old-session' if has_session else None, job.job_id))
    seed_legacy_ledger(cloud.store, [(job.job_id, cloud.now[0] - 1800, None, 2., 0.)])
    migrated = cloud_job_store.CloudJobStore(cloud.store.db_path, rollout_controlled=False)
    assert migrated.get_job(job.job_id).state == 'processing'
    assert migrated.get_cu_spent_today() == pytest.approx(1.)
    cloud.now[0] += 900
    assert migrated.get_cu_spent_today() == pytest.approx(1.5)
    assert migrated.get_cu_budget_info()['unreconciled_count'] == 1
    assert migrated.claim_job(migrated.enqueue(str(cloud.source)).job_id, owner_id='new') is None
    assert not migrated.delete_job(job.job_id)
    unavailable = SimpleNamespace(get_sessions_status=lambda: {'ok': False, 'sessions': [], 'error': 'offline'})
    migrated.recover(adapter=unavailable)
    assert migrated.list_cost_ledger(job_id=job.job_id)[0]['cleanup_verified'] == 0
    if not has_session:
        # Legacy cancellation used a terminal state before provider cleanup.
        with migrated._connect() as db:
            db.execute("UPDATE cloud_jobs SET state='stopped' WHERE job_id=?", (job.job_id,))
        assert not migrated.delete_job(job.job_id)
    if has_session:
        migrated.record_cleanup_result(job.job_id, 1, 'old-session', verified_stopped=True,
                                       cleanup_status='verified', attempts=1)
        migrated.update_progress(job.job_id, state='failed', cleanup_status='verified')
        assert migrated.get_cu_spent_today() == pytest.approx(1.5)
        cloud.now[0] += 900
        assert migrated.get_cu_spent_today() == pytest.approx(1.5)
        assert migrated.get_cu_budget_info()['unreconciled_count'] == 0


def fake_provider(cloud):
    adapter = _Phase5Adapter()
    for name in ('create_session', 'upload_file', 'download_file', 'exec_argv', 'stop_session'):
        def invoke(*args, _method=getattr(adapter, name), **kwargs):
            kwargs.pop('cancel_check', None)
            return _method(*args, **kwargs)
        setattr(adapter, name, Mock(wraps=invoke))
    runner = cloud_orchestrator.CloudOrchestrator(cloud.store, adapter=adapter, allowed_roots=[cloud.root])
    return runner, adapter


def test_cancel_during_allocation_stops_vm_and_never_uploads(cloud):
    runner, provider = fake_provider(cloud)
    job = claim(cloud)
    def allocate(session, **kwargs):
        provider.created_sessions.append(session)
        cloud.store.request_cancel(job.job_id)
        cloud.now[0] += 1800
        return True, 'created after cancel'
    provider.create_session.side_effect = allocate
    assert runner.process_job(job, owner_id='review') is False
    provider.upload_file.assert_not_called()
    provider.stop_session.assert_called_once()
    assert cloud.store.get_job(job.job_id).state == 'cancelled'
    assert cloud.store.get_cu_spent_today() == pytest.approx(.575)


def test_upload_failure_is_billed_and_cleanup_failure_blocks_admission(cloud, monkeypatch):
    runner, provider = fake_provider(cloud)
    monkeypatch.setitem(DEFAULT_CONFIG, 'cloud_upload_retry_delays_seconds', [0, 0])
    job = claim(cloud)
    def upload(*args, **kwargs):
        cloud.now[0] += 1800
        return False, 'upload failure'
    provider.upload_file.side_effect = upload
    provider.stop_session.side_effect = lambda *_: (False, 'provider timeout')
    assert runner.process_job(job, owner_id='review') is False
    saved = cloud.store.get_job(job.job_id)
    assert saved.state == 'failed'
    assert saved.cleanup_status != 'verified'
    assert provider.upload_file.call_count == 3
    assert cloud.store.get_cu_spent_today() == pytest.approx(1.725)
    assert cloud.store.get_cu_budget_info()['unreconciled_count'] == 1
    assert cloud.store.claim_job(cloud.store.enqueue(str(cloud.source)).job_id, owner_id='new') is None


def test_cancel_during_download_never_publishes(cloud):
    runner, provider = fake_provider(cloud)
    job = claim(cloud)
    original = provider.download_file._mock_wraps
    def cancel(*args, **kwargs):
        result = original(*args, **kwargs)
        cloud.store.request_cancel(job.job_id)
        return result
    provider.download_file.side_effect = cancel
    assert runner.process_job(job, owner_id='review') is False
    assert list(cloud.output.iterdir()) == []
    assert cloud.store.get_job(job.job_id).state == 'cancelled'


def test_success_cleans_up_before_publish_and_preserves_filename_apostrophe(cloud):
    source = cloud.source.with_name("it's a video.mp4")
    cloud.source.rename(source)
    cloud.source = source
    runner, provider = fake_provider(cloud)
    job = claim(cloud)
    original = provider.stop_session._mock_wraps
    def stop(*args):
        assert list(cloud.output.iterdir()) == []
        cloud.now[0] += 60
        return original(*args)
    provider.stop_session.side_effect = stop
    assert runner.process_job(job, owner_id='review') is True
    saved = cloud.store.get_job(job.job_id)
    assert saved.state == 'completed'
    assert saved.cu_actual > 0
    argv = next(call.args[1] for call in provider.exec_argv.call_args_list if call.args[1][1] == 'full_pipeline.py')
    assert argv[argv.index('--input') + 1].startswith('/content/')
    assert Path(saved.result_path).is_file()


def test_successful_pipeline_with_failed_cleanup_never_publishes(cloud):
    runner, provider = fake_provider(cloud)
    job = claim(cloud)
    def fail_stop(*args):
        cloud.now[0] += 60
        return False, 'provider timeout'
    provider.stop_session.side_effect = fail_stop
    assert runner.process_job(job, owner_id='review') is False
    saved = cloud.store.get_job(job.job_id)
    assert saved.state != 'completed'
    assert saved.result_path is None
    assert list(cloud.output.iterdir()) == []
    assert saved.cu_actual > 0
    assert saved.cleanup_status != 'verified'
    assert cloud.store.get_cu_budget_info()['unreconciled_count'] == 1


def test_wsl_argv_reaches_the_program_without_shell_quoting(monkeypatch):
    seen = []

    def fake_run(cmd_args, **kwargs):
        seen.append(list(cmd_args))
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    monkeypatch.setattr(colab_bridge.subprocess, 'run', fake_run)
    dest = "/mnt/c/tool v1/backend/cloud/results/job/attempt_1/output_video.mp4"
    args = ['download', '-s', 'cjob1234', 'content/output_video.mp4', dest, "a'b; echo $HOME"]

    assert colab_bridge._run_wsl_argv('env', args, timeout=5)[0] == 0
    [cmd] = seen
    assert cmd[cmd.index('--exec'):] == ['--exec', 'env', *args]
    assert '--' not in cmd
