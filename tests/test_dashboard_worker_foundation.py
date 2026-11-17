"""W9 primitives under isolated fixtures; does not enable the live dispatcher."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import psutil
import pytest

from execution_registry import AdmissionBusy, ExecutionRegistry, RegistryStateError, owner_is_alive
from pipeline_v2.atomic_io import atomic_write_json
import v1_resource_control as resources


@pytest.fixture
def registry(tmp_path):
    return ExecutionRegistry(tmp_path, tmp_path / 'bot_system' / 'batch.lock')


def test_two_slots_have_independent_progress_stop_and_output_reservations(registry, tmp_path):
    first = registry.claim('first', {'percent': 10}, capacity=2,
                           output_targets=[tmp_path / 'a.mp4'])
    second = registry.claim('second', {'percent': 20}, capacity=2,
                            output_targets=[tmp_path / 'b.mp4'])
    assert {first['worker_id'], second['worker_id']} == {1, 2}
    assert second['generation'] > first['generation']
    with pytest.raises(AdmissionBusy):
        registry.claim('third', {}, capacity=2)
    with pytest.raises(AdmissionBusy):
        registry.reserve_output('second', tmp_path / 'a.mp4')
    assert registry.request_stop('first') == ['first']
    with registry.owned('second') as record:
        record['tracker']['percent'] = 65
    snapshot = registry.snapshot()['executions']
    assert snapshot['first']['stop_requested']
    assert not snapshot['second']['stop_requested']
    assert snapshot['first']['tracker']['percent'] == 10
    assert snapshot['second']['tracker']['percent'] == 65


def test_terminal_status_holds_lease_until_cleanup_release(registry, tmp_path):
    registry.claim('first', {}, output_targets=[tmp_path / 'a.mp4'])
    with registry.owned('first') as record:
        record['tracker'].update(active=False, video_status='completed')
    with pytest.raises(AdmissionBusy):
        registry.claim('second', {})
    with pytest.raises(AdmissionBusy):
        registry.claim('second', {}, capacity=2, output_targets=[tmp_path / 'a.mp4'])
    assert registry.release('first')
    registry.claim('second', {}, output_targets=[tmp_path / 'a.mp4'])
    with registry.owned('first') as stale:
        assert stale is None
    assert not registry.release('first')
    with pytest.raises(AdmissionBusy):
        registry.claim('first', {}, capacity=2)


def test_downgrade_blocks_new_admission_without_stopping_existing_workers(registry):
    registry.claim('first', {}, capacity=2)
    registry.claim('second', {}, capacity=2)
    with pytest.raises(AdmissionBusy):
        registry.claim('third', {}, capacity=1)
    assert registry.release('first')
    with pytest.raises(AdmissionBusy):
        registry.claim('third', {}, capacity=1)
    assert not registry.snapshot()['executions']['second']['stop_requested']
    assert registry.release('second')
    registry.claim('third', {}, capacity=1)


def test_concurrent_claims_cannot_overbook_capacity(registry):
    def claim(number):
        try:
            return registry.claim('job_' + str(number), {}, capacity=2)['execution_id']
        except AdmissionBusy:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = [value for value in pool.map(claim, range(8)) if value]
    assert len(admitted) == 2
    assert sum(item['occupies_slot'] for item in registry.snapshot()['executions'].values()) == 2


@pytest.mark.parametrize('field,value', [
    ('owner_create_time', float('nan')), ('owner_create_time', float('inf')),
    ('owner_create_time', -1), ('owner_create_time', None),
    ('pid', True), ('worker_id', True), ('generation', 0), ('generation', 99),
    ('stop_requested', 'false'), ('occupies_slot', 1),
    ('output_targets', [{}]), ('output_targets', ['']),
    ('output_targets', ['duplicate', 'duplicate']),
])
def test_corrupt_lease_blocks_admission_and_keeps_evidence(registry, field, value):
    registry.claim('first', {}, capacity=2)
    data = registry.snapshot()
    data['executions']['first'][field] = value
    # Deliberately bypass the safe writer to simulate corrupt external state.
    registry.path.write_text(json.dumps(data), encoding='utf-8')
    original = registry.path.read_bytes()
    with pytest.raises(RegistryStateError):
        registry.claim('second', {}, capacity=2)
    assert registry.path.read_bytes() == original


def test_corrupt_json_blocks_admission_without_replacing_evidence(registry):
    registry.path.parent.mkdir(parents=True)
    registry.path.write_text('{incomplete', encoding='utf-8')
    with pytest.raises(RegistryStateError):
        registry.claim('first', {})
    assert registry.path.read_text(encoding='utf-8') == '{incomplete'


def test_duplicate_slot_and_generation_fail_closed(registry):
    registry.claim('first', {}, capacity=2)
    registry.claim('second', {}, capacity=2)
    original = registry.snapshot()
    for field in ('worker_id', 'generation'):
        data = copy.deepcopy(original)
        data['executions']['second'][field] = data['executions']['first'][field]
        atomic_write_json(registry.path, data)
        with pytest.raises(RegistryStateError):
            registry.snapshot()


def test_conflicting_persisted_output_reservations_fail_closed(registry, tmp_path):
    registry.claim('first', {}, capacity=2, output_targets=[tmp_path / 'a.mp4'])
    registry.claim('second', {}, capacity=2, output_targets=[tmp_path / 'b.mp4'])
    data = registry.snapshot()
    data['executions']['second']['output_targets'] = data['executions']['first']['output_targets']
    atomic_write_json(registry.path, data)
    with pytest.raises(RegistryStateError):
        registry.snapshot()


@pytest.mark.parametrize('identity', [None, float('nan'), float('inf'), 'bad', -1])
def test_live_legacy_pid_with_unknown_identity_is_retained(identity):
    assert owner_is_alive({'pid': os.getpid(), 'owner_create_time': identity})


def test_recycled_pid_is_reconciled_before_output_reuse(registry, tmp_path):
    registry.claim('first', {}, output_targets=[tmp_path / 'same.mp4'])
    data = registry.snapshot()
    data['executions']['first']['owner_create_time'] = psutil.Process().create_time() - 100
    atomic_write_json(registry.path, data)
    registry.claim('second', {}, output_targets=[tmp_path / 'same.mp4'])
    snapshot = registry.snapshot()['executions']
    assert not snapshot['first']['occupies_slot']
    assert snapshot['first']['tracker']['video_status'] == 'interrupted'
    assert snapshot['second']['occupies_slot']


def test_live_legacy_lock_blocks_both_registry_slots(registry):
    legacy = registry.workspace / 'batch.lock'
    legacy.write_text(json.dumps({'pid': os.getpid(), 'owner_create_time': float('nan')}), encoding='utf-8')
    with pytest.raises(AdmissionBusy):
        registry.claim('first', {}, capacity=2)
    assert legacy.exists()


def test_child_crash_reclaims_only_dead_slot(registry, tmp_path):
    backend = Path(__file__).resolve().parents[1] / 'backend'
    ready = tmp_path / 'child-ready.json'
    code = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from execution_registry import ExecutionRegistry
root = Path(sys.argv[2])
registry = ExecutionRegistry(root, root / 'bot_system' / 'batch.lock')
record = registry.claim('child', {}, capacity=2, output_targets=[root / 'child.mp4'])
Path(sys.argv[3]).write_text(json.dumps(record), encoding='utf-8')
sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(backend), str(tmp_path), str(ready)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists(), 'Child fixture did not acquire its lease'
        registry.claim('parent', {}, capacity=2)
        with pytest.raises(AdmissionBusy):
            registry.claim('third', {}, capacity=2)
        assert registry.request_stop('child') == ['child']
        assert not registry.snapshot()['executions']['parent']['stop_requested']
        child.kill()
        child.communicate(timeout=5)
        registry.claim('replacement', {}, capacity=2, output_targets=[tmp_path / 'child.mp4'])
        state = registry.snapshot()['executions']
        assert state['child']['outcome'] == 'interrupted'
        assert state['parent']['occupies_slot'] and state['replacement']['occupies_slot']
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def admission(settings=None, memory=None, vram=None):
    with patch.dict(os.environ, {'AUTODUB_EXPERIMENTAL_DUAL_WORKERS': '1'}):
        return resources.execution_admission(
            settings={'effective_concurrency': 2, **(settings or {})},
            memory={'available_mb': 8192} if memory is None else memory,
            vram={'available': True, 'free_mb': 4096, 'total_mb': 6144,
                  'percent': 33.3, 'timestamp': time.time(), **(vram or {})})


def test_resource_admission_requires_explicit_opt_in():
    with patch.dict(os.environ, {'AUTODUB_EXPERIMENTAL_DUAL_WORKERS': ''}), \
         patch.object(resources, 'memory_snapshot', side_effect=AssertionError('unneeded telemetry')):
        assert resources.execution_admission({'effective_concurrency': 2})['capacity'] == 1
    assert admission()['capacity'] == 2


@pytest.mark.parametrize('settings', [
    {'effective_concurrency': None}, {'effective_concurrency': '2'},
    {'effective_concurrency': True}, {'effective_concurrency': 99},
    {'min_free_vram_mb': float('nan')}, {'max_vram_percent_threshold': float('inf')},
    {'min_free_vram_mb': -1}, {'max_vram_percent_threshold': 0},
    {'auto_throttled': True},
])
def test_malformed_or_throttled_settings_never_admit_second_worker(settings):
    assert admission(settings=settings)['capacity'] == 1


@pytest.mark.parametrize('memory,vram,reason', [
    ({'available_mb': 4095}, {}, 'ram_below_second_worker_reserve'),
    ({'available_mb': None}, {}, 'resource_telemetry_unavailable'),
    ({'available_mb': float('nan')}, {}, 'resource_telemetry_unavailable'),
    ([], {}, 'resource_telemetry_unavailable'),
    ({'available_mb': 8192}, {'free_mb': 1499}, 'vram_below_second_worker_reserve'),
    ({'available_mb': 8192}, {'percent': 85}, 'vram_pressure'),
    ({'available_mb': 8192}, {'timestamp': 0}, 'resource_telemetry_unavailable'),
    ({'available_mb': 8192}, {'timestamp': float('nan')}, 'resource_telemetry_unavailable'),
    ({'available_mb': 8192}, {'free_mb': 7000}, 'resource_telemetry_unavailable'),
    ({'available_mb': 8192}, {'available': False}, 'resource_telemetry_unavailable'),
    ({'available_mb': 8192}, {'auto_throttled': True}, 'vram_throttled'),
])
def test_resource_pressure_and_missing_telemetry_fail_closed(memory, vram, reason):
    result = admission(memory=memory, vram=vram)
    assert result['capacity'] == 1
    assert result['reason'] == reason


def test_user_thresholds_cannot_relax_hard_reserve():
    assert admission(settings={'min_free_vram_mb': 0}, vram={'free_mb': 1499})['capacity'] == 1
    assert admission(settings={'max_vram_percent_threshold': 100}, vram={'percent': 85})['capacity'] == 1
    assert admission(settings={'min_free_vram_mb': 5000})['capacity'] == 1
