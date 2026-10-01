"""Cooperative pause between video processing stages, shared across V1 processes."""
import asyncio
import os
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent / "workspace" / "control"
FLAG = BASE / "video.pause"
ACK = BASE / "video.pause.ack"
def state():
    if not FLAG.exists(): return "running"
    try:
        return "paused" if ACK.read_text() == FLAG.read_text() else "pausing"
    except FileNotFoundError: return "pausing"
def request_pause():
    import uuid
    BASE.mkdir(parents=True, exist_ok=True)
    if not FLAG.exists(): FLAG.write_text(uuid.uuid4().hex)
def resume():
    FLAG.unlink(missing_ok=True)
    ACK.unlink(missing_ok=True)
async def checkpoint():
    import shared_state, job_tracker
    while FLAG.exists():
        if getattr(shared_state, "stop_requested", False) or job_tracker.is_stop_requested():
            resume()
            return
        try: ACK.write_text(FLAG.read_text())
        except FileNotFoundError: return
        await asyncio.sleep(0.3)
