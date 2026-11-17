"""Cloud billing and lifecycle regressions adapted to fenced attempt contracts."""
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.cloud import cloud_job_store as module, colab_bridge as bridge
from backend.cloud.cloud_contracts import CloudCapabilityError, CloudOwnershipError
from backend.cloud.cloud_job_store import CloudJobStore
from tests.test_cloud_remediation import FakeProvider, orchestrator, ready_to_publish


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    now = [datetime(2026, 11, 17, 12).timestamp()]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: now[0]))
    source = tmp_path / "nguồn ' có dấu.mp4"
    source.write_bytes(b'fixture-input')
    store = CloudJobStore(tmp_path / 'billing.sqlite')
    return SimpleNamespace(now=now, store=store, source=source, root=tmp_path)


def claimed(cloud):
    return cloud.store.enqueue(str(cloud.source), claim_owner='fixture-owner')


def token(job):
    return dict(attempt_id=job.attempt_id, owner_id=job.owner_id)


def bill(cloud, job, rate=2.):
    cloud.store.start_billing(job.job_id, rate, **token(job))


def settle(cloud, job):
    return cloud.store.finish_billing(job.job_id, **token(job))


@pytest.mark.parametrize('action', ['create_session', 'exec_argv', 'stop_session'])
def test_live_calls_are_gated_before_provider_or_secret_operations(monkeypatch, action):
    cli = Mock(side_effect=AssertionError('live CLI must not execute'))
    monkeypatch.setattr(bridge, '_run_wsl_colab', cli)
    args = ('session', ['python3', 'job.py']) if action == 'exec_argv' else ('session',)
    with pytest.raises(CloudCapabilityError, match='cloud_cli_unverified'):
        getattr(bridge, action)(*args)
    cli.assert_not_called()


def test_global_stop_cannot_touch_unowned_assignments(monkeypatch):
    cli = Mock(side_effect=AssertionError('global cleanup must not execute'))
    monkeypatch.setattr(bridge, '_run_wsl_colab', cli)
    assert bridge.stop_all_sessions()[0] is False
    cli.assert_not_called()


def test_stopping_one_session_never_falls_back_to_global_cleanup(monkeypatch):
    monkeypatch.setattr(bridge, 'ensure_live_ready', lambda: None)
    cli = Mock(return_value=(1, 'not terminated', 'denied'))
    monkeypatch.setattr(bridge, '_run_wsl_colab', cli)
    assert bridge.stop_session('owned-session')[0] is False
    assert cli.call_args.args[0] == ['stop', '-s', 'owned-session']
    assert cli.call_count == 1


@pytest.mark.parametrize('terminal', ['stopped', 'error', 'completed'])
def test_terminal_state_rejects_late_unowned_or_fenced_progress(cloud, terminal):
    job = claimed(cloud)
    if terminal == 'completed':
        stage, output, info = ready_to_publish(cloud.store, job, cloud.root)
        assert cloud.store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info)
    else:
        if terminal == 'stopped':
            cloud.store.request_cancel(job.job_id)
        cloud.store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
    assert cloud.store.get_job(job.job_id).state == terminal
    assert not cloud.store.update_progress(job.job_id, state='downloading', **token(job))
    assert not cloud.store.update_progress(job.job_id, state='completed')
    assert cloud.store.get_job(job.job_id).state == terminal


@pytest.mark.parametrize('terminal', ['error', 'stopped', 'completed'])
def test_billing_survives_failure_cancel_history_delete_and_restart(cloud, terminal):
    job = claimed(cloud)
    bill(cloud, job)
    cloud.now[0] += 1800
    if terminal == 'completed':
        stage, output, info = ready_to_publish(cloud.store, job, cloud.root)
        assert cloud.store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info)
    elif terminal == 'stopped':
        cloud.store.request_cancel(job.job_id)
    assert settle(cloud, job) == pytest.approx(1.)
    cloud.store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
    assert cloud.store.get_job(job.job_id).state == terminal
    assert cloud.store.delete_job(job.job_id)
    assert CloudJobStore(cloud.store.db_path).get_cu_spent_today() == 1.


def test_billing_splits_midnight_and_unknown_cleanup_blocks_admission(cloud):
    cloud.now[0] = datetime(2026, 11, 15, 23, 30).timestamp()
    job = claimed(cloud)
    bill(cloud, job)
    cloud.store.request_cancel(job.job_id)
    cloud.store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=False)
    cloud.now[0] += 3600
    assert cloud.store.get_cu_spent_today() == 1.
    assert not cloud.store.delete_job(job.job_id)
    next_job = cloud.store.enqueue(str(cloud.source))
    assert cloud.store.claim_job(next_job.job_id, 'other') is None
    with pytest.raises(CloudOwnershipError, match='global_cleanup'):
        cloud.store.acknowledge_all_sessions_stopped()
    cloud.store.note_cleanup_failure(job.job_id, 'timeout', **token(job))
    settle(cloud, job)
    cloud.store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
    assert cloud.store.get_cu_spent_today() == 1.
    assert cloud.store.delete_job(job.job_id)


