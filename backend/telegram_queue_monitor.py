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
            items = []
            for i, job in enumerate(self._queue, 1):
                if isinstance(job, dict):
                    name = str(job.get("filename") or job.get("url") or "Video tiếp tục xử lý")
                    chat_id = None
                    update_obj = job.get("update")
                    if update_obj and hasattr(update_obj, "message") and update_obj.message:
                        chat_id = update_obj.message.chat_id
                    elif job.get("chat_id"):
                        chat_id = job.get("chat_id")
                    items.append({
                        "position": i,
                        "telegram_position": job.get("pos", i),
                        "name": name,
                        "url": job.get("url", ""),
                        "type": job.get("type", "url"),
                        "source": "Telegram",
                        "chat_id": chat_id,
                        "file_id": job.get("file_id", ""),
                        "filename": job.get("filename", "")
                    })
                else:
                    items.append({
                        "position": i,
                        "telegram_position": i,
                        "name": str(job),
                        "url": str(job) if str(job).startswith("http") else "",
                        "source": "Telegram"
                    })
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

    def clear(self):
        """Xóa sạch hàng đợi hiện tại và cập nhật lại file JSON."""
        while not self.empty():
            try:
                self.get_nowait()
            except Exception:
                break
        try:
            atomic_write_json(SNAPSHOT, {"items": [], "updated_at": time.time(), "pid": os.getpid()})
        except Exception:
            pass

