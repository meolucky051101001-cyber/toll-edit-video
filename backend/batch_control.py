"""Cooperative cancellation scoped to batch-owned subprocesses."""
import contextvars
import os
import subprocess
import time

stop_check = contextvars.ContextVar("batch_stop_check", default=None)


def run(args, *, timeout=None, check=False, **kwargs):
    predicate = stop_check.get()
    if predicate is None:
        return subprocess.run(args, timeout=timeout, check=check, **kwargs)
    if predicate():
        raise RuntimeError("Batch stop requested")
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    started = time.monotonic()
    with subprocess.Popen(args, **kwargs) as process:
        try:
            while True:
                if predicate():
                    raise RuntimeError("Batch stop requested")
                remaining = None if timeout is None else timeout - (time.monotonic() - started)
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.25, remaining) if remaining else 0.25)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                if process.poll() is None:
                    process.kill()
            process.communicate()
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result
