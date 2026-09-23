import asyncio
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

    # 5. Provider Service Readiness
    try:
        browser = getattr(request.app.state, "browser", None)
        douyin_browser = getattr(request.app.state, "douyin_browser", None)

        if browser is None or douyin_browser is None:
            checks["providers"] = {
                "status": "UNINITIALIZED",
                "detail": f"Provider services uninitialized (douyin: {douyin_browser is not None}, xiaohongshu: {browser is not None})",
            }
            warnings.append("Provider browser services are uninitialized; system not ready for live operations")
            is_ready = False
        else:
            has_error = (
                douyin_browser.state in ("network_error", "restricted")
                or browser.state in ("network_error", "restricted")
            )
            if has_error:
                checks["providers"] = {
                    "status": "FAIL",
                    "detail": f"Provider in error state: douyin={douyin_browser.state}, xhs={browser.state}",
                }
                is_ready = False
            elif douyin_browser.state == "closed" and browser.state == "closed":
                checks["providers"] = {
                    "status": "STANDBY",
                    "detail": "Provider services configured and ready on standby. Pass ?probe=true for live network verification.",
                }
                warnings.append("Provider browser sessions on standby (not probed)")
            else:
                checks["providers"] = {
                    "status": "PASS",
                    "detail": f"Provider states operational: douyin={douyin_browser.state}, xhs={browser.state}",
                }

        # 6. Optional Live Provider Reachability Probe
        if probe:
            if browser is None or douyin_browser is None:
                checks["live_providers"] = {
                    "status": "FAIL",
                    "detail": "Cannot run live probe: provider services are uninitialized",
                }
                is_ready = False
            else:
                probe_results = await asyncio.gather(
                    douyin_browser.probe_live(),
                    browser.probe_live(),
                    return_exceptions=True,
                )
                dy_probe_res: Any = probe_results[0]
                xhs_probe_res: Any = probe_results[1]

                if isinstance(dy_probe_res, Exception):
                    dy_ok, dy_detail = False, f"Exception: {dy_probe_res}"
                elif isinstance(dy_probe_res, tuple) and len(dy_probe_res) == 2:
                    dy_ok, dy_detail = bool(dy_probe_res[0]), str(dy_probe_res[1])
                else:
                    dy_ok, dy_detail = False, f"Unexpected probe result: {dy_probe_res}"

                if isinstance(xhs_probe_res, Exception):
                    xhs_ok, xhs_detail = False, f"Exception: {xhs_probe_res}"
                elif isinstance(xhs_probe_res, tuple) and len(xhs_probe_res) == 2:
                    xhs_ok, xhs_detail = bool(xhs_probe_res[0]), str(xhs_probe_res[1])
                else:
                    xhs_ok, xhs_detail = False, f"Unexpected probe result: {xhs_probe_res}"

                def _parse_probe_state(desc: str) -> tuple[int | None, str]:
                    parts = desc.split(":", 1)
                    http_code = None
                    if "HTTP" in parts[0]:
                        try:
                            http_code = int(parts[0].replace("HTTP", "").strip())
                        except ValueError:
                            pass
                    st = parts[1].strip() if len(parts) > 1 else desc.strip()
                    return http_code, st

                dy_http, dy_session_st = _parse_probe_state(dy_detail)
                xhs_http, xhs_session_st = _parse_probe_state(xhs_detail)

                live_probe_details = {
                    "douyin": {
                        "reachable": dy_ok,
                        "http_status": dy_http,
                        "session_state": dy_session_st,
                        "state": dy_detail,
                    },
                    "xiaohongshu": {
                        "reachable": xhs_ok,
                        "http_status": xhs_http,
                        "session_state": xhs_session_st,
                        "state": xhs_detail,
                    },
                }
                if not dy_ok or not xhs_ok:
                    checks["live_providers"] = {
                        "status": "FAIL",
                        "detail": f"Live probe network failure: {live_probe_details}",
                    }
                    checks["providers"] = {
                        "status": "FAIL",
                        "detail": f"Live probe network failure: douyin={dy_detail}, xhs={xhs_detail}",
                    }
                    is_ready = False
                else:
                    checks["live_providers"] = {
                        "status": "PASS",
                        "detail": f"All providers live verified: {live_probe_details}",
                    }
                    checks["providers"] = {
                        "status": "PASS",
                        "detail": f"Provider browser services verified live: douyin={dy_detail}, xhs={xhs_detail}",
                    }
                    warnings = [w for w in warnings if "standby (not probed)" not in w]
                    has_auth_challenge = (
                        dy_session_st in ("login_required", "verification_required", "page_available", "unverified")
                        or xhs_session_st in ("login_required", "verification_required", "page_available", "unverified")
                        or "login_required" in dy_detail
                        or "verification_required" in dy_detail
                        or "login_required" in xhs_detail
                        or "verification_required" in xhs_detail
                        or "page_available" in xhs_detail
                        or "page_available" in dy_detail
                    )
                    if has_auth_challenge:
                        warnings.append(f"Provider auth challenge / unverified session: douyin={dy_detail}, xhs={xhs_detail}")
    except Exception as exc:
        checks["providers"] = {"status": "FAIL", "detail": f"Provider check exception: {exc}"}
        if probe:
            checks["live_providers"] = {"status": "FAIL", "detail": f"Live probe exception: {exc}"}
        is_ready = False
        warnings.append(f"Provider service exception: {exc}")

    # Determine overall gate status
    if not is_ready:
        has_fail = any(c.get("status") == "FAIL" for c in checks.values() if isinstance(c, dict))
        overall_status = "BLOCKED" if has_fail else "NOT_READY"
    elif warnings:
        overall_status = "DEGRADED"
    else:
        overall_status = "READY"

    # Clarify contracts:
    # - retrieval_ready: True ONLY when live retrieval is unblocked and operational without degradation
    # - workstation_ready: True when core infrastructure (database, worker pool, network) is operational
    # - is_ready: aligned with retrieval_ready (False when DEGRADED, BLOCKED, or NOT_READY)
    retrieval_ready = (overall_status == "READY")
    workstation_ready = (overall_status not in ("BLOCKED", "NOT_READY"))
    is_ready = retrieval_ready

    res: dict[str, Any] = {
        "status": overall_status,
        "is_ready": is_ready,
        "workstation_ready": workstation_ready,
        "retrieval_ready": retrieval_ready,
        "checks": checks,
        "warnings": warnings,
    }
    if probe and "live_probe_details" in locals():
        res["live_probe"] = live_probe_details
    return res