def test_old_failed_costs_are_migrated_once(cloud):
    job = cloud.store.enqueue(str(cloud.source))
    with cloud.store._connect() as db:
        db.execute("UPDATE cloud_jobs SET state='error',cu_actual=3,completed_at=? WHERE job_id=?",
                   (cloud.now[0], job.job_id))
    migrated = CloudJobStore(cloud.store.db_path)
    assert migrated.get_cu_spent_today() == 3.
    assert migrated.delete_job(job.job_id)
    assert CloudJobStore(cloud.store.db_path).get_cu_spent_today() == 3.


def test_orphan_billing_requires_dead_owner_and_keeps_cleanup_held(cloud, monkeypatch):
    job = claimed(cloud)
    bill(cloud, job, 1.)
    cloud.now[0] += 3600
    cloud.store.recover()
    assert cloud.store.get_job(job.job_id).state == 'creating_session'
    monkeypatch.setattr(cloud.store, '_billing_owner_alive', lambda row: False)
    cloud.store.recover()
    assert cloud.store.get_job(job.job_id).state == 'stop_incomplete'
    assert cloud.store.get_cu_spent_today() == 1.
    assert not cloud.store.delete_job(job.job_id)
    with cloud.store._connect() as db:
        row = db.execute('SELECT ended_at,cleanup_pending FROM cloud_cu_ledger WHERE job_id=?', (job.job_id,)).fetchone()
        assert row['ended_at'] is None and row['cleanup_pending'] == 1


def test_stale_or_unowned_billing_cannot_release_admission(cloud):
    job = claimed(cloud)
    with pytest.raises(CloudOwnershipError):
        cloud.store.start_billing(job.job_id, 2.)
    bill(cloud, job)
    with pytest.raises(CloudOwnershipError):
        cloud.store.finish_billing(job.job_id, attempt_id='stale', owner_id=job.owner_id)
    with pytest.raises(CloudOwnershipError):
        cloud.store.note_cleanup_failure(job.job_id, 'stale', attempt_id='stale', owner_id=job.owner_id)


def test_cancel_during_allocation_is_billed_and_stops_before_upload(cloud):
    job = claimed(cloud)
    def hook(name):
        if name == 'create':
            cloud.now[0] += 1800
            cloud.store.request_cancel(job.job_id)
    provider = FakeProvider(cloud.store, job, hook=hook)
    result = orchestrator(cloud.store, provider, cloud.root).process_job(job)
    assert result.status == 'stopped'
    assert 'upload' not in provider.calls and provider.calls.count('stop') == 1
    assert cloud.store.get_cu_spent_today() == .575


def test_failed_cleanup_stays_billed_and_blocks_next_claim(cloud):
    job = claimed(cloud)
    provider = FakeProvider(cloud.store, job, stop_ok=False)
    def upload(*args):
        cloud.now[0] += 1800
        return False, 'upload failed'
    provider.upload_file = upload
    assert orchestrator(cloud.store, provider, cloud.root).process_job(job).status == 'stop_incomplete'
    assert cloud.store.get_cu_spent_today() == .575
    next_job = cloud.store.enqueue(str(cloud.source))
    assert cloud.store.claim_job(next_job.job_id, 'other') is None


def test_cancel_during_download_never_publishes(cloud):
    job = claimed(cloud)
    provider = FakeProvider(cloud.store, job, hook=lambda name:
        cloud.store.request_cancel(job.job_id) if name.startswith('download:') else None)
    assert orchestrator(cloud.store, provider, cloud.root).process_job(job).status == 'stopped'
    assert not (cloud.root / 'outputs').exists()


def test_verified_success_settles_only_after_scoped_cleanup(cloud):
    job = claimed(cloud)
    provider = FakeProvider(cloud.store, job, hook=lambda name:
        cloud.now.__setitem__(0, cloud.now[0] + 60) if name == 'stop' else None)
    result = orchestrator(cloud.store, provider, cloud.root).process_job(job)
    assert result.status == 'completed' and result.receipt
    assert provider.calls.index('cleanup_secret') < provider.calls.index('stop')
    assert cloud.store.get_job(job.job_id).cu_actual > 0
    assert cloud.store.get_cu_spent_today() == pytest.approx(.019, abs=.001)


@pytest.mark.parametrize('budget', [float('nan'), float('inf'), 0., -.1])
def test_invalid_budget_cannot_disable_daily_guard(cloud, budget):
    with pytest.raises(ValueError):
        cloud.store.set_cu_budget_daily(budget)
