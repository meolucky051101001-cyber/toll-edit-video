"""Single-owner durable queue. Persist before applying every membership change."""
import asyncio
import copy
import json
import logging
import multiprocessing
import os
import time
import uuid
from pathlib import Path
from pipeline_v2.atomic_io import atomic_write_json, atomic_write_bytes
from queue_control_service import (QueueStateError, identity, read_snapshot, migrate_snapshot,
    state_lock, read_operations, health_path, snapshot_path)


SNAPSHOT = snapshot_path(os.getenv('AUTODUB_WORKSPACE', str(Path(__file__).resolve().parent.parent / 'workspace')))
FIELDS = ('job_id', 'url', 'type', 'chat_id', 'file_id', 'filename', 'voice_mode', 'video_mode',
          'job_overrides', 'resume_state', 'retry_count', 'network_retry_count', 'hold', 'pos',
          'force_cloud', 'cloud_gpu', 'voice', 'video_path')

class MonitoredQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.snapshot = Path(SNAPSHOT)
        self.active_job = None
        self.active_jobs = []
        self.revision = 0
        self.receipts = {}
        self.tombstones = set()
        self.recovery_pending = self.snapshot.exists()
        self.recovery_state = 'pending' if self.recovery_pending else 'ready'
        self._ready = asyncio.Event()

    def _serialize(self, job, position, active=False):
        identity(job)
        item = {k: copy.deepcopy(job.get(k)) for k in FIELDS if k in job}
        update = job.get('update')
        if not item.get('chat_id') and getattr(update, 'message', None):
            item['chat_id'] = update.message.chat_id
        item.update(position=position, telegram_position=job.get('pos', position),
                    name=str(job.get('filename') or job.get('url') or 'Video tiếp tục xử lý'),
                    source='Telegram', in_progress=active)
        return item

    def _failed(self, exc):
        self.recovery_pending = True
        self.recovery_state = 'failed'
        try:
            atomic_write_json(health_path(self.snapshot), dict(recovery_state='failed', error=type(exc).__name__, at=time.time()))
        except OSError:
            pass

    def _assert_ready(self):
        if self.recovery_pending or self.recovery_state != 'ready':
            raise QueueStateError('Queue cần phục hồi/đối soát trước khi nhận hoặc claim job.')
        if multiprocessing.current_process().name != 'MainProcess':
            raise QueueStateError('Chỉ queue owner được phép thay đổi snapshot.')

    def _commit(self, jobs, active, *, receipt=None, tombstones=None, state='ready', force=False):
        """Caller owns state_lock; commit snapshot first, then memory, then ACK."""
        jobs, active = list(jobs), list(active)
        saved = read_snapshot(self.snapshot)
        if not force and saved.get('revision', 0) != self.revision:
            raise QueueStateError('Snapshot owner/revision đã đổi; cần phục hồi trước.')
        receipts = dict(self.receipts)
        if receipt:
            receipts[receipt['operation_id']] = receipt
        deleted = self.tombstones if tombstones is None else set(tombstones)
        serialized = [self._serialize(j, i + 1) for i, j in enumerate(jobs)]
        running = [self._serialize(j, i + 1, True) for i, j in enumerate(active)]
        data = dict(schema_version=2, revision=self.revision + 1, items=serialized,
                    active_jobs=running, active_job=running[0] if running else None,
                    operation_receipts=receipts, tombstones=sorted(deleted),
                    recovery_state=state, owner={'pid': os.getpid(), 'create_time': self._owner_created()},
                    updated_at=time.time(), pid=os.getpid())
        if self.snapshot.exists():
            atomic_write_bytes(str(self.snapshot) + '.last-good.bak', self.snapshot.read_bytes())
        atomic_write_json(self.snapshot, data)
        self._queue.clear()
        self._queue.extend(jobs)
        self.active_jobs = active
        self.active_job = active[0] if active else None
        self.receipts, self.tombstones = receipts, set(deleted)
        self.revision = data['revision']
        self.recovery_state = state
        self._unfinished_tasks = len(jobs) + len(active)
        if self._unfinished_tasks:
            self._finished.clear()
        else:
            self._finished.set()
        self._ready.set()
        self._wakeup_next(self._getters)
        return data

    @staticmethod
    def _owner_created():
        try:
            import psutil
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return None

    def restore(self, *, delivered=None):
        """All records validate before publication. Interrupted active jobs need review."""
        self.recovery_pending = True
        try:
            with state_lock(self.snapshot):
                data = migrate_snapshot(self.snapshot)
                from telegram_local_source import normalize_restored
                jobs = [normalize_restored(job) for job in data['items']]
                active = [job for job in data['active_jobs'] if delivered is None or not delivered(job)]
                self.revision = data['revision']
                self.receipts = data.get('operation_receipts', {})
                self.tombstones = set(data.get('tombstones', []))
                # Never re-render/re-deliver an interrupted active record without reconciliation.
                resolved_active = bool(data['active_jobs']) and not active and data['recovery_state'] == 'needs_review'
                state = 'ready' if not active and (data['recovery_state'] == 'ready' or resolved_active) else 'needs_review'
                self._commit(jobs, active, state=state, force=True)
                self.recovery_pending = state != 'ready'
                if not self.recovery_pending:
                    self._apply_locked()
                    health_path(self.snapshot).unlink(missing_ok=True)
                return dict(status='needs_review' if active else ('restored' if jobs else 'empty'),
                            count=len(jobs), active_count=len(active), recovery_state=state)
        except (OSError, ValueError, TypeError) as exc:
            try:
                if self.snapshot.exists():
                    atomic_write_bytes(str(self.snapshot) + '.corrupt-' + uuid.uuid4().hex + '.bak', self.snapshot.read_bytes())
            finally:
                self._failed(exc)
            raise QueueStateError('Restore queue thất bại; không ghi đè snapshot gốc.') from exc

    def publish(self):
        # Heartbeats do not change membership revision and cannot erase startup state.
        if self.recovery_pending:
            return
        self._assert_ready()
        try:
            with state_lock(self.snapshot):
                saved = read_snapshot(self.snapshot)
                if saved['revision'] != self.revision:
                    raise QueueStateError('Snapshot revision đã đổi.')
                if self.active_jobs:
                    active = [self._serialize(job, pos, True) for pos, job in enumerate(self.active_jobs, 1)]
                    saved.update(active_jobs=active, active_job=active[0])
                saved.update(updated_at=time.time(), pid=os.getpid())
                atomic_write_json(self.snapshot, saved)
        except (OSError, ValueError) as exc:
            self._failed(exc)
            raise

    def put_nowait(self, item):
        self._assert_ready()
        if not isinstance(item, dict):
            raise ValueError('Queue yêu cầu job record có ID.')
        if not item.get('job_id'):
            item['job_id'] = 'telegram_' + uuid.uuid4().hex
        job_id = identity(item)
        try:
            with state_lock(self.snapshot):
                self._apply_locked()
                if job_id in self.tombstones:
                    raise ValueError('Job đã bị xóa; không thể tự đưa vào lại.')
                if any(identity(j) == job_id for j in list(self._queue) + self.active_jobs):
                    raise ValueError('Job ID đã tồn tại trong queue.')
                self._commit(list(self._queue) + [item], self.active_jobs)
        except (OSError, QueueStateError) as exc:
            self._failed(exc)
            raise

    def _apply_locked(self):
        for path, operation in read_operations(self.snapshot):
            oid = operation['operation_id']
            if oid in self.receipts:
                if operation['state'] == 'pending':
                    atomic_write_json(path, {**operation, **self.receipts[oid]})
                continue
            if operation['state'] != 'pending':
                continue
            jobs, active = list(self._queue), self.active_jobs
            targets = set(operation['target_job_ids'])
            kind, payload = operation['kind'], operation.get('payload') or {}
            by_id = {identity(j): j for j in jobs}
            active_ids = {identity(j) for j in active}
            statuses = {jid: ('too_late' if jid in active_ids else 'already_absent') for jid in targets}
            deleted = set(self.tombstones)
            state = 'applied'
            expected = operation.get('expected_revision')
            # Destructive deletion remains safe by exact ID even after new arrivals.
            if kind not in ('delete', 'clear_pending', 'cancel_schedule_hold', 'release_scheduled') and expected is not None and expected != self.revision:
                state = 'conflict'
            elif kind in ('delete', 'clear_pending'):
                for jid in targets & by_id.keys():
                    statuses[jid] = 'removed'
                deleted.update(targets - active_ids)
                jobs = [j for j in jobs if identity(j) not in targets]
                if not any(s == 'removed' for s in statuses.values()):
                    state = 'too_late' if 'too_late' in statuses.values() else 'already_absent'
            elif kind == 'reorder':
                order = payload.get('ordered_job_ids')
                if order is not None:
                    if len(order) != len(by_id) or set(order) != set(by_id):
                        state = 'conflict'
                    else:
                        jobs = [by_id[jid] for jid in order]
                elif len(targets) == 1 and targets <= by_id.keys():
                    job = by_id[next(iter(targets))]
                    index = jobs.index(job)
                    action = payload.get('action')
                    target = 0 if action == 'top' else (index - 1 if action == 'up' else index + 1 if action == 'down' else payload.get('to'))
                    if not isinstance(target, int):
                        state = 'conflict'
                    else:
                        jobs.pop(index)
                        jobs.insert(max(0, min(target, len(jobs))), job)
                else:
                    state = 'conflict'
            elif kind in ('hold', 'release_scheduled', 'cancel_schedule_hold', 'release_manual'):
                if kind == 'release_scheduled':
                    from service_control import service_paused
                    if service_paused(Path(__file__).resolve().parent.parent):
                        continue
                if not targets <= by_id.keys():
                    state = 'too_late' if targets & active_ids else 'conflict'
                else:
                    jobs = [dict(j) for j in jobs]
                    for job in jobs:
                        if identity(job) not in targets:
                            continue
                        hold = job.get('hold') or {}
                        from telegram_local_source import SOURCE_HOLDS
                        if hold.get('reason_code') in SOURCE_HOLDS:
                            state = 'conflict'; break
                        binding = {k: payload.get(k) for k in ('schedule_id', 'generation')}
                        if kind == 'hold':
                            if hold.get('kind') == 'scheduled' and any(hold.get(k) != v for k, v in binding.items()):
                                state = 'conflict'; break
                            job['hold'] = dict(binding, kind='scheduled', run_at=payload.get('run_at'),
                                               expires_at=payload.get('expires_at'))
                            statuses[identity(job)] = 'held'
                        elif kind in ('release_scheduled', 'cancel_schedule_hold'):
                            if not hold or any(hold.get(k) != v for k, v in binding.items()):
                                state = 'conflict'; break
                            if kind == 'release_scheduled':
                                run_at = hold.get('run_at')
                                expires_at = payload.get('expires_at')
                                if (hold.get('kind') != 'scheduled' or type(run_at) not in (float, int)
                                        or payload.get('not_before') != run_at or type(expires_at) not in (float, int)
                                        or expires_at != hold.get('expires_at') or time.time() < run_at):
                                    state = 'conflict'; break
                                if time.time() > expires_at:
                                    job['hold'] = dict(binding, kind='manual')
                                    statuses[identity(job)] = 'expired'
                                    state = 'expired'
                                    continue
                                job.pop('hold', None)
                                statuses[identity(job)] = 'released'
                            else:
                                job['hold'] = dict(binding, kind='manual')
                                statuses[identity(job)] = 'manual_hold'
                        else:
                            expires_at = hold.get('expires_at')
                            if hold.get('kind') == 'scheduled' and (
                                type(expires_at) not in (float, int) or time.time() <= expires_at
                            ):
                                state = 'conflict'; break
                            job.pop('hold', None)
                            statuses[identity(job)] = 'released'
            if state in ('conflict', 'too_late'):
                jobs = list(self._queue)
                statuses.update({job_id: 'conflict' for job_id in targets & by_id.keys()})
            receipt = dict(operation_id=oid, state=state, result=statuses, applied_at=time.time(), revision=self.revision + 1)
            self._commit(jobs, active, receipt=receipt, tombstones=deleted)
            atomic_write_json(path, {**operation, **receipt})

    def apply_commands(self):
        if self.recovery_pending:
            return
        self._assert_ready()
        try:
            with state_lock(self.snapshot):
                self._apply_locked()
        except (OSError, ValueError) as exc:
            self._failed(exc)
            raise

    def get_nowait(self, *, admit=None):
        self._assert_ready()
        from service_control import service_paused
        if service_paused(Path(__file__).resolve().parent.parent):
            raise asyncio.QueueEmpty
        try:
            with state_lock(self.snapshot):
                self._apply_locked()
                item = next((j for j in self._queue if not j.get('hold')), None)
                if item is None:
                    raise asyncio.QueueEmpty
                if admit is not None and not admit():
                    raise asyncio.QueueEmpty
                remaining = [j for j in self._queue if j is not item]
                self._commit(remaining, self.active_jobs + [item])
                return item
        except (OSError, QueueStateError) as exc:
            self._failed(exc)
            raise

    async def get(self, *, admit=None):
        while True:
            self._ready.clear()
            if not self.recovery_pending:
                try:
                    return self.get_nowait(admit=admit)
                except asyncio.QueueEmpty:
                    pass
            try:
                await asyncio.wait_for(self._ready.wait(), 1)
            except asyncio.TimeoutError:
                pass

    def task_done(self, job_id=None, *, retry=None):
        self._assert_ready()
        try:
            with state_lock(self.snapshot):
                if not self.active_jobs:
                    raise ValueError('task_done() called too many times')
                chosen = job_id or identity(self.active_jobs[0])
                if chosen not in {identity(j) for j in self.active_jobs}:
                    raise ValueError('Unknown active job')
                jobs = list(self._queue)
                if retry is not None:
                    if identity(retry) != chosen:
                        raise ValueError('Retry must retain the active job ID')
                    jobs.append(retry)
                self._commit(jobs, [j for j in self.active_jobs if identity(j) != chosen])
        except (OSError, QueueStateError) as exc:
            self._failed(exc)
            raise

    def retain_for_review(self, job_id, reason):
        """Keep an uncertain active outcome durable; it must not become a retry."""
        self._assert_ready()
        with state_lock(self.snapshot):
            active = [dict(job) for job in self.active_jobs]
            for job in active:
                if identity(job) == job_id:
                    job['resume_state'] = dict(job.get('resume_state') or {}, recovery_reason=reason)
            self._commit(list(self._queue), active, state='needs_review')
            self.recovery_pending = True

    def remove_pending(self, target):
        self._assert_ready()
        with state_lock(self.snapshot):
            jobs = [j for j in self._queue if identity(j) != target]
            removed = len(self._queue) - len(jobs)
            self._commit(jobs, self.active_jobs)
            return removed

    def clear(self):
        self._assert_ready()
        with state_lock(self.snapshot):
            self._commit([], self.active_jobs, tombstones=self.tombstones | {identity(j) for j in self._queue})
