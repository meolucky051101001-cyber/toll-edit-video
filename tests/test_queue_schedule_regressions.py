import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from starlette.responses import JSONResponse
from night_schedule_service import ScheduleStore, dispatch_due
from queue_control_service import apply_deletions, filter_restored, read_operation, submit_command, submit_delete
import telegram_queue_monitor as monitor


def stamp(day, hour, minute=0, second=0):
    return datetime(2026, 11, day, hour, minute, second, tzinfo=timezone(timedelta(hours=7))).timestamp()


@pytest.mark.parametrize('now,expected', [
    (stamp(2, 20), stamp(3, 2)), (stamp(2, 1), stamp(2, 2)),
    (stamp(2, 2), stamp(3, 2)), (stamp(2, 2, 0, 30), stamp(3, 2)),
])
def test_next_occurrence(now, expected):
    assert ScheduleStore.next_run('02:00', now) == expected


def test_evening_schedule_survives_poll_and_restart(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    data = store.create('02:00', 'AUTO', stamp(2, 20), target_kind='local', batch_id='batch')
    assert store.claim(stamp(2, 20, 1)) is None
    reopened = ScheduleStore(store.path)
    assert reopened.get()['run_at'] == stamp(3, 2)
    assert reopened.claim(stamp(3, 2))['schedule_id'] == data['schedule_id']


def test_one_shot_never_repeats_and_records_batch(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.create('02:00', 'SHORT', stamp(2, 20), target_kind='local', batch_id='batch')
    start = AsyncMock(return_value=JSONResponse({'job_id': 'batch1'}, status_code=202))
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2))) == 'accepted'
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2, 1))) is None
    assert asyncio.run(dispatch_due(ScheduleStore(store.path), start, stamp(4, 2))) is None
    assert start.call_count == 1
    assert store.get()['batch_job_id'] == 'batch1'
    assert store.get()['active'] is False


def test_busy_retry_then_expire(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.create('02:00', 'AUTO', stamp(2, 20), target_kind='local', batch_id='batch')
    start = AsyncMock(return_value=JSONResponse({}, status_code=409))
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2))) == 'busy'
    assert store.get()['state'] == 'pending'
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2, 31))) == 'expired'
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2, 32))) is None
    assert start.call_count == 1
    assert store.get()['state'] == 'expired'


