"""One isolated OCR worker per V1 process, released after each video."""
import atexit
import json
import math
import subprocess
import threading
import time
from pathlib import Path

_lock = threading.RLock()
_process = None
_key = None

def close_session():
    global _process, _key
    with _lock:
        process, _process, _key = _process, None, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()

def run_request(args, *, timeout, env, creationflags=0, **kwargs):
    global _process, _key
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
                    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
                if _process.poll() is not None:
                    raise RuntimeError("V1 OCR worker exited before returning results")
                time.sleep(min(.025, remaining))
        except BaseException:
            close_session()
            raise
    finally:
        _lock.release()

atexit.register(close_session)
