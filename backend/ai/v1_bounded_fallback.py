"""Bound caller wait; allow at most one outstanding fallback per process."""
import threading

_slot = threading.Lock()

def run_fallback(fn, *args, timeout=40):
    if not _slot.acquire(blocking=False):
        raise TimeoutError("Previous fallback is still running")
    done = threading.Event()
    result = []
    def worker():
        try:
            result.append((True, fn(*args)))
        except BaseException as exc:
            result.append((False, exc))
        finally:
            _slot.release()
            done.set()
    thread = threading.Thread(target=worker, daemon=True, name="v1-translation-fallback")
    try:
        thread.start()
    except BaseException:
        _slot.release()
        raise
    if not done.wait(timeout):
        raise TimeoutError("Fallback exceeded caller wait budget")
    ok, value = result[0]
    if not ok:
        raise value
    return value
