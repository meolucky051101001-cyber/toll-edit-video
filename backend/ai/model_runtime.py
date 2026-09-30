"""Client for the isolated strong-model runtime used by Pipeline v2."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .model_policy import RuntimeModelPolicy, current_model_policy


class ModelRuntimeError(RuntimeError):
    pass


def _creation_flags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _worker_path() -> Path:
    return Path(__file__).resolve().parents[1] / "model_workers" / "model_runtime_worker.py"


def runtime_module_available(
    module_name: str,
    policy: Optional[RuntimeModelPolicy] = None,
    timeout_seconds: float = 20.0,
) -> bool:
    selected = policy or current_model_policy()
    python = selected.runtime_python_path()
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [
                str(python),
                "-c",
                "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 2)",
                module_name,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=_creation_flags(),
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def run_model_stage(
    stage: str,
    payload: Mapping[str, Any],
    timeout_seconds: float,
    policy: Optional[RuntimeModelPolicy] = None,
) -> Dict[str, Any]:
    selected = policy or current_model_policy()
    python = selected.runtime_python_path()
    worker = _worker_path()
    if not python.is_file():
        raise ModelRuntimeError("Model runtime Python is unavailable: {}".format(python))
    if not worker.is_file():
        raise ModelRuntimeError("Model runtime worker is unavailable: {}".format(worker))

    cache_root = Path(selected.model_cache_directory).resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "HF_HOME": str(cache_root / "huggingface"),
            "MODELSCOPE_CACHE": str(cache_root / "modelscope"),
            "PADDLE_PDX_CACHE_HOME": str(cache_root / "paddlex"),
        }
    )
    with tempfile.TemporaryDirectory(prefix="autodub-model-runtime-") as temporary:
        root = Path(temporary)
        request_path = root / "request.json"
        response_path = root / "response.json"
        request_path.write_text(
            json.dumps(
                {"schema_version": 1, "stage": stage, "payload": dict(payload)},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [
                    str(python),
                    str(worker),
                    "--request",
                    str(request_path),
                    "--response",
                    str(response_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=float(timeout_seconds),
                env=environment,
                creationflags=_creation_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            raise ModelRuntimeError(
                "Strong-model stage {!r} exceeded {:.0f}s".format(
                    stage, timeout_seconds
                )
            ) from exc
        response: Dict[str, Any] = {}
        if response_path.is_file():
            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                response = {}
        if result.returncode != 0 or not response.get("success"):
            detail = (
                response.get("error")
                or result.stderr[-3000:]
                or result.stdout[-3000:]
                or "unknown model runtime error"
            )
            raise ModelRuntimeError("{} failed: {}".format(stage, detail))
        return dict(response.get("result", {}))
