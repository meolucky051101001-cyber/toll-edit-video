"""Read-only queue snapshots; failures never interrupt Telegram processing."""
import asyncio
import logging
import multiprocessing
import os
import time
from pathlib import Path
from pipeline_v2.atomic_io import atomic_write_json

def _resolve_snapshot_path() -> Path:
    ws = Path(os.getenv("AUTODUB_WORKSPACE", str(Path(__file__).resolve().parent.parent / "workspace")))
    bs = ws / "bot_system"
    target = bs / "telegram_queue.json"
    if target.exists() or bs.is_dir():
        return target
    return ws / "telegram_queue.json"

SNAPSHOT = _resolve_snapshot_path()

class MonitoredQueue(asyncio.Queue):
    def __init__(self):
        super().__init__()
        self.active_job = None
        # Importing the bot in a spawned worker must not erase the real queue.

    def publish(self):
        if multiprocessing.current_process().name != "MainProcess":
            return
        try:
            items = []
            jobs = list(self._queue)
            if self.active_job is not None:
                jobs.insert(0, self.active_job)
            for i, job in enumerate(jobs, 1):
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
                        "filename": job.get("filename", ""),
                        "voice_mode": job.get("voice_mode", "auto"),
                        "video_mode": job.get("video_mode"),
                        "job_overrides": job.get("job_overrides"),
                        "resume_state": job.get("resume_state"),
                        "retry_count": job.get("retry_count", 0),
                        "job_id": job.get("job_id"),
                        "in_progress": job is self.active_job,
                    })
                else:
                    items.append({
                        "position": i,
                        "telegram_position": i,
                        "name": str(job),
                        "url": str(job) if str(job).startswith("http") else "",
                        "source": "Telegram"
                    })
            active = items.pop(0) if self.active_job is not None else None
            atomic_write_json(SNAPSHOT, {"items": items, "active_job": active,
                                         "updated_at": time.time(), "pid": os.getpid()})
        except Exception:
            logging.getLogger(__name__).exception("Cannot publish queue snapshot")

    def put_nowait(self, item):
        super().put_nowait(item)
        self.publish()

    def get_nowait(self):
        item = super().get_nowait()
        self.active_job = item
        self.publish()
        return item

    def task_done(self):
        super().task_done()
        self.active_job = None
        self.publish()

    def clear(self):
        """Xóa sạch hàng đợi hiện tại và cập nhật lại file JSON."""
        while not self.empty():
            try:
                super().get_nowait()
                super().task_done()
            except Exception:
                break
        self.publish()

