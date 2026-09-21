import logging
import math
import threading
import time
from typing import Any

logger = logging.getLogger("observability")


class TelemetryService:
    """Collects in-memory latency percentiles, throughput counters, and error rates."""

    def __init__(self, max_samples_per_metric: int = 1000):
        self.max_samples = max_samples_per_metric
        self._counters: dict[str, int] = {}
        self._latencies: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.start_time = time.time()

    def record_counter(self, metric: str, count: int = 1) -> None:
        with self._lock:
            self._counters[metric] = self._counters.get(metric, 0) + count

    def record_latency(self, metric: str, duration_ms: float) -> None:
        with self._lock:
            samples = self._latencies.setdefault(metric, [])
            if len(samples) >= self.max_samples:
                samples.pop(0)
            samples.append(duration_ms)

    def get_percentiles(self, metric: str) -> dict[str, float]:
        with self._lock:
            samples = sorted(self._latencies.get(metric, []))

        if not samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0, "avg": 0.0, "min": 0.0, "max": 0.0, "count": 0}

        n = len(samples)

        def _p(p: float) -> float:
            idx = max(0, min(n - 1, math.ceil((p / 100.0) * n) - 1))
            return round(samples[idx], 2)

        return {
            "p50": _p(50),
            "p95": _p(95),
            "p99": _p(99),
            "avg": round(sum(samples) / n, 2),
            "min": round(samples[0], 2),
            "max": round(samples[-1], 2),
            "count": n,
        }

    def get_metrics_summary(self) -> dict[str, Any]:
        uptime_sec = round(time.time() - self.start_time, 1)

        with self._lock:
            counters = dict(self._counters)
            tracked_latencies = list(self._latencies.keys())

        histograms = {m: self.get_percentiles(m) for m in tracked_latencies}

        total_requests = counters.get("http_requests_total", 0)
        total_errors = counters.get("http_errors_total", 0)
        error_rate_pct = round((total_errors / total_requests * 100), 2) if total_requests > 0 else 0.0

        return {
            "uptime_seconds": uptime_sec,
            "error_rate_percent": error_rate_pct,
            "counters": counters,
            "latency_histograms_ms": histograms,
        }

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._latencies.clear()
            self.start_time = time.time()


# Global telemetry singleton
telemetry_service = TelemetryService()
