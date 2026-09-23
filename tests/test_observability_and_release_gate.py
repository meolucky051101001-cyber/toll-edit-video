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
