"""Durable stage payloads and cancellation that drains native work before retry."""
import asyncio
import json
import os
import threading
import uuid
from types import SimpleNamespace
from datetime import timedelta
from pathlib import Path


def _encode(value):
    if isinstance(value, timedelta):
        return {"__seconds__": value.total_seconds()}
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dict__"):
        return {"__object_fields__": vars(value)}
    raise TypeError(f"Unsupported checkpoint value: {type(value).__name__}")


def save_payload(path, **values):
    path = Path(path)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    data = {"schema": 1, **values}
    for key in ("segments", "floating_segments"):
        if key in data:
            data[key] = [dict(vars(s)) for s in data[key]]
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, default=_encode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def load_payload(path):
    import srt
    def decode(value):
        if set(value) == {"__seconds__"}:
            return timedelta(seconds=value["__seconds__"])
        if set(value) == {"__object_fields__"}:
            return SimpleNamespace(**value["__object_fields__"])
        return value
    data = json.loads(Path(path).read_text(encoding="utf-8"), object_hook=decode)
    if data.get("schema") != 1:
        raise ValueError("Unsupported stage checkpoint schema")
    for key in ("segments", "floating_segments"):
        if key in data:
            subs = []
            for fields in data[key]:
                sub = srt.Subtitle(fields["index"], fields["start"], fields["end"], fields["content"], fields.get("proprietary", ""))
                sub.__dict__.update(fields)
                subs.append(sub)
            data[key] = subs
    return data


async def await_stage(awaitable, timeout, check_stop=None):
    try:
        from batch_control import stop_check
    except ImportError:
        from .batch_control import stop_check
    stopped = threading.Event()
    inherited = stop_check.get()
    def should_stop():
        return stopped.is_set() or bool(inherited and inherited())
    token = stop_check.set(should_stop)
    task = asyncio.ensure_future(awaitable)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not task.done():
            if check_stop:
                check_stop()
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Stage exceeded {timeout}s; worker stopped before retry")
            await asyncio.wait({task}, timeout=0.25)
        return task.result()
    except BaseException:
        stopped.set()
        # A cancelled to_thread future does NOT stop its thread. Drain it, so a
        # later job cannot race the same GPU/output files. Subprocesses cooperate.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # A second stop/cancel must not release an active native worker.
                continue
            except BaseException:
                break
        if task.done() and not task.cancelled():
            try:
                task.result()
            except BaseException:
                pass
        raise
    finally:
        stop_check.reset(token)
