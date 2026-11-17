"""Read-only, bounded health checks; never starts/stops services or changes venvs."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("check deadline exhausted")
    return remaining


def _process(command, root, deadline):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               PIP_DISABLE_PIP_VERSION_CHECK="1")
    # subprocess.run reaps only the probe it created on timeout. No service PID
    # enumeration, process-tree termination or environment modification is used.
    result = subprocess.run(command, cwd=str(root), env=env, capture_output=True,
                            text=True, encoding="utf-8", errors="replace",
                            timeout=_remaining(deadline),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return result.returncode, result.stdout


def _version(root, python, expected, deadline):
    code, out = _process([str(python), "-B", "-I", "-c",
                          "import json,sys; print(json.dumps(list(sys.version_info[:3])))"], root, deadline)
    version = json.loads(out)
    valid = (code == 0 and isinstance(version, list) and len(version) == 3
             and all(type(part) is int for part in version))
    return valid and version[:2] == expected, "Python {} (expected {})".format(
        ".".join(map(str, version)), ".".join(map(str, expected)))


def _pip(root, python, deadline):
    code, _ = _process([str(python), "-B", "-m", "pip", "check"], root, deadline)
    return code == 0, "no broken requirements" if code == 0 else "dependency conflicts; run python -m pip check for details"


def _preflight(root, python, deadline):
    # The nested OCR probe has a shorter budget than this parent probe, so it
    # finishes before subprocess.run's timeout can reap the preflight process.
    budget = max(0.0, _remaining(deadline) - 0.75)
    entrypoint = Path(__file__).resolve().parents[1] / "backend" / "v1_preflight.py"
    code, out = _process([str(python), "-B", str(entrypoint),
                          "--project-root", str(root), "--interface", "all", "--read-only",
                          "--timeout-seconds", str(budget)], root, deadline)
    report = json.loads(out)
    checks = report.get("checks", [])
    passed = sum(isinstance(item, dict) and item.get("status") == "pass" for item in checks)
    ready = report.get("ready") is True
    ok = code == 0 and ready and len(checks) == 25 and passed == 25
    return ok, "ready={} {}/{} pass (read-only directory permission checks)".format(str(ready).lower(), passed, len(checks))


def _get_json(url, deadline, require_json=True):
    # Local endpoints only. Ignore proxy configuration to keep loopback local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=min(2.0, _remaining(deadline))) as response:
        body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise ValueError("health response too large")
        return json.loads(body) if require_json else None


def _metrics(value):
    if not isinstance(value, dict):
        return "unavailable"
    selected = {key: value[key] for key in ("used_gb", "total_gb", "percent")
                if type(value.get(key)) in (int, float)}
    return json.dumps(selected) if selected else "unavailable"


def _dashboard(port, deadline):
    state = _get_json(f"http://127.0.0.1:{port}/api/status", deadline)
    valid = isinstance(state.get("status"), str) and bool(state["status"]) and type(state.get("active")) is bool
    service_health = state.get("service_health")
    if service_health is not None:
        valid = valid and isinstance(service_health, dict) and service_health.get("ready") is True
    detail = "status={} active={} vram={} ram={}".format(
        json.dumps(state.get("status")), json.dumps(state.get("active")),
        _metrics(state.get("vram")), _metrics(state.get("ram")))
    if service_health is not None:
        detail += " components_ready={}".format(
            json.dumps(service_health.get("ready") if isinstance(service_health, dict) else False))
    return valid, detail


def _control(port, deadline):
    try:
        _get_json(f"http://127.0.0.1:{port}/health", deadline, require_json=False)
        return True, "HTTP /health reachable"
    except urllib.error.HTTPError as error:
        if error.code not in (404, 405):
            raise
    # Older tool_control versions have no /health endpoint. A TCP result is
    # explicitly labelled; it does not claim application-level health.
    with socket.create_connection(("127.0.0.1", port), timeout=min(1.0, _remaining(deadline))):
        return True, "TCP listening (no /health endpoint)"


def verify(root, python, expected=(3, 12), timeout=5.0, dashboard_port=8088, control_port=8090):
    deadline = time.monotonic() + timeout
    probes = [
        ("python", lambda: _version(root, python, list(expected), deadline)),
        ("pip check", lambda: _pip(root, python, deadline)),
        ("preflight", lambda: _preflight(root, python, deadline)),
        ("dashboard", lambda: _dashboard(dashboard_port, deadline)),
        ("tool control", lambda: _control(control_port, deadline)),
    ]

    def bounded(name, probe):
        try:
            ok, detail = probe()
            _remaining(deadline)
            return {"name": name, "ok": bool(ok), "detail": detail}
        except (TimeoutError, subprocess.TimeoutExpired):
            return {"name": name, "ok": False, "detail": "timeout; check incomplete (use --timeout 30 for cold imports)"}
        except Exception as error:
            # Do not echo process output, response bodies, environment or secrets.
            return {"name": name, "ok": False, "detail": type(error).__name__}

    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        futures = [executor.submit(bounded, name, probe) for name, probe in probes]
        return [future.result() for future in futures]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, help="Override probe interpreter, e.g. venv_py312 before cutover")
    parser.add_argument("--expected-python", default="3.12")
    parser.add_argument("--timeout", type=float, default=5.0, help="Total check budget in seconds (default: 5)")
    parser.add_argument("--dashboard-port", type=int, default=8088)
    parser.add_argument("--control-port", type=int, default=8090)
    args = parser.parse_args(argv)
    try:
        expected = tuple(map(int, args.expected_python.split(".")))
        if len(expected) != 2 or args.timeout <= 0 or not args.timeout < float("inf"):
            raise ValueError()
        if not all(1 <= port <= 65535 for port in (args.dashboard_port, args.control_port)):
            raise ValueError()
    except ValueError:
        parser.error("Use major.minor for --expected-python, a finite positive timeout and valid ports")
    root = args.project_root.resolve()
    python = args.python.resolve() if args.python else root / "backend" / "venv" / "Scripts" / "python.exe"
    started = time.monotonic()
    results = verify(root, python, expected, args.timeout, args.dashboard_port, args.control_port)
    for result in results:
        print("[{}] {}: {}".format("PASS" if result["ok"] else "FAIL", result["name"], result["detail"]))
    passed = all(result["ok"] for result in results)
    print("{} ({:.2f}s)".format("PASS" if passed else "FAIL", time.monotonic() - started))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
