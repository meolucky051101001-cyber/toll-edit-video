"""Cross-process coordination and robust file locking for AutoDub Tool V2.

Provides OS-level locks to coordinate between Telegram bot, FastAPI backend,
batch processor, and offline verification, preventing concurrent execution
collisions, output overwrites, and Windows sharing violations (WinError 5/32).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import IO, Optional, Union

logger = logging.getLogger(__name__)

PathLike = Union[str, os.PathLike]


class PipelineLockTimeout(TimeoutError):
    """Raised when a process fails to acquire the global pipeline or job lock."""


class CrossProcessLock:
    """Portable OS-backed cross-process file lock using msvcrt (Windows) or fcntl (Unix)."""

    def __init__(
        self,
        lock_path: PathLike,
        name: str = "PipelineLock",
        timeout_seconds: float = 7200.0,
        poll_interval: float = 0.5,
    ):
        self.lock_path = Path(lock_path)
        self.name = name
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self._handle: Optional[IO[str]] = None
        self._metadata: dict = {}

    def acquire(self, metadata: Optional[dict] = None) -> "CrossProcessLock":
        if self._handle is not None:
            raise RuntimeError(f"{self.name} is already acquired by this instance")

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        handle = self.lock_path.open("a+", encoding="utf-8")

        while True:
            try:
                self._lock_handle(handle)
                self._handle = handle
                self._write_owner(metadata or {})
                logger.info("Acquired %s at %s (pid=%d)", self.name, self.lock_path, os.getpid())
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    handle.close()
                    raise PipelineLockTimeout(
                        f"Timed out after {self.timeout_seconds}s waiting for {self.name}: {self.lock_path}"
                    )
                time.sleep(self.poll_interval)

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            self._unlock_handle(handle)
            logger.info("Released %s at %s (pid=%d)", self.name, self.lock_path, os.getpid())
        except Exception as exc:
            logger.warning("Error releasing %s: %s", self.name, exc)
        finally:
            try:
                handle.close()
            except Exception:
                pass
            self._handle = None

    def _write_owner(self, extra_meta: dict) -> None:
        assert self._handle is not None
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at_unix": time.time(),
            "name": self.name,
            **extra_meta,
        }
        try:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.write(json.dumps(owner, sort_keys=True, indent=2))
            self._handle.flush()
            if hasattr(os, "fsync"):
                os.fsync(self._handle.fileno())
        except Exception as exc:
            logger.warning("Could not write owner metadata to lock file %s: %s", self.lock_path, exc)

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

    def __enter__(self) -> "CrossProcessLock":
        return self.acquire()

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()

    async def __aenter__(self) -> "CrossProcessLock":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.acquire)

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.release)


def get_global_pipeline_lock(workspace_dir: Optional[PathLike] = None) -> CrossProcessLock:
    """Get the machine-wide cross-process pipeline execution lock."""
    if workspace_dir:
        base = Path(workspace_dir)
    else:
        try:
            from backend.config.paths import AppPaths
        except ImportError:
            from config.paths import AppPaths
        project_root = Path(__file__).resolve().parents[2]
        base = AppPaths.from_environment(project_root).workspace
    lock_file = base / ".pipeline_v2_global.lock"
    return CrossProcessLock(lock_file, name="GlobalPipelineLock", timeout_seconds=7200.0)


def get_job_lock(job_directory: PathLike, job_id: str = "") -> CrossProcessLock:
    """Get the job-specific cross-process lock."""
    lock_file = Path(job_directory) / ".job_execution.lock"
    return CrossProcessLock(lock_file, name=f"JobLock[{job_id}]", timeout_seconds=3600.0)
