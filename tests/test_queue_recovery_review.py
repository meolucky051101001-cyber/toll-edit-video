"""Recovery and dashboard commands use isolated files, without provider work."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram_bot as bot
import telegram_queue_monitor as monitor
import tool_control_runtime as runtime
from queue_control_service import apply_deletions, apply_reorders, commands, submit_delete, submit_reorder


@pytest.fixture
def queue_state(tmp_path, monkeypatch):
    directory = tmp_path / 'bot_system'
    directory.mkdir()
    snapshot = directory / 'telegram_queue.json'
    queue = monitor.MonitoredQueue()
    monkeypatch.setattr(monitor, 'SNAPSHOT', snapshot)
    monkeypatch.setattr(bot, 'WORKSPACE', str(tmp_path))
    monkeypatch.setattr(bot, 'global_queue', queue)
    monkeypatch.setattr(bot, 'queue_counter', 0)
    monkeypatch.setattr(bot, 'worker_task', SimpleNamespace(done=lambda: False))
    monkeypatch.setattr(bot, '_ensure_network_retry_scheduler', lambda: None)
    monkeypatch.setattr(bot, 'prepare_telegram_job', lambda job: job)
    monkeypatch.setattr(runtime, 'FLAGS', tmp_path / 'control')
    return queue, snapshot


def job(job_id):
    return dict(job_id=job_id, type='url', url='https://example.invalid/' + job_id)


@pytest.mark.parametrize('raw', ['{', '[]', '{"items": {}}',
    json.dumps({'items': [job('valid'), {'job_id': 'invalid', 'type': 'local_file'}]})])
def test_real_startup_retains_invalid_snapshot_and_never_publishes_prefix(queue_state, monkeypatch, raw):
    queue, snapshot = queue_state
    snapshot.write_text(raw, encoding='utf-8')
    app = SimpleNamespace(post_init=None, post_shutdown=None, handlers={},
                          add_handler=lambda *a, **k: None, bot_data={})
    watch = AsyncMock()
    runtime.install(app, {'global_queue': queue,
                         'enqueue_pending_queue_jobs': bot.enqueue_pending_queue_jobs,
                         'queue_signal_watcher': watch}, 'v1')
    with pytest.raises(ValueError):
        asyncio.run(app.post_init(app))
    assert queue.recovery_pending
    assert queue.empty()
    assert queue._unfinished_tasks == 0
    assert snapshot.read_text(encoding='utf-8') == raw
    assert app.bot_data == {}
    watch.assert_not_called()


@pytest.mark.parametrize('state', ['pending', 'applied', 'already_absent'])
def test_restore_filters_persisted_deletions_before_claim(queue_state, state):
    queue, snapshot = queue_state
    target, keep = job('deleted'), job('keep')
    operation = submit_delete(snapshot, target)
    operation['state'] = state
    (commands(snapshot) / (operation['operation_id'] + '.json')).write_text(json.dumps(operation))
    snapshot.write_text(json.dumps({'items': [target, keep]}))
    assert asyncio.run(bot.enqueue_pending_queue_jobs()) == 1
    assert queue.get_nowait()['job_id'] == 'keep'
    queue.task_done()
    assert queue.empty() and queue._unfinished_tasks == 0


def test_restore_keeps_interrupted_active_job_when_delete_was_too_late(queue_state):
    queue, snapshot = queue_state
    active = job('active')
    operation = submit_delete(snapshot, active)
    snapshot.write_text(json.dumps({'items': [], 'active_job': active}))
    assert asyncio.run(bot.enqueue_pending_queue_jobs()) == 1
    assert queue.get_nowait()['job_id'] == 'active'
    assert json.loads((commands(snapshot) / (operation['operation_id'] + '.json')).read_text())['state'] == 'too_late'


def test_offline_clear_preserves_only_interrupted_active_job(queue_state):
    queue, snapshot = queue_state
    snapshot.write_text(json.dumps({'items': [job('pending')], 'active_job': job('active')}))
    flag = snapshot.parent / 'telegram_queue_clear.flag'
    flag.write_text('clear')
    assert asyncio.run(bot.enqueue_pending_queue_jobs()) == 1
    assert not flag.exists()
    assert queue.get_nowait()['job_id'] == 'active'
    queue.task_done()
    assert queue.empty()


def test_snapshot_retry_and_restore_preserve_cloud_and_local_source_contract(queue_state):
    queue, snapshot = queue_state
    original = dict(job('local'), type='local_file', url='', filename='source.mp4',
                    video_path='C:/allowed/source.mp4', voice='chosen', execution_mode='cloud',
                    force_cloud=True, cloud_gpu='L4', cloud_rollout_permit={'permit': 'frozen'},
                    idempotency_key='tg:local', allow_cloud_fallback=False, allow_no_speech=True,
                    resume_state={'source_sha256': 'expected-hash', 'source_path': 'C:/allowed/source.mp4'})
    queue.put_nowait(original)
    snapshot_job = json.loads(snapshot.read_text())['items'][0]
    retry_job = bot._serialize_retry_job(original)
    for field in original:
        assert snapshot_job[field] == original[field]
        assert retry_job[field] == original[field]
    queue._queue.clear()
    queue._unfinished_tasks = 0
    assert asyncio.run(bot.enqueue_pending_queue_jobs()) == 1
    restored = queue.get_nowait()
    for field in original:
        assert restored[field] == original[field]


def test_watcher_acknowledges_delete_and_reorder_while_worker_is_busy(queue_state, monkeypatch):
    queue, snapshot = queue_state
    for name in ['active', 'one', 'deleted', 'two']:
        queue.put_nowait(job(name))
    queue.get_nowait()
    deletion = submit_delete(snapshot, job('deleted'))
    reorder = submit_reorder(snapshot, [job('two'), job('deleted'), job('one')])
    queue.put_nowait(job('new'))
    async def stop_after_iteration(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(bot.asyncio, 'sleep', stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.queue_signal_watcher())
    assert queue.active_job['job_id'] == 'active'
    assert [item['job_id'] for item in queue._queue] == ['two', 'one', 'new']
    assert queue._unfinished_tasks == 4
    assert json.loads((commands(snapshot) / (deletion['operation_id'] + '.json')).read_text())['state'] == 'applied'
    assert json.loads((snapshot.parent / 'queue_reorders' / (reorder['operation_id'] + '.json')).read_text())['state'] == 'applied'
    apply_deletions(queue, snapshot)
    apply_reorders(queue, snapshot)
    assert queue._unfinished_tasks == 4


def test_reorder_ack_waits_for_snapshot_publication(queue_state, monkeypatch):
    queue, snapshot = queue_state
    queue.put_nowait(job('one'))
    queue.put_nowait(job('two'))
    operation = submit_reorder(snapshot, [job('two'), job('one')])
    path = snapshot.parent / 'queue_reorders' / (operation['operation_id'] + '.json')
    publish = queue.publish
    monkeypatch.setattr(queue, 'publish', lambda: False)
    apply_reorders(queue, snapshot)
    assert json.loads(path.read_text())['state'] == 'pending'
    monkeypatch.setattr(queue, 'publish', publish)
    apply_reorders(queue, snapshot)
    assert json.loads(path.read_text())['state'] == 'applied'
    assert [item['job_id'] for item in json.loads(snapshot.read_text())['items']] == ['two', 'one']


def test_clear_survives_failed_snapshot_write_and_restart(queue_state, monkeypatch):
    queue, snapshot = queue_state
    queue.put_nowait(job('active'))
    queue.get_nowait()
    queue.put_nowait(job('clear_me'))
    original = snapshot.read_bytes()
    flag = snapshot.parent / 'telegram_queue_clear.flag'
    flag.write_text('clear')
    async def stop_after_iteration(*args):
        raise asyncio.CancelledError
    def fail_snapshot(*args, **kwargs):
        raise OSError('disk write failed')
    publish_json = monitor.atomic_write_json
    monkeypatch.setattr(monitor, 'atomic_write_json', fail_snapshot)
    monkeypatch.setattr(bot.asyncio, 'sleep', stop_after_iteration)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.queue_signal_watcher())
    assert queue.active_job['job_id'] == 'active'
    assert queue.empty() and queue._unfinished_tasks == 1
    assert snapshot.read_bytes() == original
    # The consumed flag is replaced by durable per-job deletion records.
    assert not flag.exists()
    records = [json.loads(path.read_text()) for path in commands(snapshot).glob('*.json')]
    assert [record['job_id'] for record in records] == ['clear_me']
    monkeypatch.setattr(monitor, 'atomic_write_json', publish_json)
    restored = monitor.MonitoredQueue()
    monkeypatch.setattr(bot, 'global_queue', restored)
    assert asyncio.run(bot.enqueue_pending_queue_jobs()) == 1
    assert restored.get_nowait()['job_id'] == 'active'
    restored.task_done()
    assert restored.empty() and restored._unfinished_tasks == 0


def test_dashboard_commands_do_not_write_consumer_snapshot(queue_state, monkeypatch):
    import main
    from fastapi.testclient import TestClient
    queue, snapshot = queue_state
    for name in ['active', 'one', 'two']:
        queue.put_nowait(job(name))
    queue.get_nowait()
    original = snapshot.read_bytes()
    monkeypatch.setattr(main, '_get_system_path', lambda name: snapshot.parent / name)
    client = TestClient(main.app, headers={'X-Local-Control-Token': main.LOCAL_TOKEN})
    moved = client.post('/api/queue/move', json={'index': 1, 'action': 'top'})
    assert moved.status_code == 200
    assert snapshot.read_bytes() == original
    cleared = client.post('/api/queue/clear')
    assert cleared.status_code == 200 and cleared.json()['status'] == 'pending'
    assert snapshot.read_bytes() == original
    assert list((snapshot.parent / 'queue_reorders').glob('*.json'))
