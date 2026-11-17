"""Reentrant local coordinator: lifecycle -> schedule -> plan transaction.

Only synchronous control operations enter this guard. Never hold it across an
await, media processing, or network request.
"""
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from pipeline_v2.interprocess_lock import CrossProcessLock

_locks = {}
_registry_guard = threading.Lock()
_held = threading.local()


@contextmanager
def lifecycle_lock(control):
    path = Path(control) / 'batch_lifecycle.lock'
    key = os.path.normcase(str(path.resolve()))
    with _registry_guard:
        thread_lock = _locks.setdefault(key, threading.RLock())
    with thread_lock:
        held = getattr(_held, 'keys', set())
        if key in held:
            yield
            return
        with CrossProcessLock(path, name='BatchLifecycle', timeout_seconds=10, poll_interval=.01):
            _held.keys = held | {key}
            try:
                yield
            finally:
                _held.keys = held
