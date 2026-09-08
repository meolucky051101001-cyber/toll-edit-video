"""One isolated OCR worker per V1 process, released after each video."""
import atexit
import json
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
    request = Path(args[args.index("--request") + 1])
    response = Path(args[args.index("--response") + 1])
    key = (args[0], args[1], env.get("HF_HOME"), env.get("PADDLE_PDX_CACHE_HOME"))
    with _lock:
        if _process is None or _process.poll() is not None or _key != key:
            close_session()
            _process = subprocess.Popen(
                args[:2] + ["--serve"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", env=env, creationflags=creationflags)
            _key = key
        try:
            _process.stdin.write(json.dumps({"request": str(request), "response": str(response)}) + "\n")
            _process.stdin.flush()
            deadline = time.monotonic() + timeout
            while not response.is_file():
                if _process.poll() is not None:
                    raise RuntimeError("V1 OCR worker exited before returning results")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(args, timeout)
                time.sleep(.025)
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        except BaseException:
            close_session()
            raise

atexit.register(close_session)
