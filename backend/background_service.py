"""Run one Tool V1 service independently under Windows Task Scheduler."""
import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import time
import urllib.request

from core.logging_setup import RedactingFormatter

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
DASHBOARD_STATUS_URL = "http://127.0.0.1:8088/api/status"


def dashboard_is_healthy(timeout: float = 3.0) -> bool:
    """A healthy 8088 listener means a dashboard is already serving; spawning again only causes WinError 10048 loops."""
    try:
        with urllib.request.urlopen(DASHBOARD_STATUS_URL, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def dashboard_smoke_test(python: Path) -> str:
    """Catch syntax errors and broken router imports before spawning, so the supervisor does not restart-loop on bad code."""
    commands = (
        [str(python), "-m", "py_compile", "main.py", "workflow_api.py", "script_api.py"],
        [str(python), "-c", "import workflow_api, script_api"],
    )
    for command in commands:
        try:
            result = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return "quá thời gian: " + " ".join(command[-2:])
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            return output[-800:] or f"exit code {result.returncode}"
    return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=("dashboard", "telegram", "scriptbot"), required=True)
    service = parser.parse_args().service
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, "Local\\AutoDubV1_" + service)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        return
    bs_logs = WORKSPACE / "bot_system" / "service_logs"
    logs = bs_logs if (bs_logs.is_dir() or (WORKSPACE / "bot_system").is_dir()) else (WORKSPACE / "service_logs")
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(logs / (service + "_supervisor.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    python = ROOT / "venv" / "Scripts" / "python.exe"
    scripts = {
        "dashboard": "main.py",
        "telegram": "telegram_bot.py",
        "scriptbot": "telegram_script_bot.py",
    }
    script = ROOT / scripts[service]
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    # Keep the control plane available independently of either dashboard.
    try:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _sock:
            _sock.settimeout(0.3)
            if _sock.connect_ex(('127.0.0.1', 8090)) != 0:
                control_python = ROOT / "venv" / "Scripts" / "pythonw.exe"
                control_script = ROOT / "tool_control.py"
                subprocess.Popen([str(control_python), str(control_script)],
                                 cwd=str(control_script.parent), creationflags=subprocess.CREATE_NO_WINDOW,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    bs_control = WORKSPACE / "bot_system" / "control"
    control_dir = bs_control if bs_control.is_dir() else (WORKSPACE / "control")
    control_pause = control_dir / "v1.pause"
    delay = 5
    while True:
        if service != "dashboard" and control_pause.exists():
            time.sleep(2)
            continue
        if service == "dashboard":
            if dashboard_is_healthy():
                logger.info("Dashboard đã chạy khỏe trên 8088 — bỏ qua spawn trùng.")
                time.sleep(10)
                continue
            smoke_problem = dashboard_smoke_test(python)
            if smoke_problem:
                logger.error("Smoke test dashboard thất bại, không spawn: %s", smoke_problem)
                time.sleep(60)
                continue
        started = time.monotonic()
        output_path = logs / (service + ".log")
        if output_path.exists() and output_path.stat().st_size > 10_000_000:
            os.replace(output_path, logs / (service + ".previous.log"))
        try:
            with output_path.open("ab", buffering=0) as output:
                process = subprocess.Popen(
                    [str(python), "-u", str(script)], cwd=str(ROOT), env=environment,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.NORMAL_PRIORITY_CLASS))
                logger.info("Started PID %s", process.pid)
                code = process.wait()
                logger.warning("Exited code %s; restarting", code)
        except Exception:
            logger.exception("Service launch failed")
        delay = 5 if time.monotonic() - started > 120 else min(delay * 2, 60)
        time.sleep(delay)


if __name__ == "__main__":
    main()
