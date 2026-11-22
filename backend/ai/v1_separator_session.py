"""Persistent V1 BS-RoFormer worker session with CPU-offload between jobs."""
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
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger("v1_separator_session")

_lock = threading.RLock()
_process: Optional[subprocess.Popen] = None
_key: Optional[tuple] = None
_last_used_at: float = 0.0
_stderr_thread: Optional[threading.Thread] = None
_idle_timer: Optional[threading.Timer] = None


def _creation_flags(creationflags: int = 0) -> int:
    if creationflags:
        return creationflags
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _drain_stderr(pipe: Any) -> None:
    if pipe is None:
        return
    try:
        for line in iter(pipe.readline, ""):
            text = str(line).strip()
            if text and any(
                marker in text.lower()
                for marker in (
                    "gpu:",
                    "nguồn:",
                    "đã nạp model",
                    "khôi phục model",
                    "bắt đầu suy luận",
                    "tách âm hoàn tất",
                    "error",
                    "warning",
                )
            ):
                logger.info("[SEPARATOR_WORKER] %s", text[-1000:])
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _read_worker_response(response_path: Path) -> Dict[str, Any]:
    """Read worker JSON response with short retries for Windows os.replace lock races."""
    for attempt in range(10):
        if not response_path.is_file():
            if attempt < 9:
                time.sleep(0.025)
            continue
        try:
            raw = response_path.read_text(encoding="utf-8")
            if raw.strip():
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
        except (OSError, json.JSONDecodeError):
            pass
        if attempt < 9:
            time.sleep(0.025)
    return {}


def close_session() -> None:
    """Terminate the persistent BS-RoFormer worker and reap its process tree."""
    global _process, _key, _last_used_at, _stderr_thread, _idle_timer
    with _lock:
        process = _process
        stderr_t = _stderr_thread
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
        except Exception:
            pass

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
                        timeout=5,
                    )
                except Exception:
                    pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    finally:
        if stderr_t is not None and stderr_t.is_alive():
            stderr_t.join(timeout=1.0)


def close_if_idle(max_idle_s: float = 300.0) -> bool:
    """Close persistent separator worker if idle for at least ``max_idle_s``."""
    with _lock:
        if _process is None:
            return False
        if _last_used_at > 0 and (time.monotonic() - _last_used_at) < float(max_idle_s):
            return False
    close_session()
    return True


def _close_if_idle_after_delay(idle_s: float) -> None:
    if close_if_idle(idle_s):
        return
    with _lock:
        if _process is None or threading.current_thread() is not _idle_timer:
            return
        remaining = idle_s - (time.monotonic() - _last_used_at)
        _schedule_idle_close(max(remaining, 0.001))


def _schedule_idle_close(idle_s: float) -> None:
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(idle_s, _close_if_idle_after_delay, args=(idle_s,))
    _idle_timer.daemon = True
    _idle_timer.start()


def _idle_seconds() -> float:
    try:
        idle_s = float(os.getenv("V1_SEPARATOR_KEEP_WARM_IDLE_S", "300"))
    except ValueError:
        return 300.0
    return idle_s if math.isfinite(idle_s) and idle_s >= 0 else 300.0


def _below_ram_threshold() -> bool:
    min_ram_mb = int(os.getenv("V1_SEPARATOR_KEEP_WARM_MIN_RAM_MB", "2000"))
    try:
        import psutil
        avail_bytes = psutil.virtual_memory().available
        if avail_bytes < min_ram_mb * 1024 * 1024:
            logger.info(
                "[SEPARATOR_WORKER] RAM khả dụng %.0f MB < ngưỡng giữ ấm %d MB.",
                avail_bytes / (1024.0 * 1024.0),
                min_ram_mb,
            )
            return True
    except Exception:
        pass
    return False


def run_separator_request(
    python_exe: str | Path,
    worker_script: str | Path,
    payload: Mapping[str, Any],
    *,
    timeout: float,
    check_stop: Optional[Callable[[], None]] = None,
    env: Optional[Mapping[str, str]] = None,
    creationflags: int = 0,
) -> Dict[str, Any]:
    """Send one separation job to the warm `--serve` BS-RoFormer worker."""
    global _process, _key, _last_used_at, _stderr_thread

    if check_stop is not None:
        check_stop()

    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Separator timeout must be finite and positive")

    worker_env = dict(env if env is not None else os.environ)
    worker_env["PYTHONIOENCODING"] = "utf-8"
    worker_env["PYTHONUTF8"] = "1"
    worker_env["PYTHONUNBUFFERED"] = "1"

    py_str = str(python_exe)
    script_str = str(worker_script)
    key = (py_str, script_str)
    deadline = time.monotonic() + timeout

    def _check_budget() -> float:
        if check_stop is not None:
            check_stop()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"Tiến trình tách âm timeout sau {timeout:.1f}s.")
        return remaining

    with _lock:
        _check_budget()
        with tempfile.TemporaryDirectory(prefix="autodub-v1-sep-") as tmpdir:
            root = Path(tmpdir)
            request_path = root / "request.json"
            response_path = root / "response.json"
            request_path.write_text(json.dumps(dict(payload), ensure_ascii=False), encoding="utf-8")

            try:
                if _process is None or _process.poll() is not None or _key != key:
                    close_session()
                    _check_budget()
                    _process = subprocess.Popen(
                        [py_str, script_str, "--serve"],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        env=worker_env,
                        creationflags=_creation_flags(creationflags),
                    )
                    _key = key
                    _stderr_thread = threading.Thread(
                        target=_drain_stderr,
                        args=(_process.stderr,),
                        daemon=True,
                    )
                    _stderr_thread.start()

                _check_budget()
                _process.stdin.write(
                    json.dumps({"request": str(request_path), "response": str(response_path)}) + "\n"
                )
                _process.stdin.flush()

                while True:
                    remaining = _check_budget()
                    if response_path.is_file():
                        result = _read_worker_response(response_path)
                        _last_used_at = time.monotonic()
                        if not result:
                            raise RuntimeError("Separator warm worker produced an empty response")
                        _schedule_idle_close(_idle_seconds())
                        return result
                    if _process.poll() is not None:
                        raise RuntimeError(
                            f"V1 separator worker exited unexpectedly (code={_process.returncode})"
                        )
                    time.sleep(min(0.025, remaining))
            except BaseException:
                close_session()
                raise
            finally:
                if _process is not None and _below_ram_threshold():
                    # The previous video's idle OCR process can still own two
                    # model sessions. Release it before dropping reusable weights.
                    from . import v1_ocr_session
                    if not v1_ocr_session.close_if_idle(0) or _below_ram_threshold():
                        close_session()


atexit.register(close_session)
