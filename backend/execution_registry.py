"""Shared V1 execution leases, cancellation and output reservations.

All mutations use the same OS guard as the legacy batch lease. A terminal
tracker status still occupies its slot until its owner explicitly releases it.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from pipeline_v2.atomic_io import atomic_write_json
from pipeline_v2.interprocess_lock import CrossProcessLock


class AdmissionBusy(RuntimeError):
    pass


class RegistryStateError(ValueError):
    pass


def owner_is_alive(record):
    import psutil
    try:
        process = psutil.Process(record['pid'])
        expected = record.get('owner_create_time')
        # Unknown identity is never evidence that a live PID can be reclaimed.
        if type(expected) not in (int, float) or not math.isfinite(expected) or expected <= 0:
            return True
        return abs(process.create_time() - expected) < .01
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, OSError, ValueError, TypeError):
        return True


def output_key(target):
    return os.path.normcase(str(Path(target).resolve()))


class ExecutionRegistry:
    def __init__(self, workspace, batch_lock):
        self.workspace = Path(workspace)
        self.path = self.workspace / 'bot_system' / 'executions.json'
        self.guard_path = Path(batch_lock).with_suffix('.guard')
        self.legacy_paths = {Path(batch_lock), self.workspace / 'batch.lock', self.workspace / 'bot_system' / 'batch.lock'}

    def guard(self):
        return CrossProcessLock(self.guard_path, name='V1 execution admission', timeout_seconds=5, poll_interval=.02)

    def _read_unlocked(self):
        try:
            data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return {'schema_version': 1, 'generation': 0, 'executions': {}}
        except (OSError, ValueError) as exc:
            raise RegistryStateError('Execution registry is unreadable; admission is blocked.') from exc
        if (not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1
                or type(data.get('generation')) is not int or data['generation'] < 0
                or not isinstance(data.get('executions'), dict)):
            raise RegistryStateError('Invalid execution registry schema.')
        slots = set()
        generations = set()
        reserved_outputs = set()
        for execution_id, record in data['executions'].items():
            if (not isinstance(execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', execution_id)
                    or not isinstance(record, dict) or record.get('execution_id') != execution_id
                    or type(record.get('pid')) is not int or record['pid'] <= 0
                    or type(record.get('generation')) is not int
                    or not 0 < record['generation'] <= data['generation']
                    or record['generation'] in generations
                    or type(record.get('owner_create_time')) not in (int, float)
                    or not math.isfinite(record['owner_create_time']) or record['owner_create_time'] <= 0
                    or type(record.get('occupies_slot')) is not bool
                    or type(record.get('stop_requested')) is not bool
                    or type(record.get('worker_id')) is not int
                    or record.get('worker_id') not in (1, 2)
                    or not isinstance(record.get('tracker'), dict)
                    or not isinstance(record.get('output_targets'), list)
                    or any(not isinstance(target, str) or not target for target in record['output_targets'])
                    or len(record['output_targets']) != len(set(record['output_targets']))):
                raise RegistryStateError('Invalid execution lease; original registry retained.')
            generations.add(record['generation'])
            if record['occupies_slot']:
                if record['worker_id'] in slots:
                    raise RegistryStateError('Conflicting execution slots; admission is blocked.')
                if reserved_outputs.intersection(record['output_targets']):
                    raise RegistryStateError('Conflicting output reservations; admission is blocked.')
                slots.add(record['worker_id'])
                reserved_outputs.update(record['output_targets'])
        return data

    def _save_unlocked(self, data):
        atomic_write_json(self.path, data)

    def _reconcile_unlocked(self, data):
        changed = False
        for record in data['executions'].values():
            if record['occupies_slot'] and not owner_is_alive(record):
                record.update(occupies_slot=False, released_at=time.time(), outcome='interrupted')
                record['tracker'].update(active=False, occupies_slot=False, status='interrupted',
                    video_status='interrupted', eta_seconds=None, step_name='Tiến trình đã thoát; cần kiểm tra khôi phục.')
                changed = True
        return changed

    def has_occupied_unlocked(self):
        data = self._read_unlocked()
        if self._reconcile_unlocked(data):
            self._save_unlocked(data)
        return any(record['occupies_slot'] for record in data['executions'].values())

    def _check_legacy_unlocked(self):
        for path in self.legacy_paths:
            try:
                owner = json.loads(path.read_text(encoding='utf-8-sig'))
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                raise AdmissionBusy('Legacy batch lease is unreadable.') from exc
            if not isinstance(owner, dict) or type(owner.get('pid')) is not int or owner['pid'] <= 0:
                raise AdmissionBusy('Legacy batch owner is unknown.')
            if owner_is_alive(owner):
                raise AdmissionBusy('A legacy batch still owns the pipeline.')

    def snapshot(self):
        if not self.path.exists():
            return {'schema_version': 1, 'generation': 0, 'executions': {}}
        with self.guard():
            data = self._read_unlocked()
            if self._reconcile_unlocked(data):
                self._save_unlocked(data)
            return copy.deepcopy(data)

    def claim(self, execution_id, tracker, *, source_kind='local', output_targets=(), capacity=1):
        if not isinstance(execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', execution_id):
            raise ValueError('Invalid execution ID.')
        capacity = max(1, min(2, int(capacity)))
        targets = list(dict.fromkeys(output_key(path) for path in output_targets))
        import psutil
        with self.guard():
            self._check_legacy_unlocked()
            data = self._read_unlocked()
            self._reconcile_unlocked(data)
            if execution_id in data['executions']:
                raise AdmissionBusy('Execution ID already exists; use a fresh execution ID.')
            active = [record for record in data['executions'].values() if record['occupies_slot']]
            if len(active) >= capacity:
                raise AdmissionBusy('All permitted execution slots are occupied.')
            if any(set(targets) & set(record['output_targets']) for record in active):
                raise AdmissionBusy('Output target is reserved by another execution.')
            worker_id = next(slot for slot in (1, 2) if all(record['worker_id'] != slot for record in active))
            generation = data['generation'] + 1
            state = copy.deepcopy(tracker)
            state.update(job_id=execution_id, execution_id=execution_id, generation=generation,
                         worker_id=worker_id, active=True, occupies_slot=True)
            record = {'execution_id': execution_id, 'generation': generation, 'worker_id': worker_id,
                      'pid': os.getpid(), 'owner_create_time': psutil.Process().create_time(),
                      'source_kind': source_kind, 'created_at': time.time(), 'occupies_slot': True,
                      'stop_requested': False, 'output_targets': targets, 'tracker': state}
            data['generation'] = generation
            data['executions'][execution_id] = record
            self._save_unlocked(data)
            return copy.deepcopy(record)

    @contextmanager
    def owned(self, execution_id):
        """Yield the mutable owned record; stale callbacks get None."""
        with self.guard():
            data = self._read_unlocked()
            record = data['executions'].get(execution_id)
            if (record is None or not record['occupies_slot'] or record['pid'] != os.getpid()
                    or not owner_is_alive(record)):
                yield None
                return
            try:
                yield record
            finally:
                self._save_unlocked(data)

    def reserve_output(self, execution_id, target):
        key = output_key(target)
        with self.guard():
            data = self._read_unlocked()
            self._reconcile_unlocked(data)
            record = data['executions'].get(execution_id)
            if (not record or not record['occupies_slot'] or record['pid'] != os.getpid()
                    or not owner_is_alive(record)):
                raise AdmissionBusy('Execution no longer owns a slot.')
            if any(other['occupies_slot'] and key in other['output_targets']
                   for identity, other in data['executions'].items() if identity != execution_id):
                raise AdmissionBusy('Output target is reserved by another execution.')
            if key not in record['output_targets']:
                record['output_targets'].append(key)
                self._save_unlocked(data)
        return key

    def request_stop(self, execution_id=None):
        with self.guard():
            data = self._read_unlocked()
            ids = []
            for identity, record in data['executions'].items():
                if record['occupies_slot'] and (execution_id is None or identity == execution_id):
                    record['stop_requested'] = True
                    record['tracker'].update(stop_requested=True, active=True, status='stopping',
                        video_status='stopping', eta_seconds=None, step_name='Đang chờ công việc kết thúc')
                    ids.append(identity)
            if ids:
                self._save_unlocked(data)
            return ids

    def release(self, execution_id):
        with self.owned(execution_id) as record:
            if record is None:
                return False
            record.update(occupies_slot=False, released_at=time.time(), outcome=record['tracker'].get('video_status'))
            record['tracker']['occupies_slot'] = False
            if record['tracker'].get('active'):
                record['tracker'].update(active=False, status='interrupted', video_status='interrupted', eta_seconds=None)
            return True
