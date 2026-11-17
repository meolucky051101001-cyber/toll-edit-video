"""Readiness describes real state and writable storage, separate from liveness."""
import os
import json
import shutil
import tempfile
import time
from pathlib import Path


def health_snapshot(workspace, queue_snapshot, tracker_status, disabled=False):
    workspace = Path(workspace)
    reasons = []
    writable = False
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=workspace) as probe:
            probe.write(b"ready")
            probe.flush()
            os.fsync(probe.fileno())
        writable = True
    except OSError as exc:
        reasons.append("storage_unwritable:" + type(exc).__name__)
    queue = {"recovery_state": "not_started", "revision": None, "heartbeat_age": None}
    if Path(queue_snapshot).exists():
        try:
            from queue_control_service import read_snapshot, health_path
            data = read_snapshot(queue_snapshot)
            queue = {"recovery_state": data.get("recovery_state", "ready"), "revision": data.get("revision"),
                     "heartbeat_age": max(0, time.time() - float(data.get("updated_at") or 0))}
            if queue["recovery_state"] not in {"ready", "restored", "healthy"}:
                reasons.append("queue_recovery_required")
            failure_file = health_path(queue_snapshot)
            if failure_file.exists():
                queue_health = json.loads(failure_file.read_text(encoding="utf-8"))
                if queue_health.get("recovery_state") == "failed":
                    queue["recovery_state"] = "failed"
                    reasons.append("queue_publication_failed")
            owner = data.get("owner") or {}
            import job_tracker
            if not owner.get("create_time") or not job_tracker._owner_is_running(
                    {"pid": owner.get("pid"), "owner_create_time": owner.get("create_time")}):
                queue["owner_status"] = "unknown_or_offline"
            else:
                queue["owner_status"] = "live"
            queue["stale"] = queue["heartbeat_age"] > 15
        except (ValueError, OSError) as exc:
            queue["recovery_state"] = "degraded"
            reasons.append("queue_unreadable:" + type(exc).__name__)
    try:
        import psutil
        process = psutil.Process()
        process_data = {"pid": process.pid, "create_time": process.create_time(), "rss_bytes": process.memory_info().rss,
                        "available_memory_bytes": psutil.virtual_memory().available}
    except Exception:
        process_data = {"pid": os.getpid(), "create_time": None, "rss_bytes": None, "available_memory_bytes": None}
    try:
        free = shutil.disk_usage(workspace).free
        if free < 256 * 1024 * 1024:
            reasons.append("disk_space_low")
    except OSError:
        free = None
        reasons.append("disk_unknown")
    if tracker_status.get("status") in {"recovery_required", "stop_incomplete"}:
        reasons.append("execution_needs_review")
    from batch_plan_service import BatchPlanStore
    plans = BatchPlanStore(workspace)
    try:
        interrupted = plans.pending_recovery() if plans.path.exists() else []
    except Exception as exc:
        interrupted = []
        reasons.append("batch_store_unreadable:" + type(exc).__name__)
    if interrupted:
        reasons.append("batch_recovery_required")
    return {"ready": not reasons, "status": "disabled" if disabled and not reasons else "ready" if not reasons else "degraded",
            "service_disabled": disabled, "reasons": reasons, "storage_writable": writable, "disk_free_bytes": free,
            "queue": queue, "process": process_data, "stop_state": tracker_status.get("status"),
            "interrupted_batches": interrupted,
            "active_worker_count": sum(bool(w.get("active")) for w in tracker_status.get("workers", {}).values()),
            "updated_at": time.time(), "version": "dashboard-stability-20261115"}
