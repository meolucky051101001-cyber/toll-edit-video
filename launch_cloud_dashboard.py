"""Launch the local Cloud Dashboard without a console or application imports."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Mapping
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent
STARTUP_TIMEOUT_SECONDS = 45.0


def _is_valid_pid(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def resolve_python(root: Path, environment: Mapping[str, str]) -> Path:
    override = str(environment.get("AUTODUB_CLOUD_PYTHON", "")).strip()
    if os.name == "nt":
        override = override.strip('"')
    executable = Path(override) if override else root / "backend" / "venv" / "Scripts" / "python.exe"
    if not executable.is_absolute():
        executable = root / executable
    executable = executable.resolve()
    if not executable.is_file():
        raise FileNotFoundError("Explicit Cloud Python runtime is missing")
    return executable


def resolve_log_directory(root: Path, environment: Mapping[str, str]) -> Path:
    configured = str(environment.get("AUTODUB_WORKSPACE", "")).strip()
    workspace = Path(configured) if configured else root / "workspace"
    if not workspace.is_absolute():
        workspace = root / workspace
    return workspace.resolve() / "cloud_desktop" / "logs"


def read_startup_report(path: Path, expected_pid: int) -> str | None:
    """Trust only this launch's small report; never echo its contents."""
    if not _is_valid_pid(expected_pid):
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
            return None
        report = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(report, dict):
            return None
        pid, ppid = report.get("pid"), report.get("ppid")
        if not _is_valid_pid(pid):
            return None
        # Windows venv Scripts\python.exe is a redirector stub whose child runs
        # the real interpreter (ppid == process.pid, pid != process.pid).
        if pid != expected_pid and not (_is_valid_pid(ppid) and ppid == expected_pid):
            return None
        status, code = report.get("status"), report.get("code")
        if status == "ready" and not isinstance(code, bool) and code == 0:
            return "ready"
        if status == "failed":
            return "failed"
    except (OSError, ValueError, TypeError):
        pass
    return None


def wait_for_startup(process, report_path: Path, *, timeout: float = STARTUP_TIMEOUT_SECONDS) -> bool:
    """Observe readiness or exit without terminating any process."""
    deadline = time.monotonic() + max(0.0, min(float(timeout), STARTUP_TIMEOUT_SECONDS))
    while True:
        status = read_startup_report(report_path, process.pid)
        if status is not None:
            return status == "ready" and process.poll() in (None, 0)
        if process.poll() is not None:
            # A verified duplicate may write ready and immediately exit zero.
            return process.returncode == 0 and read_startup_report(report_path, process.pid) == "ready"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def show_startup_error(log_directory: Path) -> None:
    """Display only a generic error and log location, including under pythonw."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "Không thể mở Cloud Dashboard.\nNhật ký: " + str(log_directory),
            "Tool V1 Cloud Dashboard", 0x10)
    except Exception:
        # Pythonw may have no stdout/stderr. Do not print or expose exceptions.
        pass


def launch_dashboard(*, root: Path = PROJECT_ROOT, environment: Mapping[str, str] | None = None,
                     timeout: float = STARTUP_TIMEOUT_SECONDS, platform_name: str | None = None) -> int:
    """Start the exact checkout runtime and return after bounded readiness."""
    root = Path(root).resolve()
    environment = dict(os.environ if environment is None else environment)
    log_directory = resolve_log_directory(root, environment)
    try:
        executable = resolve_python(root, environment)
        runner = root / "run_cloud_node.py"
        if not runner.is_file():
            raise FileNotFoundError("Cloud runner is missing")
        log_directory.mkdir(parents=True, exist_ok=True)
        launch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
        report_path = log_directory / (launch_id + ".startup.json")
        stdout_path = log_directory / (launch_id + ".stdout.log")
        stderr_path = log_directory / (launch_id + ".stderr.log")
        flags = 0
        if (os.name if platform_name is None else platform_name) == "nt":
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
        command = [str(executable), "-B", "-X", "utf8", "-u", str(runner), "--port", "8089",
                   "--no-worker", "--startup-report", str(report_path)]
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = subprocess.Popen(command, cwd=root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       creationflags=flags, close_fds=True)
        if wait_for_startup(process, report_path, timeout=timeout):
            return 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    show_startup_error(log_directory)
    return 1


def main() -> int:
    return launch_dashboard()


if __name__ == "__main__":
    raise SystemExit(main())
