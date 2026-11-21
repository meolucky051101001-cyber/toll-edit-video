from __future__ import annotations

import json
import threading
import time

from cloud import cloud_resource_monitor as resources


def test_parse_remote_ram_and_gpu_sample():
    payload = {
        "ram_total_bytes": 16 * 1024**3,
        "ram_used_bytes": 10 * 1024**3,
        "gpu_status": "available",
        "gpus": [{
            "name": "NVIDIA Tesla T4",
            "used_bytes": 4 * 1024**3,
            "total_bytes": 16 * 1024**3,
            "utilization_percent": 53,
        }],
    }

    result = resources._parse_sample("diagnostic output\nCLOUD_RESOURCE_SAMPLE=" + json.dumps(payload))

    assert result == {
        "ram": {"used_bytes": 10 * 1024**3, "total_bytes": 16 * 1024**3, "percent_used": 62.5},
        "gpu_status": "available",
        "gpus": [{
            "name": "NVIDIA Tesla T4",
            "used_bytes": 4 * 1024**3,
            "total_bytes": 16 * 1024**3,
            "percent_used": 25.0,
            "utilization_percent": 53,
        }],
    }


def test_parse_remote_sample_marks_gpu_absence_separately():
    payload = {"ram_total_bytes": 8, "ram_used_bytes": 3, "gpu_status": "no_gpu", "gpus": []}

    result = resources._parse_sample("CLOUD_RESOURCE_SAMPLE=" + json.dumps(payload))

    assert result["gpu_status"] == "no_gpu"
    assert result["gpus"] == []
    assert result["ram"]["percent_used"] == 37.5
    assert resources._parse_sample("not telemetry") is None


def test_snapshot_deduplicates_valid_owned_sessions_and_returns_while_probe_runs():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def probe(name):
        calls.append(name)
        started.set()
        release.wait(timeout=1)
        return {"ram": {"used_bytes": 1, "total_bytes": 2, "percent_used": 50.0}, "gpu_status": "no_gpu", "gpus": []}

    monitor = resources.CloudSessionResourceMonitor(probe, max_workers=1, cache_ttl_seconds=10)
    try:
        begin = time.monotonic()
        first = monitor.snapshot(["job-session", "job-session", "../invalid"])
        assert time.monotonic() - begin < 0.2
        assert [row["session_name"] for row in first] == ["job-session"]
        assert first[0]["state"] == "pending"
        assert started.wait(timeout=0.5)
        release.set()

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and monitor.snapshot(["job-session"])[0]["state"] != "available":
            time.sleep(0.01)

        result = monitor.snapshot(["job-session"])
        assert result[0]["state"] == "available"
        assert result[0]["ram"]["percent_used"] == 50.0
        assert result[0]["gpu_status"] == "no_gpu"
        assert calls == ["job-session"]
    finally:
        release.set()
        monitor.close(wait=True)


def test_snapshot_caps_remote_probe_concurrency():
    lock = threading.Lock()
    running = 0
    peak = 0

    def probe(_name):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.08)
        with lock:
            running -= 1
        return {"ram": None, "gpu_status": "unavailable", "gpus": []}

    monitor = resources.CloudSessionResourceMonitor(probe, max_workers=20)
    try:
        rows = monitor.snapshot([f"session-{index}" for index in range(12)])
        assert len(rows) == 12
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and running:
            time.sleep(0.01)
        assert peak <= resources._MAX_PROBES
    finally:
        monitor.close(wait=True)


def test_probe_uses_only_read_only_resource_command_and_no_session_creation(monkeypatch):
    calls = []
    payload = {"ram_total_bytes": 8, "ram_used_bytes": 3, "gpu_status": "unavailable", "gpus": []}

    def fake_exec(session_name, argv, *, timeout):
        calls.append((session_name, argv, timeout))
        return True, "CLOUD_RESOURCE_SAMPLE=" + json.dumps(payload)

    monkeypatch.setattr(resources, "exec_argv", fake_exec)

    result = resources.CloudSessionResourceMonitor._probe_session("owned-session")

    assert result["ram"]["used_bytes"] == 3
    assert calls[0][0] == "owned-session"
    assert calls[0][1][:3] == ["python3", "-c", resources._REMOTE_SAMPLE]
    assert not any(ord(char) < 32 for char in resources._REMOTE_SAMPLE)
    compile(resources._REMOTE_SAMPLE, "<cloud-resource-probe>", "exec")
    assert calls[0][2] == 12


def test_job_ram_and_vram_survive_missing_gpu_utilization():
    sample = resources._parse_sample("CLOUD_RESOURCE_SAMPLE=" + json.dumps({
        "job_ram": {"used_bytes": 1234, "stage": "pipeline"},
        "gpu_status": "available",
        "gpus": [{"name": "NVIDIA L4", "used_bytes": 2, "total_bytes": 8}],
    }))
    assert sample["job_ram"] == {"used_bytes": 1234, "stage": "pipeline"}
    assert sample["gpus"][0]["utilization_percent"] is None
    assert sample["gpus"][0]["percent_used"] == 25.0


def test_warm_session_handoff_drops_old_owners_sample():
    monitor = resources.CloudSessionResourceMonitor(
        lambda _: {"job_ram": {"used_bytes": 1234, "stage": "pipeline"}}, max_workers=1,
    )
    assigned = ["shared-session"]
    try:
        older = {"shared-session": "job-1:1"}
        monitor.snapshot(assigned, owners=older)
        deadline = time.monotonic() + 1
        while monitor.snapshot(assigned, owners=older)[0]["state"] != "available":
            assert time.monotonic() < deadline
            time.sleep(.01)
        newer = monitor.snapshot(assigned, owners={"shared-session": "job-2:1"})[0]
        assert newer["state"] == "pending"
        assert newer["job_ram"] is None
        assert newer["gpus"] == []
    finally:
        monitor.close(wait=True)


def test_snapshot_maps_sample_to_active_job_and_filters_old_stage(monkeypatch):
    from backend.cloud import cloud_routes

    def sample(names, *, owners):
        assert names == ["active-session", "warm-session"]
        assert owners == {"active-session": "job-1:2", "warm-session": "warm:job-0"}
        return [
            {"session_name": name, "job_ram": {"used_bytes": 1234, "stage": "setup"},
             "gpus": [{"name": "NVIDIA L4"}]} for name in names
        ]

    monkeypatch.setattr(cloud_routes, "get_cloud_session_resource_snapshot", sample)
    active, warm = cloud_routes._read_cloud_session_resources(
        [{"session_name": "active-session", "job_id": "job-1", "attempt": 2,
          "video_name": "video.mp4", "gpu_type": "T4", "state": "processing"}],
        [{"session_name": "warm-session", "source_job_id": "job-0", "gpu_type": "L4"}],
    )
    assert active["job_id"] == "job-1" and active["job_name"] == "video.mp4"
    assert active["requested_gpu"] == "T4" and active["gpus"][0]["name"] == "NVIDIA L4"
    assert active["job_ram"] is None
    assert warm["warm"] and warm["job_ram"] is None
