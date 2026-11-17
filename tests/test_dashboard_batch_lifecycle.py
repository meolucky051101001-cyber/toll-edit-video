"""Batch retry/recovery acceptance using real stores, API and a stub runner."""
import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_dashboard_acceptance_api import api
from batch_plan_service import PlanConflict
from night_schedule_service import ScheduleStore


def prepare(api, key='batch-a', filename='A.mp4'):
    response = api.client.post('/api/jobs/batch', json={'idempotency_key': key, 'trigger': False,
        'items': [{'filename': filename, 'overrides': {'speed': 1.1}}]})
    assert response.status_code == 201, response.text
    return api.client.get('/api/batches/' + response.json()['batch_id']).json()


def schedule(api, plan, key='schedule-a'):
    payload = {'target_kind': 'local', 'batch_id': plan['batch_id'], 'time': '02:00',
               'operation_id': key, **plan['action_identity']}
    response = api.client.post('/api/schedule', json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def cancel(api, scheduled):
    return api.client.request('DELETE', '/api/schedule', json={key: scheduled[key]
        for key in ('schedule_id', 'generation', 'operation_id')})


def get(api, plan):
    return api.client.get('/api/batches/' + plan['batch_id']).json()


def test_prepared_retry_reuses_frozen_plan_once_after_busy(api, monkeypatch):
    import job_tracker
    import job_config_service
    received = []
    async def runner(*args, **kwargs):
        received.append(kwargs['batch_plan'])
    monkeypatch.setattr(api.main, 'batch_runner', runner)
    payload = {'idempotency_key': 'retry-same', 'trigger': True,
               'items': [{'filename': 'A.mp4', 'overrides': {'speed': 1.1}}]}
    job_tracker.start_batch(1, job_id='busy-owner')
    assert api.client.post('/api/jobs/batch', json=payload).status_code == 409
    before = api.client.get('/api/batch-operations/retry-same').json()
    job_tracker.fail_batch('done', job_id='busy-owner')
    job_tracker.release_batch('busy-owner')
    monkeypatch.setattr(job_config_service, 'freeze_job_config', lambda **kw: pytest.fail('must not freeze a replay'))
    response = api.client.post('/api/jobs/batch', json=payload)
    assert response.status_code == 202, response.text
    assert api.client.post('/api/jobs/batch', json=payload).status_code == 200
    assert len(received) == 1
    assert response.json()['items'] == before['items']
    assert response.json()['batch_id'] == before['batch_id']


def test_canceled_request_drains_reservation_and_hands_off_once(api, monkeypatch):
    plan = prepare(api)
    reserved, release = threading.Event(), threading.Event()
    original = api.main._reserve_batch_plan
    runs = []
    def reserve(*args):
        result = original(*args)
        reserved.set()
        assert release.wait(5)
        return result
    async def runner(*args, **kwargs):
        runs.append(kwargs['batch_plan']['batch_id'])
    monkeypatch.setattr(api.main, '_reserve_batch_plan', reserve)
    monkeypatch.setattr(api.main, 'batch_runner', runner)
    async def run():
        request = asyncio.create_task(api.main.admit_batch_plan(plan['batch_id']))
        assert await asyncio.to_thread(reserved.wait, 5)
        request.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        await api.main.BATCH_TASK
    asyncio.run(run())
    assert runs == [plan['batch_id']]
    assert get(api, plan)['state'] == 'accepted'


def test_cancel_reschedule_abandon_and_select_same_source(api):
    original = prepare(api)
    first, _ = schedule(api, original)
    assert cancel(api, first).json()['state'] == 'canceled'
    held = get(api, original)
    assert held['state'] == 'scheduled'
    assert held['actions'] == ['start', 'reschedule', 'abandon']
    second, replay = schedule(api, held, 'schedule-again')
    assert second['schedule_id'] != first['schedule_id']
    assert second['generation'] > first['generation']
    assert get(api, original)['items'] == original['items']
    # Lost cancel ACK for the previous schedule must never cancel this new one.
    assert cancel(api, first).json()['state'] == 'canceled'
    assert api.client.get('/api/schedule').json()['state'] == 'pending'
    assert api.client.post('/api/schedule', json=replay).json()['schedule_id'] == second['schedule_id']
    assert cancel(api, second).status_code == 200
    held = get(api, original)
    abandoned = api.client.post('/api/batches/' + held['batch_id'] + '/abandon', json=held['action_identity'])
    assert abandoned.json()['state'] == 'abandoned'
    assert api.client.post('/api/batches/' + held['batch_id'] + '/abandon', json=held['action_identity']).json()['state'] == 'abandoned'
    assert api.client.post('/api/batches/' + held['batch_id'] + '/start', json=held['action_identity']).json()['state'] == 'abandoned'
    assert (api.source / 'A.mp4').read_bytes() == b'A'
    newer = prepare(api, 'batch-new')
    assert schedule(api, newer, 'schedule-new')[0]['state'] == 'pending'


def test_canceled_batch_remains_discoverable_after_other_schedule(api):
    first = prepare(api)
    old, _ = schedule(api, first)
    cancel(api, old)
    second = prepare(api, 'batch-b', 'B.mp4')
    schedule(api, second, 'night-b')
    recovered = api.client.get('/api/batches?recoverable=true').json()['items']
    held = next(p for p in recovered if p['batch_id'] == first['batch_id'])
    assert held['actions'] == ['start', 'reschedule', 'abandon']
    assert api.client.get('/api/schedule-operations/' + old['operation_id']).json()['state'] == 'canceled'


def test_stale_revision_binding_and_missing_cancel_identity_are_rejected(api):
    first = prepare(api)
    scheduled, _ = schedule(api, first)
    assert api.client.request('DELETE', '/api/schedule', json={}).status_code == 422
    cancel(api, scheduled)
    path = '/api/batches/' + first['batch_id']
    assert api.client.post(path + '/abandon', json=first['action_identity']).status_code == 409
    current = get(api, first)
    wrong = {**current['action_identity'], 'schedule_generation': 999}
    assert api.client.post(path + '/start', json=wrong).status_code == 409
    assert api.client.post(path + '/abandon', json=wrong).status_code == 409
    assert get(api, first)['state'] == 'scheduled'


@pytest.mark.parametrize('failure', ['schedule', 'ledger', 'binding'])
def test_half_committed_schedule_retains_hold_until_explicit_replay(api, monkeypatch, failure):
    import night_schedule_service
    from batch_plan_service import BatchPlanStore
    plan = prepare(api)
    lifecycle = api.main._batch_lifecycle()
    payload = {'target_kind': 'local', 'batch_id': plan['batch_id'], 'time': '02:00',
               'operation_id': 'half-write', **plan['action_identity']}
    with monkeypatch.context() as patch:
        if failure in ('schedule', 'ledger'):
            original = night_schedule_service.atomic_write_json
            def write(path, data):
                if (failure == 'ledger' and 'schedule_operations' in str(path)) or (failure == 'schedule' and str(path).endswith('schedule.json')):
                    raise OSError('injected ledger failure')
                return original(path, data)
            patch.setattr(night_schedule_service, 'atomic_write_json', write)
        else:
            original = BatchPlanStore.update
            def update(self, *args, **kwargs):
                if kwargs.get('schedule_id'):
                    raise OSError('injected binding failure')
                return original(self, *args, **kwargs)
            patch.setattr(BatchPlanStore, 'update', update)
        with pytest.raises(OSError):
            lifecycle.create_schedule(payload)
    held = lifecycle.get(plan['batch_id'])
    assert held['state'] == 'scheduled' and held['actions'] == []
    if failure != 'schedule':
        current = lifecycle.schedules.get()
        assert lifecycle.schedules.claim(current['run_at'])['state'] == 'needs_review'
        assert lifecycle.schedules.claim(current['run_at'] + 1) is None
    else:
        with pytest.raises(PlanConflict):
            lifecycle.create_schedule({**payload, 'time':'03:00'})
        assert not lifecycle.schedules.get()['active']
    fixed = lifecycle.create_schedule(payload)
    assert fixed['binding_confirmed'] and fixed['state'] == 'pending'
    assert fixed['run_at'] == held['schedule_intent']['run_at']
    assert lifecycle.get(plan['batch_id'])['schedule_id'] == fixed['schedule_id']


def test_legacy_current_repairs_ledger_but_unknown_history_keeps_reservation(api):
    plan = prepare(api)
    scheduled, _ = schedule(api, plan)
    cancel(api, scheduled)
    store = api.main._schedule_store()
    ledger = store._ledger_path(scheduled['operation_id'])
    ledger.write_text(json.dumps(scheduled), encoding='utf-8')
    assert store.operation(scheduled['operation_id'])['state'] == 'canceled'
    assert json.loads(ledger.read_text())['state'] == 'canceled'
    new = prepare(api, 'b', 'B.mp4')
    schedule(api, new, 'new-night')
    # Simulate a legacy operation record that never recorded cancellation.
    ledger.write_text(json.dumps(scheduled), encoding='utf-8')
    unknown = get(api, plan)
    assert unknown['state'] == 'scheduled' and unknown['actions'] == []
    assert 'needs_review' in unknown['recovery_reason']


def test_dispatch_uncertainty_cannot_be_canceled_or_abandoned(api):
    plan = prepare(api)
    scheduled, _ = schedule(api, plan)
    store = api.main._schedule_store()
    store.claim(scheduled['run_at'])
    store.claim(scheduled['run_at'] + 301)
    assert cancel(api, scheduled).status_code == 409
    held = get(api, plan)
    assert held['actions'] == []
    assert api.client.post('/api/batches/' + plan['batch_id'] + '/abandon', json=held['action_identity']).status_code == 409


def test_stale_schedule_generation_cannot_admit_or_finish_even_after_acceptance(api):
    plan = prepare(api)
    scheduled, _ = schedule(api, plan)
    store = api.main._schedule_store()
    claim = store.claim(scheduled['run_at'])
    stale = {**claim, 'generation': claim['generation'] + 1}
    assert api.main._reserve_batch_plan(plan['batch_id'], stale, False, None, False)[0] == 409
    store.finish(claim['schedule_id'], 'accepted', 'bad-exec', stale['generation'])
    assert store.get()['state'] == 'dispatching'
    assert api.main._reserve_batch_plan(plan['batch_id'], claim, False, None, False)[0] == 202
    assert api.main._reserve_batch_plan(plan['batch_id'], stale, False, None, False)[0] == 409
    assert api.main._reserve_batch_plan(plan['batch_id'], claim, False, None, False)[0] == 200


def test_explicit_replay_confirms_legacy_current_binding_without_reactivating_cancel(api):
    plan = prepare(api)
    scheduled, payload = schedule(api, plan)
    cancel(api, scheduled)
    store = api.main._schedule_store()
    legacy = json.loads(store.path.read_text())
    legacy.pop('binding_confirmed')
    store.path.write_text(json.dumps(legacy), encoding='utf-8')
    replay = api.client.post('/api/schedule', json=payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()['state'] == 'canceled' and replay.json()['binding_confirmed'] is True
    assert replay.json()['schedule_id'] == scheduled['schedule_id']


@pytest.mark.parametrize('winner', ['cancel', 'claim'])
def test_cancel_claim_linearization_uses_lock_and_barrier(tmp_path, winner):
    store = ScheduleStore(tmp_path / 'control' / 'schedule.json')
    scheduled = store.create('02:00', target_kind='local', batch_id='batch', operation_id='op')
    entered, contender_ready = threading.Event(), threading.Event()
    identity = {key: scheduled[key] for key in ('schedule_id', 'generation', 'operation_id')}
    def first():
        from batch_lifecycle_lock import lifecycle_lock
        with lifecycle_lock(store.path.parent):
            entered.set()
            assert contender_ready.wait(5)
            return store.cancel(**identity) if winner == 'cancel' else store.claim(scheduled['run_at'])
    def second():
        assert entered.wait(5)
        contender_ready.set()
        try:
            return store.claim(scheduled['run_at']) if winner == 'cancel' else store.cancel(**identity)
        except RuntimeError as exc:
            return str(exc)
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        one, two = a.result(timeout=10), b.result(timeout=10)
    if winner == 'cancel':
        assert one['state'] == 'canceled' and two is None
    else:
        assert one['state'] == 'dispatching' and 'already_dispatching' in two


@pytest.mark.parametrize('winner', ['abandon', 'reschedule', 'start'])
def test_competing_batch_actions_cannot_resurrect_or_dispatch_twice(api, winner):
    plan = prepare(api)
    scheduled, _ = schedule(api, plan)
    cancel(api, scheduled)
    held = get(api, plan)
    lifecycle = api.main._batch_lifecycle()
    entered, contender_ready = threading.Event(), threading.Event()
    def act(action):
        if action == 'start':
            return api.main._reserve_batch_plan(plan['batch_id'], None, True, held['action_identity'], False)
        if action == 'abandon':
            return lifecycle.abandon(plan['batch_id'], held['action_identity'])
        return lifecycle.create_schedule({'target_kind':'local', 'batch_id':plan['batch_id'], 'time':'03:00',
            'operation_id':'reschedule-race', **held['action_identity']})
    def first():
        with lifecycle.guard():
            entered.set()
            assert contender_ready.wait(5)
            return act(winner)
    def second():
        assert entered.wait(5)
        contender_ready.set()
        try:
            return act('abandon' if winner == 'start' else 'start')
        except PlanConflict:
            return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        one, two = a.result(timeout=10), b.result(timeout=10)
    final = lifecycle.plans.get(plan['batch_id'])
    assert final['state'] == {'start':'accepted', 'abandon':'abandoned', 'reschedule':'scheduled'}[winner]
    if winner == 'abandon':
        assert two[0] == 200 and two[1]['state'] == 'abandoned' and two[2] is None
    else:
        assert two == 'conflict'
