"""Read-only queue snapshots; failures never interrupt Telegram processing."""
import asyncio
import logging
import multiprocessing
import os
import time
from pathlib import Path
from pipeline_v2.atomic_io import atomic_write_json

SNAPSHOT = Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parent.parent / "workspace"))) / "telegram_queue.json"

class MonitoredQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        # Importing the bot in a spawned worker must not erase the real queue.

    def publish(self):
        if multiprocessing.current_process().name != "MainProcess":
            return
        try:
            items = [{"position": i, "telegram_position": job.get("pos"),
                      "name": str(job.get("filename") or job.get("url") or "Video tiếp tục xử lý"),
                      "source": "Telegram"} for i, job in enumerate(self._queue, 1)]
            atomic_write_json(SNAPSHOT, {"items": items, "updated_at": time.time(), "pid": os.getpid()})
        except Exception:
            logging.getLogger(__name__).exception("Cannot publish queue snapshot")

    def put_nowait(self, item):
        super().put_nowait(item)
        self.publish()

    def get_nowait(self):
        item = super().get_nowait()
        self.publish()
        return item
