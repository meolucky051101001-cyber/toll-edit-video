"""Run one Tool V1 service independently under Windows Task Scheduler."""
import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", choices=("dashboard", "telegram"), required=True)
    service = parser.parse_args().service
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    mutex = kernel.CreateMutexW(None, False, "Local\\AutoDubV1_" + service)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        return
    logs = ROOT.parent / "workspace" / "service_logs"
    logs.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(service)
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(logs / (service + "_supervisor.log"),
                                 maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    python = ROOT / "venv" / "Scripts" / "python.exe"
    script = ROOT / ("main.py" if service == "dashboard" else "telegram_bot.py")
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    delay = 5
    while True:
        started = time.monotonic()
        output_path = logs / (service + ".log")
        if output_path.exists() and output_path.stat().st_size > 10_000_000:
            os.replace(output_path, logs / (service + ".previous.log"))
        try:
            with output_path.open("ab", buffering=0) as output:
                process = subprocess.Popen(
                    [str(python), "-u", str(script)], cwd=str(ROOT), env=environment,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                logger.info("Started PID %s", process.pid)
                code = process.wait()
                logger.warning("Exited code %s; restarting", code)
        except Exception:
            logger.exception("Service launch failed")
        delay = 5 if time.monotonic() - started > 120 else min(delay * 2, 60)
        time.sleep(delay)


if __name__ == "__main__":
    main()
