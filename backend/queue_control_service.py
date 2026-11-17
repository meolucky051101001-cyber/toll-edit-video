"""Durable commands. Only the queue owner publishes queue membership."""
import hashlib
import json
import logging
import math
import re
import time
import uuid
from pathlib import Path
from pipeline_v2.atomic_io import atomic_write_json, atomic_write_bytes
from pipeline_v2.interprocess_lock import CrossProcessLock

logger = logging.getLogger(__name__)
COMMAND_KINDS = {'delete', 'clear_pending', 'reorder', 'hold', 'release_scheduled', 'cancel_schedule_hold', 'release_manual'}
TERMINAL_STATES = {'applied', 'already_absent', 'too_late', 'conflict', 'needs_review', 'expired'}

class QueueStateError(ValueError):
    pass

def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{1,120}', value))

def snapshot_path(workspace):
    legacy = Path(workspace) / 'telegram_queue.json'
    canonical = legacy.parent / 'bot_system' / legacy.name
    if canonical.exists() or (canonical.parent.is_dir() and not legacy.exists()):
        return canonical
    return legacy

def identity(job):
    if not isinstance(job, dict) or not valid_id(job.get('job_id')):
        raise QueueStateError('Job chưa có định danh đã được lưu; cần phục hồi queue trước.')
    return job['job_id']

def commands(snapshot):
    return Path(snapshot).parent / 'queue_commands'

def health_path(snapshot):
    return Path(snapshot).with_suffix('.health.json')

def state_lock(snapshot):
    return CrossProcessLock(str(snapshot) + '.owner.lock', timeout_seconds=5)

def empty_snapshot():
    return dict(schema_version=2, revision=0, items=[], active_jobs=[], active_job=None,
                operation_receipts={}, tombstones=[], recovery_state='ready')

