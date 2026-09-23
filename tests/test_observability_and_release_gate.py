import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.services.observability import TelemetryService
from scripts.release_gate import check_database, check_environment


def test_telemetry_service_counters_and_percentiles():
    tele = TelemetryService()

    # Record counters
    tele.record_counter("search_queries", 5)
    tele.record_counter("search_queries", 3)
    assert tele.get_metrics_summary()["counters"]["search_queries"] == 8

    # Record latencies: 10, 20, 30, 40, 50, 60, 70, 80, 90, 100
    for val in [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]:
        tele.record_latency("search_latency_ms", val)

    p = tele.get_percentiles("search_latency_ms")
    assert p["count"] == 10
    assert p["min"] == 10.0
    assert p["max"] == 100.0
    assert p["avg"] == 55.0
    assert p["p50"] == 50.0
    assert p["p95"] == 100.0
    assert p["p99"] == 100.0


def test_telemetry_metrics_summary_error_rate():
    tele = TelemetryService()
    tele.record_counter("http_requests_total", 100)
    tele.record_counter("http_errors_total", 4)

    summary = tele.get_metrics_summary()
    assert summary["error_rate_percent"] == 4.0
    assert summary["uptime_seconds"] >= 0.0


def test_observability_api_metrics_endpoint():
    with TestClient(app) as client:
        res = client.get("/api/system/metrics")
        assert res.status_code == 200
        data = res.json()
        assert "uptime_seconds" in data
        assert "error_rate_percent" in data
        assert "counters" in data
        assert "latency_histograms_ms" in data


def test_release_readiness_api_endpoint():
    with TestClient(app) as client:
        res = client.get("/api/system/release-readiness")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in {"READY", "DEGRADED", "BLOCKED", "NOT_READY"}
        assert "checks" in data
        assert "database" in data["checks"]
        assert "job_queue" in data["checks"]
        assert "providers" in data["checks"]


def test_release_gate_functions():
    env_ok, env_msg = check_environment()
    assert env_ok is True
    assert "Python 3" in env_msg

    db_ok, db_msg = check_database()
    assert db_ok is True
    assert "SQLite" in db_msg


def test_release_readiness_probe_fail_closed_on_error():
    from unittest.mock import AsyncMock, patch

    from backend.services.observability import telemetry_service

    # Isolate telemetry service from earlier test runs to ensure clean test baseline
    telemetry_service.clear()
    telemetry_service.record_counter("http_requests_total", 10)

    with TestClient(app) as client:
        with patch.object(app.state.douyin_browser, "probe_live", new_callable=AsyncMock) as mock_dy, \
             patch.object(app.state.browser, "probe_live", new_callable=AsyncMock) as mock_xhs:
            # Case 1: Douyin succeeds, XHS throws exception -> Fail-closed
            mock_dy.return_value = (True, "HTTP 200: ready")
            mock_xhs.side_effect = RuntimeError("Simulated XHS network timeout")
            res = client.get("/api/system/release-readiness?probe=true")
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "BLOCKED"
            assert data["is_ready"] is False
            assert "live_providers" in data["checks"]
            assert data["checks"]["live_providers"]["status"] == "FAIL"

            # Case 2: Douyin throws exception, XHS succeeds -> Fail-closed
            mock_dy.side_effect = RuntimeError("Simulated Douyin connection refused")
            mock_xhs.side_effect = None
            mock_xhs.return_value = (True, "HTTP 200: ready")
            res2 = client.get("/api/system/release-readiness?probe=true")
            assert res2.status_code == 200
            data2 = res2.json()
            assert data2["status"] == "BLOCKED"
            assert data2["is_ready"] is False
            assert data2["checks"]["live_providers"]["status"] == "FAIL"

            # Case 3: Both succeed cleanly without auth challenges -> READY
            mock_dy.side_effect = None
            mock_dy.return_value = (True, "HTTP 200: ready")
            mock_xhs.return_value = (True, "HTTP 200: ready")
            res3 = client.get("/api/system/release-readiness?probe=true")
            assert res3.status_code == 200
            data3 = res3.json()
            assert data3["status"] == "READY"
            assert data3["is_ready"] is True
            assert data3["checks"]["live_providers"]["status"] == "PASS"
            assert data3["checks"]["providers"]["status"] == "PASS"
            assert not any("standby" in w.lower() for w in data3.get("warnings", []))

            # Case 4: Reachable (HTTP 200) but unverified session or auth challenge -> DEGRADED
            mock_dy.return_value = (True, "HTTP 200: verification_required")
            mock_xhs.return_value = (True, "HTTP 200: page_available")
            res4 = client.get("/api/system/release-readiness?probe=true")
            assert res4.status_code == 200
            data4 = res4.json()
            assert data4["status"] == "DEGRADED"
            assert data4["is_ready"] is True
            assert data4["checks"]["live_providers"]["status"] == "PASS"
            assert data4["checks"]["providers"]["status"] == "PASS"
            assert any("auth challenge" in w.lower() or "unverified session" in w.lower() for w in data4.get("warnings", []))


@pytest.mark.anyio
async def test_browser_service_probe_fresh_request_not_stale_status():
    from unittest.mock import AsyncMock, MagicMock

    from backend.services.browser_service import BrowserService

    service = BrowserService(headless=True)
    mock_context = MagicMock()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_context.new_page = AsyncMock(return_value=mock_page)

    service.context = mock_context
    service.ensure_open = AsyncMock(return_value=mock_page)
    service.inspect = AsyncMock(return_value="ready")

    # 1. First probe: Server returns HTTP 200
    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)

    ok1, desc1 = await service.probe_live()
    assert ok1 is True
    assert "HTTP 200" in desc1
    assert service.last_http_status == 200
    assert mock_page.close.await_count == 1

    # 2. Second probe on same service instance: Server returns HTTP 503
    mock_resp_503 = MagicMock(status=503)
    mock_page.goto = AsyncMock(return_value=mock_resp_503)

    ok2, desc2 = await service.probe_live()
    # Must NOT reuse stale 200 from first probe!
    assert ok2 is False
    assert "HTTP 503" in desc2
    assert service.last_http_status == 503
    assert mock_page.close.await_count == 2


@pytest.mark.anyio
async def test_douyin_browser_service_probe_fresh_request_not_stale_status():
    from unittest.mock import AsyncMock, MagicMock

    from backend.services.douyin_browser_service import DouyinBrowserService

    service = DouyinBrowserService(headless=True)
    mock_context = MagicMock()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_context.new_page = AsyncMock(return_value=mock_page)

    service.context = mock_context
    service.ensure_open = AsyncMock(return_value=mock_page)
    service.inspect = AsyncMock(return_value="ready")

    # 1. First probe: Server returns HTTP 200
    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)

    ok1, desc1 = await service.probe_live()
    assert ok1 is True
    assert "HTTP 200" in desc1
    assert service.last_http_status == 200
    assert mock_page.close.await_count == 1

    # 2. Second probe on same service instance: Server returns HTTP 503
    mock_resp_503 = MagicMock(status=503)
    mock_page.goto = AsyncMock(return_value=mock_resp_503)

    ok2, desc2 = await service.probe_live()
    # Must NOT reuse stale 200 from first probe!
    assert ok2 is False
    assert "HTTP 503" in desc2
    assert service.last_http_status == 503
    assert mock_page.close.await_count == 2