def test_uncertain_handoff_does_not_repeat(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.create('02:00', 'AUTO', stamp(2, 20), target_kind='local', batch_id='batch')
    start = AsyncMock(side_effect=RuntimeError('lost response'))
    assert asyncio.run(dispatch_due(store, start, stamp(3, 2))) == 'unknown'
    assert asyncio.run(dispatch_due(store, start, stamp(4, 2))) is None
    assert store.get()['state'] == 'needs_review'


def test_claim_excludes_other_watchers_and_protects_cancel(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.create('02:00', 'AUTO', stamp(2, 20), target_kind='local', batch_id='batch')
    store.claim(stamp(3, 2))
    assert ScheduleStore(store.path).claim(stamp(3, 2, 1)) is None
    with pytest.raises(RuntimeError):
        store.cancel()
    with pytest.raises(RuntimeError):
        store.create('03:00', 'AUTO', stamp(3, 2), target_kind='local', batch_id='batch')
    store.claim(stamp(3, 2, 6))
    assert store.get()['state'] == 'needs_review'


@pytest.mark.parametrize('extra,state', [
    ({'created_at': stamp(2, 20)}, 'needs_review'),
    ({'created_at': stamp(2, 20), 'last_fired_date': '2026-11-03'}, 'fired'),
    ({}, 'needs_review'),
])
def test_legacy_migration(tmp_path, extra, state):
    path = tmp_path / 'schedule.json'
    path.write_text(json.dumps({'time': '02:00', **extra}), encoding='utf-8')
    store = ScheduleStore(path)
    assert store.get()['state'] == state
    assert store.claim(stamp(4, 2)) is None
    assert json.loads(next(tmp_path.glob('*.legacy-*.bak')).read_text()) == {'time': '02:00', **extra}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, 'SNAPSHOT', tmp_path / 'telegram_queue.json')
    return monitor.MonitoredQueue()


def test_telegram_schedule_keeps_held_job_across_restart_and_only_releases_target(queue, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import service_control
    monkeypatch.setattr(service_control, 'service_paused', lambda *_: False)
    clock = SimpleNamespace(time=lambda: stamp(2, 20))
    monkeypatch.setattr(monitor, 'time', clock)
    queue.put_nowait({'job_id': 'one'})
    store = ScheduleStore(tmp_path / 'schedule.json', queue.snapshot)
    schedule = store.create('02:00', now=stamp(2, 20), target_kind='telegram',
                            target_job_ids=['one'], expected_revision=queue.revision)
    queue.put_nowait({'job_id': 'two'})  # Applies the earlier hold first.
    assert store.get()['state'] == 'pending'
    restored = monitor.MonitoredQueue()
    restored.restore()
    assert restored.get_nowait()['job_id'] == 'two'
    restored.task_done('two')
    with pytest.raises(asyncio.QueueEmpty):
        restored.get_nowait()
    manual = submit_command(queue.snapshot, 'release_manual', ['one'], expected_revision=restored.revision)
    restored.apply_commands()
    assert read_operation(queue.snapshot, manual['operation_id'])['state'] == 'conflict'
    local = AsyncMock()
    assert asyncio.run(dispatch_due(store, local, stamp(3, 2))) == 'pending'
    clock.time = lambda: stamp(3, 2)
    assert restored.get_nowait()['job_id'] == 'one'
    assert store.get()['state'] == 'fired'
    assert store.get()['schedule_id'] == schedule['schedule_id']
    local.assert_not_called()


def test_telegram_cancel_and_late_release_preserve_hold(queue, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import service_control
    monkeypatch.setattr(service_control, 'service_paused', lambda *_: False)
    clock = SimpleNamespace(time=lambda: stamp(2, 20))
    monkeypatch.setattr(monitor, 'time', clock)
    queue.put_nowait({'job_id': 'one'})
    store = ScheduleStore(tmp_path / 'schedule.json', queue.snapshot)
    old = store.create('02:00', now=stamp(2, 20), target_kind='telegram',
                       target_job_ids=['one'], expected_revision=queue.revision)
    queue.apply_commands()
    assert store.cancel()['state'] == 'canceling'
    queue.apply_commands()
    assert store.get()['state'] == 'canceled'
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()

    store.create('02:00', now=stamp(2, 20), target_kind='telegram',
                 target_job_ids=['one'], expected_revision=queue.revision)
    queue.apply_commands()
    stale = submit_command(queue.snapshot, 'release_scheduled', ['one'],
                           payload={'schedule_id': old['schedule_id'], 'generation': old['generation'],
                                    'not_before': old['run_at'], 'expires_at': old['run_at'] + 1800})
    queue.apply_commands()
    assert read_operation(queue.snapshot, stale['operation_id'])['state'] == 'conflict'
    local = AsyncMock()
    assert asyncio.run(dispatch_due(store, local, stamp(3, 2))) == 'pending'
    clock.time = lambda: stamp(3, 2, 31)
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()
    assert store.get()['state'] == 'expired'
    assert queue.qsize() == 1
    local.assert_not_called()


def test_delete_only_pending_job_and_join(queue):
    job = {'job_id': 'one', 'url': 'u'}
    queue.put_nowait(job)
    op = submit_delete(monitor.SNAPSHOT, job)
    queue.publish()  # A normal publication cannot erase the delete request.
    apply_deletions(queue, monitor.SNAPSHOT)
    assert queue.empty()
    asyncio.run(asyncio.wait_for(queue.join(), 1))
    assert json.loads(monitor.SNAPSHOT.read_text())['items'] == []
    assert json.loads((monitor.SNAPSHOT.parent / 'queue_commands' / (op['operation_id'] + '.json')).read_text())['state'] == 'applied'
    apply_deletions(queue, monitor.SNAPSHOT)  # replay must not double-decrement
    assert queue._unfinished_tasks == 0


def test_delete_same_url_only_selected_identity(queue):
    queue.put_nowait({'job_id': 'one', 'url': 'u'})
    job = {'job_id': 'two', 'url': 'u'}
    queue.put_nowait(job)
    submit_delete(monitor.SNAPSHOT, job)
    assert queue.get_nowait()['job_id'] == 'one'
    queue.task_done()
    assert queue.empty()
    assert queue._unfinished_tasks == 0


def test_delete_cannot_stop_active_job(queue):
    job = {'job_id': 'one', 'url': 'u'}
    queue.put_nowait(job)
    queue.get_nowait()
    submit_delete(monitor.SNAPSHOT, job)
    apply_deletions(queue, monitor.SNAPSHOT)
    assert queue.active_job == job
    assert queue._unfinished_tasks == 1
    assert filter_restored(monitor.SNAPSHOT, [job]) == [job]
    queue.task_done()


def test_pending_delete_survives_snapshot_recovery(queue):
    job = {'job_id': 'one', 'url': 'u'}
    queue.put_nowait(job)
    submit_delete(monitor.SNAPSHOT, job)
    restored = monitor.MonitoredQueue()
    restored.restore()
    with pytest.raises(asyncio.QueueEmpty):
        restored.get_nowait()
    with pytest.raises(ValueError, match='đã bị xóa'):
        restored.put_nowait(job)


def test_async_consumer_waits_after_last_item_deleted(queue):
    job = {'job_id': 'one', 'url': 'u'}
    queue.put_nowait(job)
    submit_delete(monitor.SNAPSHOT, job)
    async def consume():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), .05)
        queue.put_nowait({'job_id': 'two'})
        assert (await queue.get())['job_id'] == 'two'
        queue.task_done()
        await asyncio.wait_for(queue.join(), .1)
    asyncio.run(consume())


def test_recovery_retains_job_already_active_when_delete_requested(queue):
    job = {'job_id': 'one', 'url': 'u'}
    submit_delete(monitor.SNAPSHOT, job)
    assert filter_restored(monitor.SNAPSHOT, [job], active=job) == [job]
    assert filter_restored(monitor.SNAPSHOT, [job]) == [job]


def test_api_delete_ack_and_stale_identity(tmp_path, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    snapshot = tmp_path / 'telegram_queue.json'
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    owner = monitor.MonitoredQueue()
    owner.put_nowait({'job_id': 'new', 'url': 'u'})
    monkeypatch.setattr(main, '_get_system_path', lambda name: tmp_path / name)
    client = TestClient(main.app, headers={'X-Local-Control-Token': main.LOCAL_TOKEN})
    assert client.post('/api/queue/delete', json={'job_id': 'old', 'expected_revision': owner.revision}).status_code == 409
    result = client.post('/api/queue/delete', json={'job_id': 'new', 'expected_revision': owner.revision}).json()
    assert result['status'] == 'pending'
    assert client.get('/api/queue/operations/' + result['operation_id']).json()['state'] == 'pending'
    # API acceptance does not rewrite the bot's state snapshot.
    assert json.loads(snapshot.read_text())['items'][0]['job_id'] == 'new'
    owner.apply_commands()
    assert client.get('/api/queue/operations/' + result['operation_id']).json()['state'] == 'applied'
