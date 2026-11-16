"""Independent review probes; only pytest tmp_path data is written."""
import json
import asyncio
import pytest
import telegram_queue_monitor as monitor
from queue_control_service import commands, submit_delete
from night_schedule_service import ScheduleStore
from queue_control_service import apply_deletions
from types import SimpleNamespace
from unittest.mock import AsyncMock


def test_startup_watcher_preserves_snapshot_before_restore(tmp_path, monkeypatch):
    import telegram_bot as bot
    directory = tmp_path / 'bot_system'
    directory.mkdir()
    snapshot = directory / 'telegram_queue.json'
    target = {'job_id': 'delete_me', 'url': 'https://example.invalid/a', 'type': 'url'}
    keep = {'job_id': 'keep_me', 'url': 'https://example.invalid/b', 'type': 'url'}
    active = {'job_id': 'resume_me', 'url': 'https://example.invalid/c', 'type': 'url'}
    snapshot.write_text(json.dumps({'items': [target, keep], 'active_job': active}))
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    monkeypatch.setattr(bot, 'WORKSPACE', str(tmp_path))
    monkeypatch.setattr(bot, 'global_queue', monitor.MonitoredQueue())
    submit_delete(snapshot, target)
    # Match tool_control_runtime.initialized: watcher is scheduled before restoration.
    async def end_iteration(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(bot.asyncio, 'sleep', end_iteration)
    async def once():
        with pytest.raises(asyncio.CancelledError):
            await bot.queue_signal_watcher()
    asyncio.run(once())
    saved = json.loads(snapshot.read_text())
    assert saved.get('active_job') == active, saved
    assert any(j['job_id'] == 'keep_me' for j in saved['items']), saved


def test_invalid_old_operation_does_not_kill_consumer(tmp_path, monkeypatch):
    snapshot = tmp_path / 'telegram_queue.json'
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    queue = monitor.MonitoredQueue()
    queue.put_nowait({'job_id': 'valid_job', 'url': 'https://example.invalid/a'})
    commands(snapshot).mkdir()
    (commands(snapshot) / 'old-operation.json').write_text('{')
    assert queue.get_nowait()['job_id'] == 'valid_job'


def test_cancel_corrupt_schedule_allows_recovery(tmp_path):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.path.write_text('{')
    store.cancel()
    assert store.get()['active'] is False
    assert [p.read_bytes() for p in tmp_path.glob('*.bak')] == [b'{']


@pytest.mark.parametrize('raw', ['{', '[]', '{"schema_version":2,"state":"pending"}'])
def test_replace_corrupt_schedule_preserves_original(tmp_path, raw):
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.path.write_text(raw)
    assert store.create('02:00', 'AUTO')['active'] is True
    assert [p.read_text() for p in tmp_path.glob('*.bak')] == [raw]


def test_failed_backup_does_not_overwrite_corrupt_schedule(tmp_path, monkeypatch):
    import night_schedule_service as module
    store = ScheduleStore(tmp_path / 'schedule.json')
    store.path.write_text('{')
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(module, 'atomic_write_bytes', fail)
    with pytest.raises(OSError):
        store.create('02:00', 'AUTO')
    assert store.path.read_bytes() == b'{'


def test_bad_record_does_not_block_valid_delete(tmp_path, monkeypatch):
    snapshot = tmp_path / 'telegram_queue.json'
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    q = monitor.MonitoredQueue()
    job = {'job_id': 'delete_me'}
    q.put_nowait(job)
    q.put_nowait({'job_id': 'keep_me'})
    submit_delete(snapshot, job)
    (commands(snapshot) / 'bad.json').write_text('{}')
    apply_deletions(q, snapshot)
    assert q.get_nowait()['job_id'] == 'keep_me'
    q.task_done()
    assert q._unfinished_tasks == 0
    assert (commands(snapshot) / 'bad.json').read_text() == '{}'


@pytest.mark.parametrize('fail_recovery', [False, True])
def test_real_post_init_awaits_recovery_before_watcher(tmp_path, monkeypatch, fail_recovery):
    import tool_control_runtime as runtime
    monkeypatch.setattr(runtime, 'FLAGS', tmp_path)
    snapshot = tmp_path / 'telegram_queue.json'
    original = json.dumps({'items': [{'job_id': 'keep_me'}, {'job_id': 'other'}]})
    snapshot.write_text(original)
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    q = monitor.MonitoredQueue()
    events = []
    async def restore(app):
        assert q.recovery_pending
        events.append('restore')
        q.put_nowait({'job_id': 'keep_me'})
        assert snapshot.read_text() == original
        if fail_recovery:
            raise ValueError('invalid snapshot')
    async def watch(app):
        assert not q.recovery_pending
        events.append('watch')
    app = SimpleNamespace(post_init=None, post_shutdown=None, handlers={},
                          add_handler=lambda *a, **k: None, bot_data={})
    runtime.install(app, {'global_queue': q, 'enqueue_pending_queue_jobs': restore,
                         'queue_signal_watcher': watch}, 'v1')
    async def check():
        if fail_recovery:
            with pytest.raises(ValueError):
                await app.post_init(app)
            assert app.bot_data == {}
            assert q.recovery_pending
            assert snapshot.read_text() == original
        else:
            await app.post_init(app)
            await app.bot_data['_queue_watcher']
            app.bot_data['_control_heartbeat'].cancel()
            with pytest.raises(asyncio.CancelledError):
                await app.bot_data['_control_heartbeat']
            assert events == ['restore', 'watch']
    asyncio.run(check())
