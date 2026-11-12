"""Immutable delete requests survive queue snapshot publication and bot restarts."""
import hashlib
import json
import uuid
import logging
from pathlib import Path
from pipeline_v2.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)
_reported_errors = {}


def identity(job):
    if job.get('job_id'):
        return job['job_id']
    values = [str(job.get(k) or '') for k in ('url', 'file_id', 'filename', 'chat_id')]
    return 'legacy_' + hashlib.sha256(json.dumps(values).encode()).hexdigest()[:32]


def commands(snapshot):
    return Path(snapshot).parent / 'queue_commands'


def submit_delete(snapshot, item):
    operation = dict(operation_id=uuid.uuid4().hex, job_id=identity(item), state='pending')
    atomic_write_json(commands(snapshot) / (operation['operation_id'] + '.json'), operation)
    return operation


def read_operations(snapshot):
    directory = commands(snapshot)
    operations = []
    for path in directory.glob('*.json'):
        try:
            op = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(op, dict) or not isinstance(op.get('job_id'), str)
                    or not op['job_id'] or op.get('state') not in
                    ('pending', 'applied', 'already_absent', 'too_late')
                    or not isinstance(op.get('operation_id'), str)):
                raise ValueError('Invalid operation schema')
            operations.append((path, op))
            _reported_errors.pop(str(path), None)
        except (OSError, ValueError, TypeError) as exc:
            # Retain the original record for diagnosis; one bad record cannot
            # terminate the consumer or prevent other valid commands applying.
            try:
                signature = path.stat().st_mtime_ns
            except OSError:
                signature = None
            key = str(path)
            if key not in _reported_errors or _reported_errors[key] != signature:
                logger.error('Invalid queue operation retained at %s (%s)', path, type(exc).__name__)
                _reported_errors[key] = signature
    return operations


def filter_restored(snapshot, items, active=None):
    if active:
        for path, op in read_operations(snapshot):
            if op['state'] == 'pending' and op['job_id'] == identity(active):
                op['state'] = 'too_late'
                atomic_write_json(path, op)
    deleted = {op['job_id'] for _, op in read_operations(snapshot)
               if op['state'] in ('pending', 'applied', 'already_absent')}
    return [item for item in items if identity(item) not in deleted]


def apply_deletions(queue, snapshot):
    if getattr(queue, 'recovery_pending', False):
        return
    for path, op in read_operations(snapshot):
        if op['state'] != 'pending':
            continue
        target = op['job_id']
        if queue.active_job and identity(queue.active_job) == target:
            op['state'] = 'too_late'
        else:
            removed = queue.remove_pending(target)
            if not removed:
                # During startup, an empty in-memory queue is not authoritative.
                # Leave the command pending until snapshot recovery has run.
                try:
                    saved = json.loads(Path(snapshot).read_text(encoding='utf-8-sig'))
                    jobs = list(saved.get('items') or []) + [saved.get('active_job') or {}]
                    if any(identity(job) == target for job in jobs):
                        continue
                except FileNotFoundError:
                    pass
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            # Publication precedes acknowledgement, so replay after a crash is safe.
            if removed:
                queue.publish()
            op['state'] = 'applied' if removed else 'already_absent'
        atomic_write_json(path, op)
