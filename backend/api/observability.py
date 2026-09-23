from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.core.database import get_db
from backend.core.queue import system_queue
from backend.services.observability import telemetry_service

router = APIRouter(prefix="/api/system", tags=["observability"])


@router.get("/metrics")
def get_system_metrics() -> dict[str, Any]:
    """Returns aggregated runtime performance telemetry, latency percentiles, and error rates."""
    return telemetry_service.get_metrics_summary()


@router.get("/release-readiness")
async def get_release_readiness(
    request: Request,
    probe: bool = False,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Evaluates release criteria checklist for automated CI/CD and production deployment."""
    checks = {}
    is_ready = True
    warnings = []

    # 1. Database Health & Schema Check
    try:
        db.execute(text("SELECT 1"))
        table_rows = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
        existing_tables = {row[0] for row in table_rows}

        required_tables = {"videos", "search_jobs", "job_videos", "dubbing_jobs", "video_metric_snapshots"}
        missing_tables = required_tables - existing_tables

        if missing_tables:
            checks["database"] = {"status": "FAIL", "detail": f"Missing tables: {list(missing_tables)}"}
            is_ready = False
        else:
            checks["database"] = {"status": "PASS", "detail": f"All {len(required_tables)} required tables verified"}
    except Exception as exc:
        checks["database"] = {"status": "FAIL", "detail": str(exc)}
        is_ready = False

    # 2. Queue Health
    try:
        q_stats = system_queue.get_stats()
        dlq_count = q_stats.get("dead_letter", 0)
        if dlq_count > 10:
            checks["job_queue"] = {"status": "WARN", "detail": f"{dlq_count} tasks in Dead Letter Queue"}
            warnings.append(f"Elevated DLQ count: {dlq_count}")
        else:
            checks["job_queue"] = {"status": "PASS", "detail": f"Queue normal (pending: {q_stats['pending']}, in_flight: {q_stats['in_flight']})"}
    except Exception as exc:
        checks["job_queue"] = {"status": "FAIL", "detail": str(exc)}
        is_ready = False

    # 3. Telemetry Traffic & Error Rate
    try:
        metrics = telemetry_service.get_metrics_summary()
        total_requests = metrics.get("counters", {}).get("http_requests_total", 0)
        if total_requests == 0:
            checks["telemetry_traffic"] = {
                "status": "WARN",
                "detail": "Telemetry has not observed any traffic yet",
            }
            warnings.append("Telemetry has not observed any traffic yet")
        else:
            checks["telemetry_traffic"] = {
                "status": "PASS",
                "detail": f"Observed {total_requests} HTTP requests",
            }

        err_rate = metrics.get("error_rate_percent", 0.0)
        if err_rate > 5.0:
            checks["error_rate"] = {"status": "WARN", "detail": f"High error rate: {err_rate}%"}
            warnings.append(f"HTTP error rate is {err_rate}%")
        else:
            checks["error_rate"] = {"status": "PASS", "detail": f"Acceptable error rate ({err_rate}%)"}
    except Exception as exc:
        checks["error_rate"] = {"status": "WARN", "detail": str(exc)}

    # 4. Worker Pool Status
    try:
        from backend.core.queue import system_worker_pool
        pool_status = system_worker_pool.get_status()
        active_w = pool_status.get("active_workers", 0)
        reg_handlers = pool_status.get("registered_task_types", [])
        if active_w == 0 or not reg_handlers:
            checks["worker_pool"] = {
                "status": "WARN",
                "detail": f"Worker pool active={active_w}, handlers={reg_handlers}",
            }
            warnings.append("Worker pool has no registered handlers or active workers")
        else:
            checks["worker_pool"] = {
                "status": "PASS",
                "detail": f"Worker pool healthy ({active_w} workers, handlers: {reg_handlers})",
            }
    except Exception as exc:
        checks["worker_pool"] = {"status": "WARN", "detail": str(exc)}

    # 5. Real Provider Readiness (Douyin & Xiaohongshu)
    try:
        browser = getattr(request.app.state, "browser", None)
        douyin_browser = getattr(request.app.state, "douyin_browser", None)

        provider_details = {
            "douyin": douyin_browser.state if douyin_browser else "uninitialized",
            "xiaohongshu": browser.state if browser else "uninitialized",
        }

        if probe and douyin_browser and browser:
            dy_ok, dy_detail = await douyin_browser.probe_live()
            xhs_ok, xhs_detail = await browser.probe_live()
            provider_details["douyin_live_probe"] = "PASS" if dy_ok else f"FAIL ({dy_detail})"
            provider_details["xhs_live_probe"] = "PASS" if xhs_ok else f"FAIL ({xhs_detail})"
            if not dy_ok or not xhs_ok:
                checks["providers"] = {
                    "status": "FAIL",
                    "detail": f"Live probe failed: {provider_details}",
                }
                is_ready = False
            else:
                checks["providers"] = {
                    "status": "PASS",
                    "detail": f"All providers live verified: {provider_details}",
                }
        else:
            has_error = (
                (douyin_browser is not None and douyin_browser.state in ("network_error", "restricted"))
                or (browser is not None and browser.state in ("network_error", "restricted"))
            )
            if has_error:
                checks["providers"] = {
                    "status": "FAIL",
                    "detail": f"Provider in error state: {provider_details}",
                }
                is_ready = False
            else:
                checks["providers"] = {
                    "status": "PASS",
                    "detail": f"Provider states operational: {provider_details}",
                }
    except Exception as exc:
        checks["providers"] = {"status": "WARN", "detail": str(exc)}

    # Determine overall gate status
    if not is_ready:
        overall_status = "BLOCKED"
    elif warnings:
        overall_status = "DEGRADED"
    else:
        overall_status = "READY"

    return {
        "status": overall_status,
        "is_ready": is_ready,
        "checks": checks,
        "warnings": warnings,
    }
