"""Safely stop Tool V1 Telegram bot without touching Tool V2 or other python processes."""
import json
import os
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

def stop_v1_bot():
    backend = Path(__file__).resolve().parent
    v1_root = backend.parent
    control_dir = v1_root / "workspace" / "control"
    v1_json = control_dir / "v1.json"

    target_pid = None
    if v1_json.is_file():
        try:
            data = json.loads(v1_json.read_text(encoding="utf-8"))
            target_pid = data.get("pid")
        except Exception:
            pass

    stopped = False
    if psutil is not None:
        target_procs = []
        script_path = str(backend / "telegram_bot.py").lower()
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                name = (p.info["name"] or "").lower()
                if not name.startswith("python"):
                    continue
                cmdline = [str(arg).lower() for arg in (p.cmdline() or [])]
                if script_path in cmdline or any("telegram_bot.py" in arg and str(backend).lower() in " ".join(cmdline) for arg in cmdline):
                    target_procs.append(p)
                elif target_pid and p.pid == target_pid:
                    target_procs.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        for p in target_procs:
            try:
                p.terminate()
                stopped = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if target_procs:
            _, alive = psutil.wait_procs(target_procs, timeout=3.0)
            for p in alive:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    elif target_pid:
        try:
            os.system(f"taskkill /PID {target_pid} /F >nul 2>&1")
            stopped = True
        except Exception:
            pass

    try:
        if v1_json.is_file():
            data = json.loads(v1_json.read_text(encoding="utf-8"))
            data["pid"] = None
            data["polling"] = False
            data["busy"] = False
            v1_json.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass

    if stopped:
        print("[Tool V1] Telegram bot has been stopped successfully.")
    else:
        print("[Tool V1] No active Tool V1 Telegram bot process was found.")

if __name__ == "__main__":
    stop_v1_bot()
