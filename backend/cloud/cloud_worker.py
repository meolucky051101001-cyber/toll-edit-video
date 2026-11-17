"""
cloud_worker.py - Luồng chạy nền (Background Worker) lấy job từ queue và gọi CloudOrchestrator.
Hỗ trợ dừng an toàn và theo dõi trạng thái.
"""

import logging
import threading
import time
import uuid
from typing import Optional

from .cloud_job_store import CloudJob, CloudJobStore
from .cloud_orchestrator import CloudOrchestrator

logger = logging.getLogger("cloud.worker")

_WORKER_INSTANCE: Optional["CloudWorker"] = None
_WORKER_LOCK = threading.Lock()


class CloudWorker:
    def __init__(self, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self.store = CloudJobStore()
        self.orchestrator = CloudOrchestrator(self.store)
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current_job: Optional[CloudJob] = None
        self._owner_id = 'worker-' + uuid.uuid4().hex

    def start(self):
        """Khởi động background thread nếu chưa chạy."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self.store.recover()  # Dọn dẹp trạng thái job cũ nếu máy vừa khởi động lại
        self._thread = threading.Thread(target=self._run_loop, name="CloudGPUWorker", daemon=True)
        self._thread.start()
        logger.info("Cloud GPU Worker đã khởi động nền.")

    def stop(self, timeout: float = 10.0):
        """Yêu cầu dừng luồng nền."""
        self._stop_event.set()
        if self._current_job:
            self.store.request_cancel(self._current_job.job_id)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        stopped = not self.is_running()
        logger.info("Cloud GPU Worker %s", "đã dừng" if stopped else "đang chờ bước hiện tại kết thúc")
        return stopped

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def get_current_job(self) -> Optional[CloudJob]:
        return self._current_job

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                job = self.store.claim_next(self._owner_id)
                if job:
                    self._current_job = job
                    try:
                        self.orchestrator.process_job(job, cancel_check=self._stop_event.is_set)
                    finally:
                        self._current_job = None
                else:
                    # Hàng đợi trống, nghỉ giải lao
                    self._stop_event.wait(self.poll_interval)
            except Exception as exc:
                logger.error("Lỗi trong vòng lặp CloudWorker: %s", exc, exc_info=True)
                self._stop_event.wait(self.poll_interval)


def get_cloud_worker() -> CloudWorker:
    """Singleton getter cho CloudWorker."""
    global _WORKER_INSTANCE
    with _WORKER_LOCK:
        if _WORKER_INSTANCE is None:
            _WORKER_INSTANCE = CloudWorker()
        return _WORKER_INSTANCE
