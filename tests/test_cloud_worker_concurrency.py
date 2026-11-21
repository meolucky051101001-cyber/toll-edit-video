from __future__ import annotations

import threading
import time

from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_worker import CloudWorker


def test_worker_runs_up_to_shared_limit_and_drains_each_job(tmp_path):
    store = CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False)
    store.set_concurrency_limit(2)
    video = tmp_path / "parallel.mp4"
    video.write_bytes(b"sample")
    jobs = [store.enqueue(str(video), video_name=f"clip-{index}.mp4") for index in range(2)]
    both_started = threading.Event()
    release_jobs = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    class ConcurrentOrchestrator:
        adapter = None

        def __init__(self, job_store):
            self.store = job_store

        def process_job(self, job, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_started.set()
            release_jobs.wait(timeout=5)
            for state, percent in (("uploading", 20), ("preparing", 40),
                                   ("processing", 60), ("downloading", 80),
                                   ("completed", 100)):
                assert self.store.update_progress(
                    job.job_id, state=state, percent=percent,
                    owner_id=job.owner_id, attempt=job.attempt,
                )
            with lock:
                active -= 1
            return True

        def release_warm_sessions(self):
            return 0

        def release_idle_warm_sessions_if_no_work(self):
            return 0

    worker = CloudWorker(
        poll_interval=0.01, heartbeat_interval=0.05, lease_seconds=5,
        store=store, orchestrator=ConcurrentOrchestrator(store),
    )
    worker.start()
    try:
        assert both_started.wait(timeout=3), "worker did not start both jobs within the configured limit"
        active_jobs = store.list_active_jobs()
        assert len(active_jobs) == 2
        assert {job.job_id for job in active_jobs} == {job.job_id for job in jobs}
        assert worker.get_current_job() is not None
        assert len(worker.get_current_jobs()) == 2
    finally:
        release_jobs.set()
        stopped = worker.stop(timeout=3)

    assert stopped["stopped"] is True
    assert peak == 2
    assert all(store.get_job(job.job_id).state == "completed" for job in jobs)


def test_worker_can_cancel_one_of_multiple_current_jobs(tmp_path):
    store = CloudJobStore(tmp_path / "cloud.db", rollout_controlled=False)
    store.set_concurrency_limit(2)
    video = tmp_path / "cancel.mp4"
    video.write_bytes(b"sample")
    jobs = [store.enqueue(str(video), video_name=f"cancel-{index}.mp4") for index in range(2)]
    release = threading.Event()
    entered = threading.Event()

    class SlowOrchestrator:
        adapter = None

        def process_job(self, job, **kwargs):
            entered.set()
            release.wait(timeout=2)
            self_state = store.get_job(job.job_id)
            if self_state and self_state.state == "cancel_requested":
                store.update_progress(
                    job.job_id, state="cancelled", percent=self_state.percent,
                    owner_id=job.owner_id, attempt=job.attempt,
                )
            else:
                for state, percent in (("uploading", 20), ("preparing", 40),
                                       ("processing", 60), ("downloading", 80),
                                       ("completed", 100)):
                    assert store.update_progress(
                        job.job_id, state=state, percent=percent,
                        owner_id=job.owner_id, attempt=job.attempt,
                    )
            return True

        def release_warm_sessions(self):
            return 0

    worker = CloudWorker(
        poll_interval=0.01, heartbeat_interval=0.05, lease_seconds=5,
        store=store, orchestrator=SlowOrchestrator(),
    )
    worker.start()
    try:
        assert entered.wait(timeout=2)
        deadline = time.monotonic() + 2
        while len(worker.get_current_jobs()) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(worker.get_current_jobs()) == 2
        assert worker.cancel_job(jobs[0].job_id)
        assert store.get_job(jobs[0].job_id).state == "cancel_requested"
        assert store.get_job(jobs[1].job_id).state in {"provisioning", "processing"}
    finally:
        release.set()
        worker.stop(timeout=3)

    assert store.get_job(jobs[0].job_id).state == "cancelled"
    assert store.get_job(jobs[1].job_id).state == "completed"


def test_worker_can_refresh_and_claim_after_a_stale_provider_snapshot(tmp_path):
    from types import SimpleNamespace
    store = CloudJobStore(tmp_path / "refresh.db", rollout_controlled=False)
    store.record_provider_usage_snapshot({"ok": True, "balance": 1, "age_seconds": 10000})
    job = store.enqueue("offline.mp4")
    entered = threading.Event()

    class OfflineOrchestrator:
        adapter = SimpleNamespace(get_usage=lambda: {"ok": True, "balance": 50.0})

        def process_job(self, claimed, **kwargs):
            assert store.update_progress(claimed.job_id, state="failed", owner_id=claimed.owner_id,
                                         attempt=claimed.attempt, cleanup_status="verified")
            entered.set()
            return False

        def release_warm_sessions(self):
            return 0

        def release_idle_warm_sessions_if_no_work(self):
            return 0

    worker = CloudWorker(store=store, orchestrator=OfflineOrchestrator(), poll_interval=0.01)
    worker.start()
    try:
        assert entered.wait(timeout=3), "fresh read-only provider sample did not unblock the worker"
        assert store.get_provider_usage_snapshot()["balance"] == 50.0
        assert store.get_provider_usage_snapshot()["is_stale"] is False
        assert store.get_job(job.job_id).state == "failed"
    finally:
        assert worker.stop(timeout=3)["stopped"] is True
