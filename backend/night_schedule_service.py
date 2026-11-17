"""One-shot schedules bound to exact local plans or held Telegram job IDs."""
import asyncio
import hashlib
import inspect
import json
import math
import re
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import contextmanager
from batch_lifecycle_lock import lifecycle_lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pipeline_v2.atomic_io import atomic_write_json, atomic_write_bytes
from pipeline_v2.interprocess_lock import CrossProcessLock
from queue_control_service import submit_command, read_operation, valid_id

ACTIVE_STATES = {'holding', 'pending', 'dispatching', 'canceling'}
GRACE_SECONDS = 1800

class ScheduleStore:
    def __init__(self, path, queue_snapshot=None):
        self.path = Path(path)
        self.queue_snapshot = Path(queue_snapshot) if queue_snapshot else None

    @contextmanager
    def _lock(self):
        with lifecycle_lock(self.path.parent):
            with CrossProcessLock(str(self.path) + '.lock', timeout_seconds=5):
                yield

    def _ledger_path(self, operation_id):
        if not valid_id(operation_id):
            raise ValueError('Operation ID không hợp lệ.')
        return self.path.parent / 'schedule_operations' / (operation_id + '.json')

    def _save(self, data):
        # Current is authoritative if a crash interrupts the ledger write. Before
        # replacing current, create() repairs its ledger under the same lock.
        atomic_write_json(self.path, data)
        if data.get('operation_id'):
            atomic_write_json(self._ledger_path(data['operation_id']), data)

    def _operation(self, operation_id, current):
        path = self._ledger_path(operation_id)
        if current and current.get('operation_id') == operation_id:
            atomic_write_json(path, current)
            return current
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('operation_id') != operation_id:
            raise ValueError('Invalid schedule operation record')
        # Old versions only saved the initial pending state. That history cannot
        # prove cancellation or admission once another schedule replaced it.
        if data.get('state') in ACTIVE_STATES:
            data = {**data, 'state': 'needs_review', 'reason': 'historical_ack_unknown'}
        return data

    def operation(self, operation_id):
        with self._lock():
            current = self._reconcile(self._read())
            data = self._operation(operation_id, current)
            if data is None:
                raise FileNotFoundError(operation_id)
            return {**data, 'active': data['state'] in ACTIVE_STATES}

    @staticmethod
    def next_run(hhmm, now, timezone='Asia/Bangkok'):
        if not isinstance(hhmm, str) or not re.fullmatch(r'([01]?\d|2[0-3]):[0-5]\d', hhmm):
            raise ValueError('Giờ không hợp lệ; dùng HH:MM.')
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ValueError('Múi giờ không hợp lệ hoặc máy chưa cài dữ liệu tzdata.') from exc
        hour, minute = map(int, hhmm.split(':'))
        current = datetime.fromtimestamp(now, zone)
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target.timestamp()

    def _read(self):
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('Invalid schedule schema')
        if data.get('schema_version') != 3:
            if data.get('schema_version') not in (None, 1, 2):
                raise ValueError('Unsupported schedule schema')
            # A legacy clock-only schedule has no immutable target. Never guess it.
            atomic_write_bytes(str(self.path) + '.legacy-' + uuid.uuid4().hex + '.bak', self.path.read_bytes())
            data.update(schema_version=3, schedule_id=data.get('schedule_id') or uuid.uuid4().hex,
                        state='fired' if data.get('last_fired_date') or data.get('state') == 'fired' else 'needs_review',
                        reason='legacy_schedule_requires_target', generation=1)
            self._save(data)
        if not valid_id(data.get('schedule_id')) or data.get('state') not in ACTIVE_STATES | {'fired', 'expired', 'canceled', 'needs_review'}:
            raise ValueError('Invalid schedule state')
        if data['state'] in ACTIVE_STATES:
            if (type(data.get('run_at')) not in (int, float) or data.get('target_kind') not in ('local', 'telegram')
                    or type(data.get('generation')) is not int):
                raise ValueError('Invalid schedule target/timing')
        return data

    def _read_for_edit(self):
        try:
            return self._read()
        except (ValueError, KeyError, TypeError, AttributeError):
            atomic_write_bytes(str(self.path) + '.corrupt-' + uuid.uuid4().hex + '.bak', self.path.read_bytes())
            return None

    def _reconcile(self, data):
        if not data or data.get('target_kind') != 'telegram' or not self.queue_snapshot:
            return data
        key = {'holding': 'hold_operation_id', 'dispatching': 'dispatch_operation_id', 'canceling': 'cancel_operation_id'}.get(data['state'])
        if not key or not data.get(key):
            return data
        operation = read_operation(self.queue_snapshot, data[key])
        if operation and operation['state'] != 'pending':
            if operation['state'] == 'applied':
                data['state'] = {'holding': 'pending', 'dispatching': 'fired', 'canceling': 'canceled'}[data['state']]
            elif operation['state'] == 'expired' and data['state'] == 'dispatching':
                data.update(state='expired', reason='queue_release_expired')
            else:
                data.update(state='needs_review', reason='queue_' + operation['state'])
            self._save(data)
        return data

    def get(self):
        with self._lock():
            data = self._reconcile(self._read())
            if data and data.get('operation_id'):
                self._operation(data['operation_id'], data)
            return {'active': False} if data is None else {**data, 'active': data['state'] in ACTIVE_STATES}

    def create(self, hhmm, mode='AUTO', now=None, *, target_kind=None, batch_id=None,
               target_job_ids=None, expected_revision=None, operation_id=None, timezone='Asia/Bangkok', require_binding=False,
               planned_run_at=None):
        now = time.time() if now is None else now
        run_at = self.next_run(hhmm, now, timezone)
        if planned_run_at is not None:
            if type(planned_run_at) not in (int, float) or not math.isfinite(planned_run_at):
                raise ValueError('Thời điểm lịch đã lưu không hợp lệ.')
            run_at = planned_run_at
        if target_kind not in ('local', 'telegram'):
            raise ValueError('Lịch phải chỉ rõ target_kind local/telegram.')
        targets = list(target_job_ids or [])
        if target_kind == 'local' and (not valid_id(batch_id) or targets):
            raise ValueError('Lịch local phải gắn một batch_id.')
        if target_kind == 'telegram' and (batch_id or not targets or any(not valid_id(j) for j in targets) or len(set(targets)) != len(targets) or not self.queue_snapshot):
            raise ValueError('Lịch Telegram cần snapshot và tập job IDs hợp lệ.')
        if target_kind == 'telegram' and type(expected_revision) is not int:
            raise ValueError('Lịch Telegram cần revision queue.')
        operation_id = operation_id or uuid.uuid4().hex
        if not valid_id(operation_id):
            raise ValueError('Operation ID không hợp lệ.')
        intent = [hhmm, mode, target_kind, batch_id, targets, expected_revision, timezone]
        payload_key = hashlib.sha256(json.dumps(intent, sort_keys=True).encode()).hexdigest()
        with self._lock():
            current = self._reconcile(self._read_for_edit())
            if current and current.get('operation_id'):
                self._operation(current['operation_id'], current)
            known = self._operation(operation_id, current)
            if known:
                if known.get('payload_key') != payload_key:
                    raise RuntimeError('Operation ID đã dùng với lịch khác.')
                return {**known, 'active': known['state'] in ACTIVE_STATES}
            if current and current['state'] in ACTIVE_STATES:
                raise RuntimeError('Đã có lịch đang chờ; hãy hủy hoặc đối soát lịch đó trước.')
            sid = uuid.uuid4().hex
            data = dict(schema_version=3, schedule_id=sid, generation=int((current or {}).get('generation', 0)) + 1,
                        operation_id=operation_id, payload_key=payload_key, time=hhmm, video_mode=mode,
                        timezone=timezone, created_at=now, run_at=run_at, target_kind=target_kind,
                        batch_id=batch_id, target_job_ids=targets, expected_revision=expected_revision,
                        binding_confirmed=not require_binding,
                        state='holding' if target_kind == 'telegram' else 'pending',
                        dispatch_operation_id='release_' + sid, hold_operation_id='hold_' + sid)
            # Persist the exact target before submitting its hold. Crash here is
            # recoverable through this same operation ID; it never starts jobs.
            self._save(data)
            if target_kind == 'telegram':
                try:
                    self._submit_hold(data)
                except Exception:
                    data.update(state='needs_review', reason='hold_not_confirmed')
                    self._save(data)
                    raise
            return {**data, 'active': True}

    def confirm_binding(self, schedule_id, generation):
        with self._lock():
            data = self._read()
            if not data or (data['schedule_id'], data['generation']) != (schedule_id, generation):
                raise RuntimeError('stale_state: Lịch đã thay đổi.')
            if data.get('binding_confirmed') is not True:
                data['binding_confirmed'] = True
                if data.get('reason') == 'binding_incomplete':
                    data.update(state='pending', reason=None)
                self._save(data)
            return {**data, 'active': data['state'] in ACTIVE_STATES}

    def _submit_hold(self, data):
        return submit_command(self.queue_snapshot, 'hold', data['target_job_ids'],
                              expected_revision=data['expected_revision'], operation_id=data['hold_operation_id'],
                              payload={**{k: data[k] for k in ('schedule_id', 'generation', 'run_at')},
                                       'expires_at': data['run_at'] + GRACE_SECONDS})

    def _submit_cancel(self, data):
        return submit_command(self.queue_snapshot, 'cancel_schedule_hold', data['target_job_ids'],
                              operation_id=data['cancel_operation_id'],
                              payload={k: data[k] for k in ('schedule_id', 'generation')})

    def cancel(self, *, schedule_id=None, generation=None, operation_id=None):
        with self._lock():
            data = self._reconcile(self._read_for_edit())
            if schedule_id is not None:
                known = self._operation(operation_id, data)
                if not known or (known.get('schedule_id'), known.get('generation')) != (schedule_id, generation):
                    raise RuntimeError('stale_state: Không xác minh được lịch cần hủy.')
                if known['state'] == 'canceled':
                    return {**known, 'active': False, 'manual_release_required': True}
                if not data or (data.get('schedule_id'), data.get('generation')) != (schedule_id, generation):
                    raise RuntimeError('stale_state: Lịch hiện tại đã thay đổi.')
            if not data:
                self.path.unlink(missing_ok=True)
                return {'active': False, 'state': 'canceled'}
            if data['state'] in ('dispatching', 'fired') or (data.get('dispatch_at') and data['state'] == 'needs_review'):
                raise RuntimeError('already_dispatching: Lịch đang/đã bàn giao; hãy kiểm tra execution trước khi dừng.')
            if data['state'] == 'canceled':
                return {**data, 'active': False}
            if data.get('target_kind') == 'telegram':
                data.update(state='canceling', cancel_operation_id='cancel_' + data['schedule_id'])
                self._save(data)
                self._submit_cancel(data)
                data = self._reconcile(data)
            else:
                data['state'] = 'canceled'
                self._save(data)
            return {**data, 'active': data['state'] in ACTIVE_STATES, 'manual_release_required': True}

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self._lock():
            data = self._reconcile(self._read())
            if not data:
                return None
            if data['state'] == 'holding':
                # A crash between schedule persistence and hold submission is repaired
                # idempotently; failure leaves the schedule visible as needs_review.
                self._submit_hold(data)
                return None
            if data['state'] == 'canceling':
                self._submit_cancel(data)
                return None
            if data['state'] == 'dispatching':
                if data['target_kind'] == 'telegram':
                    if now - data['run_at'] > GRACE_SECONDS:
                        data.update(state='expired', reason='queue_release_expired')
                        self._save(data)
                    return data
                if now - data.get('dispatch_at', now) > 300:
                    data.update(state='needs_review', reason='dispatch_ack_timeout')
                    self._save(data)
                    return data
                return None
            if data['state'] != 'pending' or now < data['run_at']:
                return None
            if data.get('binding_confirmed') is False:
                data.update(state='needs_review', reason='binding_incomplete')
                self._save(data)
                return data
            if now - data['run_at'] > GRACE_SECONDS:
                data['state'] = 'expired'
                self._save(data)
                # Expiry retains hold. A manual release remains explicit.
                return data
            data.update(state='dispatching', dispatch_at=now)
            self._save(data)
            return data

    def finish(self, schedule_id, outcome, batch_job_id=None, generation=None):
        with self._lock():
            data = self._read()
            if not data or data['schedule_id'] != schedule_id or data['state'] != 'dispatching' or (generation is not None and data['generation'] != generation):
                return
            data['state'] = {'accepted': 'fired', 'busy': 'pending', 'pending': 'dispatching'}.get(outcome, 'needs_review')
            data['batch_job_id'] = batch_job_id
            self._save(data)


