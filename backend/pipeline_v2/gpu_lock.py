"""Portable OS-backed inter-process lock for the single GPU worker."""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import IO, Optional, Union


PathLike = Union[str, os.PathLike]


class GPULockTimeout(TimeoutError):
    pass


class InterProcessGPULock:
    def __init__(
        self,
        path: PathLike,
        timeout_seconds: float = 1800.0,
        poll_seconds: float = 0.1,
    ):
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("GPU lock timeouts must be positive")
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> "InterProcessGPULock":
        if self._handle is not None:
            raise RuntimeError("GPU lock is already acquired by this object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        handle = self.path.open("a+", encoding="utf-8")
        while True:
            try:
                self._lock_handle(handle)
                self._handle = handle
                self._write_owner()
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise GPULockTimeout(
                        "Timed out waiting for GPU lock: {}".format(self.path)
                    )
                time.sleep(self.poll_seconds)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None

    def _write_owner(self) -> None:
        assert self._handle is not None
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at_unix": time.time(),
        }
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write(json.dumps(owner, sort_keys=True))
        self._handle.flush()
        os.fsync(self._handle.fileno())

    @staticmethod
    def _lock_handle(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_handle(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "InterProcessGPULock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
