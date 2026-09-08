"""Stage timings without subtitle contents, credentials or network calls."""
import functools
import inspect
import logging
import time

def _memory():
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1048576, 1)
    except (ImportError, OSError):
        return None

def stage(name, cleanup=None):
    def decorate(fn):
        def finish(start, ok):
            try:
                if cleanup:
                    cleanup()
            finally:
                logging.getLogger("v1.performance").info(
                    "stage=%s seconds=%.2f success=%s rss_mb=%s",
                    name, time.monotonic()-start, ok, _memory())
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapped(*args, **kwargs):
                start, ok = time.monotonic(), False
                try:
                    result = await fn(*args, **kwargs)
                    ok = True
                    return result
                finally:
                    finish(start, ok)
        else:
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                start, ok = time.monotonic(), False
                try:
                    result = fn(*args, **kwargs)
                    ok = True
                    return result
                finally:
                    finish(start, ok)
        return wrapped
    return decorate
