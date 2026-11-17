"""Measure isolated dashboard reads with synthetic media and queue records.

This is API throughput with one/two polling clients, not a GPU/render benchmark.
No Telegram, service launcher, or production workspace is used.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("test_evidence/dashboard/benchmark.json"))
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="dashboard_benchmark_") as temp:
        runtime = Path(temp)
        for key, folder in {"AUTODUB_WORKSPACE": "workspace", "AUTODUB_INPUT_DIR": "input",
                            "AUTODUB_OUTPUT_DIR": "output", "AUTODUB_DATA_DIR": "data",
                            "AUTODUB_MODEL_CACHE": "models", "AUTODUB_SHARED_ASSETS_DIR": "assets"}.items():
            os.environ[key] = str(runtime / folder)
            Path(os.environ[key]).mkdir()
        os.environ.update(AUTODUB_TESTING="1", AUTODUB_LOCAL_TOKEN="benchmark-only-token")
        sample = runtime / "sample.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=160x90:r=5",
                        "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.4", "-c:v", "mpeg4",
                        "-c:a", "aac", "-y", str(sample)], check=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for i in range(20):
            shutil.copyfile(sample, runtime / "input" / f"Fixture{i:02}.mp4")
            shutil.copyfile(sample, runtime / "output" / f"Dubbed_Fixture{i:02}.mp4")
        sys.path.insert(0, str(root / "backend"))
        import main as api
        from fastapi.testclient import TestClient
        from telegram_queue_monitor import MonitoredQueue
        import psutil
        queue = MonitoredQueue()
        queue.restore()
        for i in range(20):
            queue.put_nowait({"job_id": f"fixture_{i}", "type": "url",
                              "url": f"https://example.invalid/{i}", "filename": f"Queued{i:02}.mp4"})
        process = psutil.Process()
        endpoints = ["/api/status", "/api/queue", "/api/phoi?limit=20&offset=0", "/api/banve?limit=20&offset=0", "/api/logs", "/health/ready"]
        records = []
        def client_run(count):
            data = []
            with TestClient(api.app, headers={"X-Local-Control-Token": api.LOCAL_TOKEN}) as client:
                for _ in range(count):
                    for path in endpoints:
                        started = time.perf_counter()
                        response = client.get(path)
                        data.append((path, (time.perf_counter() - started) * 1000, response.status_code))
            return data
        cold = client_run(1)
        before = process.memory_info().rss
        for clients in (1, 2):
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=clients) as pool:
                measurements = [row for batch in pool.map(client_run, [args.samples] * clients) for row in batch]
            metrics = {}
            for path in endpoints:
                selected = [row for row in measurements if row[0] == path]
                times = sorted(row[1] for row in selected)
                quantiles = statistics.quantiles(times, n=100, method="inclusive")
                metrics[path] = {"n": len(times), "p50_ms": round(statistics.median(times), 2),
                                 "p95_ms": round(quantiles[94], 2), "p99_ms": round(quantiles[98], 2),
                                 "errors": sum(status >= 400 for _, _, status in selected)}
            records.append({"clients": clients, "duration_seconds": round(time.monotonic() - started, 2), "endpoints": metrics})
        result = {"scope": "isolated ASGI reads; 20 generated 0.4s videos and 20 queue records; no real GPU or Telegram",
                  "cold_ms": {path: round(ms, 2) for path, ms, _ in cold}, "runs": records,
                  "rss_before_bytes": before, "rss_after_bytes": process.memory_info().rss,
                  "system_available_bytes": psutil.virtual_memory().available, "soak_completed": False}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
