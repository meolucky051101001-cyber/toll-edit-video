"""Cooperative cancellation scoped to batch-owned subprocesses."""
import contextvars
import os
import subprocess
import time

stop_check = contextvars.ContextVar("batch_stop_check", default=None)


def _video_context_id():
    try:
        try:
            from v1_resource_control import video_context
        except ImportError:
            from .v1_resource_control import video_context
        metrics = video_context.get()
        return getattr(metrics, "video_id", None) if metrics is not None else None
    except ImportError:
        return None


def _add_video_correlation(kwargs):
    video_id = _video_context_id()
    if not video_id:
        return kwargs
    env = kwargs.get("env")
    child_env = dict(os.environ if env is None else env)
    child_env["V1_METRICS_VIDEO_ID"] = video_id
    kwargs["env"] = child_env
    return kwargs


def run(args, *, timeout=None, check=False, **kwargs):
    predicate = stop_check.get()
    if predicate is None:
        return subprocess.run(args, timeout=timeout, check=check, **_add_video_correlation(kwargs))
    if predicate():
        raise RuntimeError("Batch stop requested")
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    kwargs = _add_video_correlation(kwargs)
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


import sys as _sys  # noqa: E402
_sys.modules.setdefault("batch_control", _sys.modules[__name__])
_sys.modules.setdefault("backend.batch_control", _sys.modules[__name__])
