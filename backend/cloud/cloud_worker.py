"""
cloud_worker.py - Luồng chạy nền (Background Worker) lấy job từ queue và gọi CloudOrchestrator.
Hỗ trợ lease/heartbeat, hủy trong lúc chạy và trả về trạng thái dừng trung thực.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

from .cloud_job_store import CloudJob, CloudJobStore
from .cloud_orchestrator import CloudOrchestrator
from .colab_bridge import ColabProviderAdapter, abort_session_processes

logger = logging.getLogger("cloud.worker")

_WORKER_INSTANCE: Optional["CloudWorker"] = None
_WORKER_LOCK = threading.Lock()


class CloudWorker:
    def __init__(
        self,
        poll_interval: float = 5.0,
        *,
        worker_id: Optional[str] = None,
        lease_seconds: float = 60.0,
        heartbeat_interval: float = 15.0,
        store: Optional[CloudJobStore] = None,
        adapter: Optional[ColabProviderAdapter] = None,
        orchestrator: Optional[CloudOrchestrator] = None,
    ):
        self.poll_interval = max(0.05, float(poll_interval))
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.heartbeat_interval = max(0.05, float(heartbeat_interval))
        self.store = store or CloudJobStore()
        self.orchestrator = orchestrator or CloudOrchestrator(self.store, adapter=adapter)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._accepting_jobs = True
        self._thread: Optional[threading.Thread] = None
        self._current_job: Optional[CloudJob] = None
        self._current_cancel_event: Optional[threading.Event] = None
        self._last_reconcile_at = 0.0

    def start(self):
        """Khởi động một worker và đối soát lease hết hạn trong luồng nền."""
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._accepting_jobs = True

            self._last_reconcile_at = time.monotonic() - max(self.heartbeat_interval, self.poll_interval)
            self._thread = threading.Thread(
                target=self._run_loop,
                name=f"CloudGPUWorker-{self.worker_id}",
                daemon=True,
            )
            self._thread.start()
        logger.info("Cloud GPU Worker %s đã khởi động nền.", self.worker_id)

    def cancel_current_job(self, reason: str = "Worker đang dừng theo yêu cầu.") -> bool:
        """Báo hủy job đang chạy hiện tại của worker (nếu có) và ngắt tiến trình con của session đó."""
        with self._state_lock:
            cur_job = self._current_job
            cancel_ev = self._current_cancel_event
        if cancel_ev is not None:
            cancel_ev.set()
        if cur_job is None:
            return False

        self.store.request_cancel(cur_job.job_id, reason=reason)
        latest = self.store.get_job(cur_job.job_id) or cur_job
        if latest.session_name:
            abort_fn = getattr(self.orchestrator.adapter, "abort_session_processes", None)
            if callable(abort_fn):
                try:
                    abort_fn(latest.session_name)
                except Exception:
                    pass
            else:
                try:
                    abort_session_processes(latest.session_name)
                except Exception:
                    pass
        return True

    def stop(self, timeout: float = 10.0, cancel_current: bool = False) -> Dict[str, Any]:
        """Ngừng nhận job mới và đợi luồng worker thoát; trả về trạng thái dừng thực tế."""
        with self._state_lock:
            self._accepting_jobs = False
            self._stop_event.set()
            thread_ref = self._thread

        if cancel_current:
            self.cancel_current_job()

        if thread_ref and thread_ref.is_alive():
            thread_ref.join(timeout=max(0.0, float(timeout)))

        still_alive = bool(thread_ref and thread_ref.is_alive())
        if not still_alive:
            release_fn = getattr(self.orchestrator, "release_warm_sessions", None)
            if callable(release_fn):
                try:
                    release_fn()
                except Exception as rel_exc:
                    logger.warning("Cảnh báo giải phóng session giữ ấm khi dừng worker: %s", rel_exc)
        state_str = "stopping" if still_alive else "stopped"
        if still_alive:
            logger.warning(
                "Cloud GPU Worker %s vẫn đang chạy job hiện tại sau timeout %.1fs (%s).",
                self.worker_id,
                timeout,
                state_str,
            )
        else:
            logger.info("Cloud GPU Worker %s đã dừng hoàn toàn.", self.worker_id)

        return {
            "stopped": not still_alive,
            "worker_running": still_alive,
            "worker_state": state_str,
            "accepting_jobs": False,
        }

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def is_accepting_jobs(self) -> bool:
        return bool(self.is_running() and self._accepting_jobs and not self._stop_event.is_set())

    def get_worker_state(self) -> str:
        if not self.is_running():
            return "stopped"
        if self._stop_event.is_set() or not self._accepting_jobs:
            return "stopping"
        return "running"

    def get_current_job(self) -> Optional[CloudJob]:
        with self._state_lock:
            cur = self._current_job
        if cur is None:
            return None
        try:
            fresh = self.store.get_job(cur.job_id)
            if isinstance(fresh, CloudJob):
                return fresh
        except Exception:
            pass
        return cur

    def _heartbeat_loop(
        self,
        job: CloudJob,
        job_cancel_ev: threading.Event,
        hb_stop_ev: threading.Event,
    ) -> None:
        while not hb_stop_ev.wait(self.heartbeat_interval):
            try:
                lost, reason = self.store.is_cancel_or_lease_lost(
                    job.job_id,
                    owner_id=self.worker_id,
                    attempt=job.attempt,
                )
                if lost:
                    job_cancel_ev.set()
                    if reason == "cancel_requested":
                        latest = self.store.get_job(job.job_id) or job
                        if latest.session_name:
                            try:
                                abort_session_processes(latest.session_name)
                            except Exception:
                                pass
                    return
                renewed = self.store.heartbeat(
                    job.job_id,
                    owner_id=self.worker_id,
                    attempt=job.attempt,
                    lease_seconds=self.lease_seconds,
                )
                if not renewed:
                    job_cancel_ev.set()
                    return
            except Exception as exc:
                logger.warning("Lỗi heartbeat cho job %s: %s", job.job_id, exc)

    def _run_loop(self):
        while not self._stop_event.is_set() and self._accepting_jobs:
            try:
                now_mono = time.monotonic()
                if now_mono - self._last_reconcile_at >= max(self.heartbeat_interval, self.poll_interval):
                    self._last_reconcile_at = now_mono
                    try:
                        self.store.reconcile_expired_leases(adapter=self.orchestrator.adapter)
                    except Exception as rec_exc:
                        logger.warning("Lỗi đối soát lease hết hạn: %s", rec_exc)

                if self._stop_event.is_set() or not self._accepting_jobs:
                    break

                fresh_usage: Optional[Dict[str, Any]] = None
                snap = self.store.get_provider_usage_snapshot()
                if snap is not None and (snap.get("is_stale") or not snap.get("ok")):
                    get_usage_fn = getattr(self.orchestrator.adapter, "get_usage", None)
                    if callable(get_usage_fn):
                        try:
                            u = get_usage_fn()
                            if isinstance(u, dict):
                                fresh_usage = u
                        except Exception as u_exc:
                            logger.warning("Không làm mới được số dư Colab trước claim_next: %s", u_exc)

                job = self.store.claim_next(
                    owner_id=self.worker_id,
                    lease_seconds=self.lease_seconds,
                    provider_usage=fresh_usage,
                )
                if job:
                    self.process_claimed_job(job)
                else:
                    idle_rel_fn = getattr(self.orchestrator, "release_idle_warm_sessions_if_no_work", None)
                    if callable(idle_rel_fn):
                        try:
                            idle_rel_fn()
                        except Exception as rel_exc:
                            logger.warning("Cảnh báo đối soát session giữ ấm rảnh: %s", rel_exc)
                    self._stop_event.wait(self.poll_interval)
            except Exception as exc:
                logger.error("Lỗi trong vòng lặp CloudWorker: %s", exc, exc_info=True)
                self._stop_event.wait(self.poll_interval)

    def process_claimed_job(self, job: CloudJob, progress_callback=None) -> bool:
        """Keep the lease alive for both dashboard and Telegram jobs."""
        job_cancel_ev = threading.Event()
        hb_stop_ev = threading.Event()
        with self._state_lock:
            self._current_job = job
            self._current_cancel_event = job_cancel_ev
        hb_thread = threading.Thread(
            target=self._heartbeat_loop,
            args=(job, job_cancel_ev, hb_stop_ev),
            name=f"CloudHB-{job.job_id[-8:]}",
            daemon=True,
        )
        hb_thread.start()
        try:
            args = (job,) if progress_callback is None else (job, progress_callback)
            return self.orchestrator.process_job(
                *args, owner_id=self.worker_id, cancel_event=job_cancel_ev,
            )
        finally:
            hb_stop_ev.set()
            hb_thread.join(timeout=2.0)
            with self._state_lock:
                self._current_job = None
                self._current_cancel_event = None


def get_cloud_worker() -> CloudWorker:
    """Singleton getter cho CloudWorker."""
    global _WORKER_INSTANCE
    with _WORKER_LOCK:
        if _WORKER_INSTANCE is None:
            _WORKER_INSTANCE = CloudWorker()
        return _WORKER_INSTANCE


def peek_cloud_worker() -> Optional[CloudWorker]:
    """Read-only getter: trả về CloudWorker hiện có mà không tự khởi tạo mới."""
    with _WORKER_LOCK:
        return _WORKER_INSTANCE
