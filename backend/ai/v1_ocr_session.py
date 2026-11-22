"""One isolated OCR worker per V1 process, released after each video."""
import atexit
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path

_lock = threading.RLock()
_process = None
_key = None
_last_used_at = 0.0
_idle_timer = None

def close_session():
    global _process, _key, _last_used_at, _idle_timer
    with _lock:
        process, _process, _key = _process, None, None
        _last_used_at = 0.0
        idle_timer, _idle_timer = _idle_timer, None
        if idle_timer is not None:
            idle_timer.cancel()
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except Exception:
                    pass
            pid = getattr(process, "pid", None)
            if process.poll() is None:
                process.terminate()
                if os.name == "nt" and isinstance(pid, int) and pid > 0:
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            timeout=5,
                        )
                    except Exception:
                        pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

def close_if_idle(max_idle_s: float = 180.0) -> bool:
    """Close warm OCR worker if it has been idle for at least ``max_idle_s``."""
    if not _lock.acquire(blocking=False):
        return False
    try:
        if _process is None:
            return False
        if _last_used_at > 0 and (time.monotonic() - _last_used_at) < float(max_idle_s):
            return False
        close_session()
        return True
    finally:
        _lock.release()


def _close_if_idle_after_delay(idle_s):
    if close_if_idle(idle_s):
        return
    with _lock:
        if _process is None or threading.current_thread() is not _idle_timer:
            return
        remaining = idle_s - (time.monotonic() - _last_used_at)
        _schedule_idle_close(max(remaining, 0.001))


def _schedule_idle_close(idle_s):
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
    _idle_timer = threading.Timer(idle_s, _close_if_idle_after_delay, args=(idle_s,))
    _idle_timer.daemon = True
    _idle_timer.start()


def _idle_seconds():
    try:
        idle_s = float(os.getenv("V1_OCR_KEEP_WARM_IDLE_S", "180"))
    except ValueError:
        return 180.0
    return idle_s if math.isfinite(idle_s) and idle_s >= 0 else 180.0


def run_request(args, *, timeout, env, creationflags=0, **kwargs):
    global _process, _key, _last_used_at
    from batch_control import stop_check
    predicate = stop_check.get()
    if predicate and predicate():
        raise RuntimeError("OCR stop requested")
    request = Path(args[args.index("--request") + 1])
    response = Path(args[args.index("--response") + 1])
    key = (args[0], args[1], env.get("HF_HOME"), env.get("PADDLE_PDX_CACHE_HOME"))
    timeout = float(timeout)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("OCR timeout must be finite and positive")
    deadline = time.monotonic() + timeout

    def check_budget():
        if predicate and predicate():
            raise RuntimeError("OCR stop requested")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(args, timeout)
        return remaining

    # A queued caller must be cancellable without terminating the current owner.
    waited = not _lock.acquire(blocking=False)
    if waited:
        while not _lock.acquire(timeout=min(.025, check_budget())):
            pass
    try:
        check_budget()
        try:
            if _process is None or _process.poll() is not None or _key != key:
                close_session()
                check_budget()
                _process = subprocess.Popen(
                    args[:2] + ["--serve"], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", env=env, creationflags=creationflags)
                _key = key
            check_budget()
            _process.stdin.write(json.dumps({"request": str(request), "response": str(response)}) + "\n")
            _process.stdin.flush()
            while True:
                remaining = check_budget()
                if response.is_file():
                    _last_used_at = time.monotonic()
                    _schedule_idle_close(_idle_seconds())
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if _process.poll() is not None:
                    raise RuntimeError("V1 OCR worker exited before returning results")
                time.sleep(min(.005, remaining))
        except BaseException:
            close_session()
            raise
    finally:
        _lock.release()

atexit.register(close_session)
