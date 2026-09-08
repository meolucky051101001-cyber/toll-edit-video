"""Launch exactly one heavyweight GPU stage in a short-lived process."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from .atomic_io import atomic_write_json
from .gpu_lock import InterProcessGPULock


PathLike = Union[str, os.PathLike]


class GPUStageError(RuntimeError):
    pass


class GPUStageExecutor:
    def __init__(
        self,
        control_directory: PathLike,
        lock_path: PathLike,
        python_executable: Optional[str] = None,
        lock_timeout_seconds: float = 1800.0,
        stage_timeout_seconds: float = 3600.0,
    ):
        self.control_directory = Path(control_directory)
        self.lock_path = Path(lock_path)
        self.python_executable = python_executable or sys.executable
        self.lock_timeout_seconds = lock_timeout_seconds
        self.stage_timeout_seconds = stage_timeout_seconds

    def run(
        self,
        stage: str,
        payload: Mapping[str, Any],
        timeout_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.control_directory.mkdir(parents=True, exist_ok=True)
        repository_root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(
            prefix="gpu-{}-".format(stage), dir=str(self.control_directory)
        ) as temporary_directory:
            temp = Path(temporary_directory)
            request_path = temp / "request.json"
            response_path = temp / "response.json"
            atomic_write_json(
                request_path,
                {"schema_version": 1, "stage": stage, "payload": dict(payload)},
            )
            command = [
                self.python_executable,
                "-m",
                "backend.pipeline_v2.gpu_worker",
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ]
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
            with InterProcessGPULock(
                self.lock_path, timeout_seconds=self.lock_timeout_seconds
            ):
                try:
                    result = subprocess.run(
                        command,
                        cwd=str(repository_root),
                        env=environment,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout_seconds or self.stage_timeout_seconds,
                        creationflags=creation_flags,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise GPUStageError(
                        "GPU stage {!r} exceeded its timeout".format(stage)
                    ) from exc
            response: Dict[str, Any] = {}
            if response_path.is_file():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    response = {}
            if result.returncode != 0 or not response.get("success"):
                detail = response.get("error") or result.stderr[-2000:] or result.stdout[-2000:]
                raise GPUStageError(
                    "GPU stage {!r} failed: {}".format(stage, detail or "unknown error")
                )
            return dict(response.get("result", {}))
