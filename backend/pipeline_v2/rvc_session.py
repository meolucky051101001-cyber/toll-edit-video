"""Persistent GPU worker session for the RVC stage of a pipeline job."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import psutil

from .atomic_io import atomic_write_json
from .gpu_lock import InterProcessGPULock

PathLike = Union[str, os.PathLike]
logger = logging.getLogger(__name__)


class RVCSessionError(RuntimeError):
    pass


class RVCSession:
    """Retains a single GPU worker process across multiple RVC batches in a job."""

    def __init__(
        self,
        control_directory: PathLike,
        lock_path: PathLike,
        python_executable: Optional[str] = None,
        lock_timeout_seconds: float = 1800.0,
    ):
        self.control_directory = Path(control_directory)
        self.control_directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(lock_path)
        self.python_executable = python_executable or sys.executable
        self.lock = InterProcessGPULock(self.lock_path, timeout_seconds=lock_timeout_seconds)
        self.lock.acquire()
        self.lock_held = True

        self.temp = tempfile.TemporaryDirectory(
            prefix="v2-rvc-session-", dir=str(self.control_directory)
        )
        self.root = Path(self.temp.name)
        self.number = 0
        self.closed = False

        repository_root = Path(__file__).resolve().parents[2]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        existing_python_path = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = os.pathsep.join(
            item
            for item in (str(repository_root), existing_python_path)
            if item
        )

        creation_flags = 0
        if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation_flags = subprocess.CREATE_NO_WINDOW

        self.log_file = (self.root / "worker.log").open("wb")
        command = [
            self.python_executable,
            "-m",
            "backend.pipeline_v2.gpu_worker",
            "--session-dir",
            str(self.root),
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=str(repository_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log_file,
                stderr=self.log_file,
                creationflags=creation_flags,
            )
        except BaseException:
            self.log_file.close()
            self.temp.cleanup()
            if self.lock_held:
                try:
                    self.lock.release()
                except Exception:
                    pass
                self.lock_held = False
            raise

    def run(self, payload: Mapping[str, Any], timeout_seconds: float = 900.0) -> Dict[str, Any]:
        if self.closed:
            raise RVCSessionError("RVC session is closed")
        self.number += 1
        request_path = self.root / ("request-%06d.json" % self.number)
        response_path = self.root / ("response-%06d.json" % self.number)

        atomic_write_json(
            request_path,
            {"schema_version": 1, "stage": "rvc", "payload": dict(payload)},
        )

        deadline = time.monotonic() + timeout_seconds
        while not response_path.is_file():
            if self.process.poll() is not None:
                raise RVCSessionError(
                    "RVC session worker exited unexpectedly with code {}".format(
                        self.process.returncode
                    )
                )
            if time.monotonic() >= deadline:
                self.close()
                raise RVCSessionError("RVC session timed out after {}s".format(timeout_seconds))
            time.sleep(0.05)

        try:
            data = json.loads(response_path.read_text(encoding="utf-8"))
        finally:
            try:
                response_path.unlink()
            except OSError:
                pass

        if not data.get("success"):
            raise RVCSessionError(data.get("error", "RVC batch processing failed"))
        return data.get("result", {})

    def close(self) -> None:
        if self.closed:
            return
        children = []
        try:
            if self.process.pid:
                children = psutil.Process(self.process.pid).children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        if self.process.poll() is None:
            (self.root / "stop").touch()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                for child in reversed(children):
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                self.process.kill()
                self.process.wait(timeout=5)

        for child in reversed(children):
            try:
                if child.is_running():
                    child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        self.log_file.close()
        self.closed = True
        try:
            self.temp.cleanup()
        except OSError as exc:
            logger.warning("RVC session temp directory cleanup deferred: %s", exc)
        finally:
            if self.lock_held:
                try:
                    self.lock.release()
                except Exception:
                    pass
                self.lock_held = False

    def __enter__(self) -> "RVCSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
