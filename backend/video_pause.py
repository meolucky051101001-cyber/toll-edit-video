"""Cooperative pause with canonical paths and execution-bound acknowledgements."""
import asyncio
import json
import uuid
from pathlib import Path
from pipeline_v2.interprocess_lock import CrossProcessLock
from service_control import control_directory, control_directories, write_intent
ROOT = Path(__file__).resolve().parent.parent
BASE = control_directory(ROOT)
FLAG = BASE / "video.pause"
ACK = BASE / "video.pause.ack"

def _flags():
    return list(dict.fromkeys([FLAG, *(p / "video.pause" for p in control_directories(ROOT))]))

def _guard():
    return CrossProcessLock(FLAG.with_suffix(".guard"), name="Video pause", timeout_seconds=5, poll_interval=.02)

def _identity():
    import job_tracker
    status = job_tracker.get_status()
    identity = status.get("execution_id") or status.get("job_id")
    context = job_tracker.current_execution.get()
    if context is not None and context != identity:
        return None, None
    return identity, status.get("generation")

def _matches(raw, job_id, generation):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return job_id is None
    if not isinstance(value, dict):
        return job_id is None
    return (job_id is None or value.get("job_id") == job_id) and (generation is None or value.get("generation") == generation)

def _state_unlocked(job_id, generation):
    for flag in _flags():
        try:
            raw = flag.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        if not _matches(raw, job_id, generation):
            continue
        try:
            return "paused" if flag.with_name("video.pause.ack").read_text(encoding="utf-8") == raw else "pausing"
        except FileNotFoundError:
            return "pausing"
    return "running"

def state(job_id=None, generation=None):
    with _guard():
        return _state_unlocked(job_id, generation)

def request_pause(job_id=None, generation=None):
    if job_id is None:
        job_id, generation = _identity()
    if not job_id or generation is None:
        raise ValueError("Không xác minh được execution cần tạm dừng.")
    with _guard():
        if _state_unlocked(job_id, generation) == "running":
            write_intent(FLAG, json.dumps({"job_id": job_id, "generation": generation, "token": uuid.uuid4().hex}))

def _clear_unlocked(job_id=None, generation=None):
    for flag in _flags():
        for path in (flag, flag.with_name("video.pause.ack")):
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            if job_id is None or _matches(raw, job_id, generation):
                path.unlink(missing_ok=True)

def clear_execution_pause(job_id, generation):
    if not job_id or generation is None:
        return
    with _guard():
        _clear_unlocked(job_id, generation)

def resume(job_id=None, generation=None):
    # No-argument resume is reserved for an explicit administrative reset.
    with _guard():
        _clear_unlocked(job_id, generation)

def _acknowledge(job_id, generation):
    with _guard():
        matched = False
        for flag in _flags():
            try:
                raw = flag.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            if _matches(raw, job_id, generation):
                write_intent(flag.with_name("video.pause.ack"), raw)
                matched = True
        return matched

async def checkpoint():
    import shared_state, job_tracker
    job_id, generation = _identity()
    if not job_id or generation is None:
        return
    while True:
        if getattr(shared_state, "stop_requested", False) or job_tracker.is_stop_requested():
            clear_execution_pause(job_id, generation)
            return
        if not _acknowledge(job_id, generation):
            return
        await asyncio.sleep(.3)