async def dispatch_due(store, dispatch_local, now=None, admission_allowed=None):
    if admission_allowed is not None:
        allowed = admission_allowed()
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if not allowed:
            # Still expire overdue schedules without clearing their holds.
            current = await asyncio.to_thread(store.get)
            current_time = time.time() if now is None else now
            if not current.get('run_at') or current_time - current['run_at'] <= GRACE_SECONDS:
                return 'busy'
    data = await asyncio.to_thread(store.claim, now)
    if not data:
        return None
    if data['state'] in ('expired', 'needs_review'):
        return data['state']
    outcome, job_id = 'unknown', None
    try:
        if data['target_kind'] == 'telegram':
            operation = await asyncio.to_thread(submit_command, store.queue_snapshot,
                'release_scheduled', data['target_job_ids'], operation_id=data['dispatch_operation_id'],
                payload={**{k: data[k] for k in ('schedule_id', 'generation')},
                         'not_before': data['run_at'], 'expires_at': data['run_at'] + GRACE_SECONDS})
            outcome = 'accepted' if operation['state'] == 'applied' else 'pending'
        else:
            response = await dispatch_local(data)
            if isinstance(response, dict):
                outcome = response.get('status', 'unknown')
                job_id = response.get('job_id') or response.get('batch_id')
            elif response.status_code in (200, 202):
                outcome = 'accepted'
                job_id = json.loads(response.body).get('job_id')
            elif response.status_code == 409:
                outcome = 'busy'
    except Exception as exc:
        if getattr(exc, 'status_code', None) == 409:
            outcome = 'busy'
    finally:
        await asyncio.to_thread(store.finish, data['schedule_id'], outcome, job_id, data['generation'])
    return outcome
