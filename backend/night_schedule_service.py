"""Persist one-shot schedules and claim dispatch before handing work to the batch API."""
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from pipeline_v2.atomic_io import atomic_write_json, atomic_write_bytes
from pipeline_v2.interprocess_lock import CrossProcessLock


class ScheduleStore:
    def __init__(self, path):
        self.path = Path(path)

    def _lock(self):
        return CrossProcessLock(str(self.path) + '.lock', timeout_seconds=5)

    @staticmethod
    def next_run(hhmm, now):
        hour, minute = map(int, hhmm.split(':'))
        current = datetime.fromtimestamp(now)
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
        if data.get('schema_version') == 2:
            if (data.get('state') not in ('pending', 'dispatching', 'fired', 'expired', 'needs_review')
                    or not isinstance(data.get('schedule_id'), str)):
                raise ValueError('Invalid schedule state')
            if data['state'] in ('pending', 'dispatching'):
                if not isinstance(data.get('run_at'), (int, float)) or not data.get('video_mode'):
                    raise ValueError('Invalid schedule timing')
            if data['state'] == 'dispatching' and not isinstance(data.get('dispatch_at'), (int, float)):
                raise ValueError('Invalid dispatch timestamp')
        if data.get('schema_version') != 2:
            # Legacy schedules are anchored to creation, never to migration time.
            data.update(schema_version=2, schedule_id=uuid.uuid4().hex)
            data.setdefault('video_mode', 'AUTO')
            if data.get('last_fired_date'):
                data['state'] = 'fired'
            elif isinstance(data.get('created_at'), (int, float)):
                data['run_at'] = self.next_run(data['time'], data['created_at'])
                data['state'] = 'pending'
            else:
                data['state'] = 'needs_review'
            atomic_write_json(self.path, data)
        return data

    def _read_for_edit(self):
        try:
            return self._read()
        except (ValueError, KeyError, TypeError, AttributeError):
            # An explicit cancel/replace may recover corruption. Keep exact bytes
            # before modifying the original; backup failure leaves it untouched.
            backup = self.path.with_name(self.path.name + '.corrupt-' + uuid.uuid4().hex + '.bak')
            atomic_write_bytes(backup, self.path.read_bytes())
            return None

    def get(self):
        with self._lock():
            data = self._read()
            if data is None:
                return {'active': False}
            return {**data, 'active': data['state'] in ('pending', 'dispatching')}

    def create(self, hhmm, mode, now=None):
        now = time.time() if now is None else now
        data = dict(schema_version=2, schedule_id=uuid.uuid4().hex, time=hhmm,
                    video_mode=mode, created_at=now, run_at=self.next_run(hhmm, now),
                    state='pending', last_fired_date='')
        with self._lock():
            current = self._read_for_edit()
            if current and current.get('state') == 'dispatching':
                raise RuntimeError('Lịch đang bàn giao batch; vui lòng kiểm tra trạng thái trước.')
            atomic_write_json(self.path, data)
        return {**data, 'active': True}

    def cancel(self):
        with self._lock():
            current = self._read_for_edit()
            if current and current.get('state') == 'dispatching':
                raise RuntimeError('Batch đang được bàn giao; không thể hủy lịch lúc này.')
            self.path.unlink(missing_ok=True)

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self._lock():
            data = self._read()
            if not data:
                return None
            if data['state'] == 'dispatching' and now - data['dispatch_at'] > 300:
                data['state'] = 'needs_review'
                atomic_write_json(self.path, data)
                return data
            if data['state'] != 'pending' or now < data['run_at']:
                return None
            if now - data['run_at'] > 1800:
                data['state'] = 'expired'
                atomic_write_json(self.path, data)
                return data
            data.update(state='dispatching', dispatch_at=now)
            atomic_write_json(self.path, data)
            return data

    def finish(self, schedule_id, outcome, batch_job_id=None):
        with self._lock():
            data = self._read()
            if not data or data['schedule_id'] != schedule_id or data['state'] != 'dispatching':
                return
            data['state'] = {'accepted': 'fired', 'busy': 'pending'}.get(outcome, 'needs_review')
            data['batch_job_id'] = batch_job_id
            atomic_write_json(self.path, data)


async def dispatch_due(store, start_batch, now=None):
    """One bounded tick; an uncertain handoff is never retried automatically."""
    import asyncio
    data = await asyncio.to_thread(store.claim, now)
    if not data:
        return None
    if data['state'] in ('expired', 'needs_review'):
        return data['state']
    outcome, job_id = 'unknown', None
    try:
        response = await start_batch(None, video_mode=data['video_mode'])
        if response.status_code == 202:
            outcome = 'accepted'
            job_id = json.loads(response.body).get('job_id')
        elif response.status_code == 409:
            outcome = 'busy'
    except Exception as exc:
        if getattr(exc, 'status_code', None) == 409:
            outcome = 'busy'
    finally:
        await asyncio.to_thread(store.finish, data['schedule_id'], outcome, job_id)
    return outcome
