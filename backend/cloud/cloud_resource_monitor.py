"""Bounded, best-effort RAM/GPU telemetry for sessions already owned by Cloud jobs."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .colab_bridge import exec_argv
from .cloud_security import CloudValidationError, validate_session_name

logger = logging.getLogger("cloud.resources")

_CACHE_TTL_SECONDS = 15.0
_MAX_SESSIONS = 12
_MAX_PROBES = 4
_MARKER = "CLOUD_RESOURCE_SAMPLE="

_REMOTE_SAMPLE_SOURCE = r'''import json, os, subprocess
from pathlib import Path

def process_group_ram():
    for stage, script in (("pipeline", "full_pipeline.py"), ("setup", "setup_env.py")):
        try:
            run = json.loads(Path(f"/content/.autodub_{stage}.pid.json").read_text())
            root = run["child_pid"]
            if type(root) is not int or root <= 1:
                continue
            status = Path(f"/content/.autodub_{stage}.status.json")
            if status.is_file():
                finished = json.loads(status.read_text())
                if finished.get("done") and finished.get("run_id") == run.get("run_id"):
                    continue
            root_cmd = (Path("/proc") / str(root) / "cmdline").read_bytes().split(b"\0")
            if len(root_cmd) < 2 or os.path.basename(root_cmd[1]) != script.encode():
                continue
            if os.getpgid(root) != root:
                continue
        except (OSError, ValueError, TypeError, KeyError):
            continue
        total = 0
        count = 0
        for proc in Path("/proc").iterdir():
            if not proc.name.isdecimal():
                continue
            try:
                if os.getpgid(int(proc.name)) != root:
                    continue
                with (proc / "smaps_rollup").open(encoding="ascii") as source:
                    pss = next((line for line in source if line.startswith("Pss:")), "")
                if pss:
                    total += int(pss.split()[1]) * 1024
                    count += 1
            except (ProcessLookupError, FileNotFoundError):
                continue
            except (OSError, ValueError, IndexError):
                return None
        if count and total:
            return {"used_bytes": total, "stage": stage}
    return None

result = {"ram_total_bytes": None, "ram_used_bytes": None, "gpu_status": "unavailable", "gpus": []}
result["job_ram"] = process_group_ram()
try:
    mem = {}
    with open("/proc/meminfo", "r", encoding="ascii") as source:
        for line in source:
            key, _, value = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                mem[key] = int(value.strip().split()[0]) * 1024
    total = mem.get("MemTotal")
    available = mem.get("MemAvailable")
    if total and available is not None:
        available = max(0, min(total, available))
        result["ram_total_bytes"] = total
        result["ram_used_bytes"] = total - available
except (OSError, ValueError, IndexError):
    pass
try:
    proc = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=4, check=False,
    )
    for line in (proc.stdout or "").splitlines()[:4]:
        columns = [part.strip() for part in line.split(",")]
        if len(columns) != 4:
            continue
        try:
            used_mb, total_mb = (int(float(value)) for value in columns[1:3])
        except ValueError:
            continue
        try:
            utilization = max(0, min(100, int(float(columns[3]))))
        except ValueError:
            utilization = None
        if total_mb <= 0:
            continue
        result["gpus"].append({
            "name": columns[0][:80], "used_bytes": max(0, used_mb) * 1048576,
            "total_bytes": total_mb * 1048576,
            "utilization_percent": utilization,
        })
    if result["gpus"]:
        result["gpu_status"] = "available"
    elif proc.returncode == 0 or "no devices were found" in (proc.stderr or "").lower():
        result["gpu_status"] = "no_gpu"
    else:
        result["gpu_status"] = "unavailable"
except (OSError, subprocess.SubprocessError, ValueError):
    result["gpu_status"] = "unavailable"
print("CLOUD_RESOURCE_SAMPLE=" + json.dumps(result, separators=(",", ":")))
'''
_REMOTE_SAMPLE = "exec(" + repr(_REMOTE_SAMPLE_SOURCE) + ")"


def _parse_sample(output: str) -> Optional[Dict[str, Any]]:
    encoded = next((line[len(_MARKER):] for line in reversed(str(output or "").splitlines()) if line.startswith(_MARKER)), None)
    if not encoded:
        return None
    try:
        raw = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    gpu_status = raw.get("gpu_status")
    result: Dict[str, Any] = {
        "ram": None,
        "gpu_status": gpu_status if gpu_status in ("available", "no_gpu", "unavailable") else "unavailable",
        "gpus": [],
    }
    total, used = raw.get("ram_total_bytes"), raw.get("ram_used_bytes")
    if isinstance(total, int) and isinstance(used, int) and 0 <= used <= total and total > 0:
        result["ram"] = {"used_bytes": used, "total_bytes": total, "percent_used": round(100 * used / total, 1)}
    job_ram = raw.get("job_ram")
    if isinstance(job_ram, dict):
        job_used = job_ram.get("used_bytes")
        stage = job_ram.get("stage")
        if type(job_used) is int and job_used > 0 and stage in ("setup", "pipeline"):
            result["job_ram"] = {"used_bytes": job_used, "stage": stage}
    gpus = raw.get("gpus")
    if isinstance(gpus, list):
        for gpu in gpus[:4]:
            if not isinstance(gpu, dict):
                continue
            gpu_total, gpu_used = gpu.get("total_bytes"), gpu.get("used_bytes")
            utilization = gpu.get("utilization_percent")
            if not (isinstance(gpu_total, int) and isinstance(gpu_used, int) and gpu_total > 0 and 0 <= gpu_used <= gpu_total):
                continue
            if not isinstance(utilization, int) or not 0 <= utilization <= 100:
                utilization = None
            name = gpu.get("name") if isinstance(gpu.get("name"), str) else "GPU"
            result["gpus"].append({
                "name": name[:80],
                "used_bytes": gpu_used,
                "total_bytes": gpu_total,
                "percent_used": round(100 * gpu_used / gpu_total, 1),
                "utilization_percent": utilization,
            })
    return result


class CloudSessionResourceMonitor:
    """Return cached telemetry promptly and probe only DB-owned sessions in a bounded pool."""

    def __init__(
        self,
        probe: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        *,
        max_workers: int = _MAX_PROBES,
        cache_ttl_seconds: float = _CACHE_TTL_SECONDS,
        max_sessions: int = _MAX_SESSIONS,
    ) -> None:
        self._probe = probe or self._probe_session
        self._cache_ttl = max(1.0, float(cache_ttl_seconds))
        self._max_sessions = max(1, int(max_sessions))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(int(max_workers), _MAX_PROBES)),
            thread_name_prefix="cloud-resource",
        )
        self._lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._in_flight: set[str] = set()
        self._closed = False

    @staticmethod
    def _probe_session(session_name: str) -> Optional[Dict[str, Any]]:
        try:
            ok, output = exec_argv(
                session_name,
                ["python3", "-c", _REMOTE_SAMPLE],
                timeout=12,
            )
        except Exception as exc:
            logger.debug("Cloud resource probe unavailable for %s (%s)", session_name, type(exc).__name__)
            return None
        if not ok:
            return None
        return _parse_sample(output)

    def _refresh(self, session_name: str, owner: str) -> None:
        sampled_at = time.time()
        try:
            sample = self._probe(session_name)
        except Exception as exc:
            logger.debug("Cloud resource sample failed for %s (%s)", session_name, type(exc).__name__)
            sample = None
        completed_at = time.time()
        with self._lock:
            entry = self._cache.setdefault((session_name, owner), {})
            entry["checked_at"] = completed_at
            if sample is not None:
                entry["sample"] = sample
                entry["sampled_at"] = sampled_at
                entry["available"] = True
            elif "sample" not in entry:
                entry["available"] = False
            self._in_flight.discard(session_name)

    def snapshot(self, session_names: Iterable[str], *, owners: Optional[Mapping[str, str]] = None,
                 now: Optional[float] = None) -> List[Dict[str, Any]]:
        check_now = time.time() if now is None else float(now)
        names: List[str] = []
        seen = set()
        for raw_name in session_names:
            try:
                name = validate_session_name(raw_name)
            except (CloudValidationError, TypeError):
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
            if len(names) >= self._max_sessions:
                break

        with self._lock:
            if self._closed:
                return [{"session_name": name, "state": "unavailable", "ram": None, "gpus": [], "sampled_at": None, "stale": True} for name in names]

            owned = owners or {}
            keys = [(name, owned.get(name, "")) for name in names]
            visible_keys = set(keys)
            for cached_key in list(self._cache):
                if cached_key not in visible_keys:
                    self._cache.pop(cached_key, None)

            for name, owner in keys:
                cached = self._cache.get((name, owner), {})
                checked_at = float(cached.get("checked_at", 0.0))
                if name not in self._in_flight and check_now - checked_at >= self._cache_ttl and len(self._in_flight) < _MAX_PROBES:
                    self._in_flight.add(name)
                    try:
                        self._executor.submit(self._refresh, name, owner)
                    except RuntimeError:
                        self._in_flight.discard(name)
                        self._closed = True
                        break

            result = []
            for name, owner in keys:
                cached = self._cache.get((name, owner), {})
                sampled_at = cached.get("sampled_at")
                sample = cached.get("sample") or {}
                age = max(0.0, check_now - float(sampled_at)) if sampled_at is not None else None
                is_stale = age is None or age > self._cache_ttl * 2
                if cached.get("available") is False:
                    state = "unavailable"
                elif sampled_at is None:
                    state = "pending" if checked_at <= 0 or name in self._in_flight else "unavailable"
                elif is_stale:
                    state = "stale"
                else:
                    state = "available"
                result.append({
                    "session_name": name,
                    "state": state,
                    "ram": sample.get("ram"),
                    "job_ram": sample.get("job_ram"),
                    "gpus": list(sample.get("gpus", [])),
                    "gpu_status": sample.get("gpu_status", "unavailable"),
                    "sampled_at": sampled_at,
                    "age_seconds": round(age, 1) if age is not None else None,
                    "stale": is_stale,
                    "probe_pending": name in self._in_flight,
                })
            return result

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)


_monitor = CloudSessionResourceMonitor()


def get_cloud_session_resource_snapshot(session_names: Iterable[str], *,
                                        owners: Optional[Mapping[str, str]] = None) -> List[Dict[str, Any]]:
    return _monitor.snapshot(session_names, owners=owners)