def read_snapshot(snapshot):
    try:
        data = json.loads(Path(snapshot).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return empty_snapshot()
    except (OSError, ValueError) as exc:
        raise QueueStateError('Không đọc được snapshot queue; bản gốc được giữ nguyên.') from exc
    if not isinstance(data, dict) or not isinstance(data.get('items'), list):
        raise QueueStateError('Schema queue không hợp lệ.')
    data = dict(data)
    data.setdefault('active_jobs', [data['active_job']] if data.get('active_job') else [])
    if not isinstance(data['active_jobs'], list):
        raise QueueStateError('Active jobs không hợp lệ.')
    jobs = data['items'] + data['active_jobs']
    if any(not isinstance(j, dict) for j in jobs):
        raise QueueStateError('Job trong snapshot không hợp lệ.')
    if data.get('schema_version') == 2:
        ids = [identity(j) for j in jobs]
        if len(set(ids)) != len(ids) or type(data.get('revision')) is not int or data['revision'] < 0:
            raise QueueStateError('ID hoặc revision queue không hợp lệ.')
        if data.get('recovery_state') not in ('ready', 'needs_review', 'failed'):
            raise QueueStateError('Recovery state không hợp lệ.')
    elif data.get('schema_version') not in (None, 1):
        raise QueueStateError('Version queue chưa được hỗ trợ.')
    else:
        data['recovery_state'] = 'migration_required'
        data.setdefault('revision', 0)
    data['active_job'] = data['active_jobs'][0] if data['active_jobs'] else None
    data.setdefault('operation_receipts', {})
    data.setdefault('tombstones', [])
    if (not isinstance(data['operation_receipts'], dict) or not isinstance(data['tombstones'], list)
            or not all(valid_id(job_id) for job_id in data['tombstones'])
            or (data.get('owner') is not None and not isinstance(data['owner'], dict))):
        raise QueueStateError('Thông tin owner, receipt hoặc tombstone không hợp lệ.')
    return data

def migrate_snapshot(snapshot):
    """Owner-only, under state_lock. Never infer identity from a URL/name."""
    data = read_snapshot(snapshot)
    if data.get('schema_version') == 2:
        return data
    if Path(snapshot).exists():
        atomic_write_bytes(str(snapshot) + '.legacy-' + uuid.uuid4().hex + '.bak', Path(snapshot).read_bytes())
    seen = set()
    for job in data['items'] + data['active_jobs']:
        if not valid_id(job.get('job_id')) or job['job_id'] in seen:
            job['job_id'] = 'telegram_' + uuid.uuid4().hex
        seen.add(job['job_id'])
    data.update(schema_version=2, recovery_state='ready', revision=int(data['revision']) + 1)
    data['active_job'] = data['active_jobs'][0] if data['active_jobs'] else None
    atomic_write_json(snapshot, data)
    return data

def _payload_key(kind, targets, revision, payload):
    return hashlib.sha256(json.dumps([kind, targets, revision, payload], sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()

def submit_command(snapshot, kind, target_job_ids, expected_revision=None, payload=None, operation_id=None):
    if kind not in COMMAND_KINDS:
        raise ValueError('Loại lệnh queue không hợp lệ.')
    if not isinstance(target_job_ids, list) or not target_job_ids or any(not valid_id(x) for x in target_job_ids) or len(set(target_job_ids)) != len(target_job_ids):
        raise ValueError('Danh sách ID không hợp lệ.')
    operation_id = operation_id or uuid.uuid4().hex
    if not valid_id(operation_id):
        raise ValueError('Operation ID không hợp lệ.')
    payload = dict(payload or {})
    if kind == 'reorder' and 'ordered_job_ids' in payload:
        order = payload['ordered_job_ids']
        if not isinstance(order, list) or any(not valid_id(job_id) for job_id in order):
            raise ValueError('Thứ tự queue phải là danh sách ID hợp lệ.')
    key = _payload_key(kind, target_job_ids, expected_revision, payload)
    with state_lock(snapshot):
        existing = read_operation(snapshot, operation_id)
        if existing:
            if existing.get('payload_key') != key:
                raise RuntimeError('Operation ID đã dùng cho nội dung khác.')
            return existing
        data = read_snapshot(snapshot)
        if data['recovery_state'] != 'ready':
            raise QueueStateError('Queue chưa phục hồi xong.')
        if expected_revision is not None and data['revision'] != expected_revision:
            raise RuntimeError('Revision queue đã thay đổi; hãy tải lại.')
        known = {identity(j) for j in data['items'] + data['active_jobs']}
        if expected_revision is not None and not set(target_job_ids) <= known:
            raise RuntimeError('Job đã thay đổi; hãy tải lại.')
        op = dict(schema_version=2, operation_id=operation_id, kind=kind,
                  target_job_ids=target_job_ids, expected_revision=expected_revision,
                  payload=payload, payload_key=key, state='pending', created_at=time.time())
        atomic_write_json(commands(snapshot) / (operation_id + '.json'), op)
        return op

def read_operation(snapshot, operation_id):
    if not valid_id(operation_id):
        raise ValueError('Operation ID không hợp lệ.')
    path = commands(snapshot) / (operation_id + '.json')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict) or data.get('operation_id') != operation_id:
        raise QueueStateError('Operation record không hợp lệ.')
    # Snapshot receipt is authoritative if the owner crashed before command ACK.
    saved = read_snapshot(snapshot)
    receipt = saved.get('operation_receipts', {}).get(operation_id)
    return {**data, **receipt} if receipt else data

def read_operations(snapshot):
    result = []
    for path in commands(snapshot).glob('*.json'):
        try:
            op = json.loads(path.read_text(encoding='utf-8-sig'))
            if (not valid_id(op.get('operation_id')) or path.stem != op['operation_id']
                    or op.get('state') not in {'pending'} | TERMINAL_STATES):
                raise ValueError('Invalid operation')
            if op.get('schema_version') != 2:
                # Old index/URL/derived identity commands cannot be applied safely.
                op.update(state='needs_review', reason='legacy_command_requires_confirmation')
                atomic_write_json(path, op)
            else:
                targets, created, payload = op.get('target_job_ids'), op.get('created_at'), op.get('payload')
                if (op.get('kind') not in COMMAND_KINDS or not isinstance(targets, list) or not targets
                        or not all(valid_id(target) for target in targets) or len(set(targets)) != len(targets)
                        or type(created) not in (int, float) or not math.isfinite(created) or not isinstance(payload, dict)):
                    raise ValueError('Invalid command')
                order = payload.get('ordered_job_ids')
                if order is not None and (not isinstance(order, list) or not all(valid_id(item) for item in order)):
                    raise ValueError('Invalid order')
            result.append((path, op))
        except (OSError, ValueError, TypeError, AttributeError):
            logger.error('Invalid queue command retained: %s', path)
    def sort_key(item):
        created = item[1].get('created_at')
        if type(created) not in (int, float) or not math.isfinite(created):
            created = 0
        return created, item[1]['operation_id']
    return sorted(result, key=sort_key)

def apply_commands(queue, snapshot=None):
    if getattr(queue, 'recovery_pending', False):
        return
    queue.apply_commands()

def apply_deletions(queue, snapshot):
    return apply_commands(queue, snapshot)

def submit_delete(snapshot, item):
    return submit_command(snapshot, 'delete', [identity(item)])

def filter_restored(snapshot, items, active=None):
    """Compatibility helper; restoration itself uses owner commit + receipts."""
    active_id = identity(active) if active else None
    deleted = set()
    for path, op in read_operations(snapshot):
        if op.get('kind') not in ('delete', 'clear_pending'):
            continue
        targets = set(op.get('target_job_ids', []))
        if active_id in targets and op['state'] == 'pending':
            op.update(state='too_late')
            atomic_write_json(path, op)
        elif op['state'] in ('pending', 'applied', 'already_absent'):
            deleted.update(targets)
    return [job for job in items if identity(job) not in deleted]
