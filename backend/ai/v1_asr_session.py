"""Persistent V1 ASR worker session."""
from __future__ import annotations

import atexit
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .v1_separator_session import _creation_flags, _drain_stderr, _read_worker_response

logger = logging.getLogger("v1_asr_session")

_lock = threading.RLock()
_process: subprocess.Popen | None = None
_key: tuple | None = None
_last_used_at: float = 0.0
_stderr_thread: threading.Thread | None = None
_idle_timer: threading.Timer | None = None


def close_session() -> None:
    """Terminate the persistent ASR worker and reap its process tree."""
    global _process, _key, _last_used_at, _stderr_thread, _idle_timer
    with _lock:
        process = _process
        stderr_thread = _stderr_thread
        idle_timer = _idle_timer
        _process = None
        _key = None
        _last_used_at = 0.0
        _stderr_thread = None
        _idle_timer = None

    if idle_timer is not None:
        idle_timer.cancel()

    if process is None:
        return

    if getattr(process, "stdin", None) is not None:
        try:
            process.stdin.close()
        except (OSError, ValueError) as exc:
            logger.debug("Could not close ASR worker stdin: %s", exc)

    pid = getattr(process, "pid", None)
    try:
        if process.poll() is None:
            process.terminate()
            if os.name == "nt" and isinstance(pid, int) and pid > 0:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=_creation_flags(),
                        check=False,
                        timeout=5,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    logger.debug("Could not terminate ASR worker process tree: %s", exc)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=1.0)


def _schedule_idle_close(idle_s: float) -> None:
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(idle_s, close_if_idle, args=(idle_s,))
    _idle_timer.daemon = True
    _idle_timer.start()


def close_if_idle(max_idle_s: float = 300.0) -> bool:
    """Close the ASR worker when it has been idle for at least ``max_idle_s``."""
    with _lock:
        if _process is None:
            return False
        if _last_used_at > 0 and time.monotonic() - _last_used_at < float(max_idle_s):
            return False
    close_session()
    return True


def _below_ram_threshold() -> bool:
    try:
        min_ram_mb = int(os.getenv("V1_ASR_KEEP_WARM_MIN_RAM_MB", "2000"))
    except (TypeError, ValueError):
        min_ram_mb = 2000
    try:
        import psutil
        available = psutil.virtual_memory().available
        if available < min_ram_mb * 1024 * 1024:
            logger.info(
                "[ASR_WORKER] RAM khả dụng %.0f MB < ngưỡng %d MB; đóng worker giữ ấm.",
                available / (1024.0 * 1024.0),
                min_ram_mb,
            )
            return True
    except (ImportError, OSError, AttributeError, RuntimeError) as exc:
        logger.debug("Could not inspect memory for ASR session eviction: %s", exc)
    return False


def run_asr_request(
    python_exe: str | Path,
    worker_script: str | Path,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    creationflags: int = 0,
    check_stop: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Send one ASR request to the warm worker, reusing its process when possible."""
    global _process, _key, _last_used_at, _stderr_thread

    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ASR timeout must be finite and positive")

    worker_env = dict(env if env is not None else os.environ)
    worker_env["V1_ASR_REUSE"] = "1"
    worker_env["PYTHONIOENCODING"] = "utf-8"
    worker_env["PYTHONUTF8"] = "1"
    worker_env["PYTHONUNBUFFERED"] = "1"

    excluded_env = {"V1_ASR_JOB_OPTIONS", "V1_ASR_REUSE"}
    asr_env = tuple(sorted(
        (name, str(value))
        for name, value in worker_env.items()
        if name.startswith(("V1_WHISPER_", "V1_ASR_")) and name not in excluded_env
    ))
    py_str = str(python_exe)
    worker_str = str(worker_script)
    key = (py_str, worker_str, asr_env)
    deadline = time.monotonic() + timeout

    def check_budget() -> float:
        if check_stop is not None:
            check_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"ASR warm worker timed out after {timeout:.1f}s")
        return remaining

    with _lock:
        check_budget()
        with tempfile.TemporaryDirectory(prefix="autodub-v1-asr-") as temp_dir:
            root = Path(temp_dir)
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text(
                json.dumps(dict(payload), ensure_ascii=False),
                encoding="utf-8",
            )

            try:
                if _process is None or _process.poll() is not None or _key != key:
                    close_session()
                    check_budget()
                    _process = subprocess.Popen(
                        [py_str, worker_str, "--serve"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=worker_env,
                        cwd=str(cwd) if cwd is not None else None,
                        creationflags=_creation_flags(creationflags),
                    )
                    _key = key
                    _stderr_thread = threading.Thread(
                        target=_drain_stderr,
                        args=(_process.stderr,),
                        daemon=True,
                    )
                    _stderr_thread.start()

                check_budget()
                _process.stdin.write(
                    json.dumps({
                        "request": str(request_path),
                        "response": str(response_path),
                    }) + "\n"
                )
                _process.stdin.flush()

                while True:
                    remaining = check_budget()
                    if response_path.is_file():
                        result = _read_worker_response(response_path)
                        _last_used_at = time.monotonic()
                        if not result:
                            raise RuntimeError("ASR warm worker produced an empty response")
                        try:
                            idle_s = float(worker_env.get("V1_ASR_KEEP_WARM_IDLE_S", "300"))
                            if not math.isfinite(idle_s) or idle_s < 0:
                                raise ValueError
                        except (TypeError, ValueError):
                            idle_s = 300.0
                        _schedule_idle_close(idle_s)
                        return result
                    if _process.poll() is not None:
                        raise RuntimeError(
                            f"ASR warm worker exited unexpectedly (code={_process.returncode})"
                        )
                    time.sleep(min(0.025, remaining))
            except BaseException:
                close_session()
                raise
            finally:
                if _process is not None and _below_ram_threshold():
                    close_session()


atexit.register(close_session)
