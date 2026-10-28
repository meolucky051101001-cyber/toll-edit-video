"""Giám sát vận hành Tool V1: sức khỏe dịch vụ, cảnh báo và tự khởi động dịch vụ thiếu.

Mặc định chỉ đọc và ghi `workspace/bot_system/ops_health.json`. Chỉ khi bật
`--heal` (hoặc `AUTODUB_OPS_AUTOHEAL=1`) mới chạy Scheduled Task đang thiếu.
Watchdog không kill tiến trình đang sống; bot có heartbeat cũ chỉ được cảnh báo.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(PROJECT_ROOT / "workspace")))
DASHBOARD_URL = "http://127.0.0.1:8088/api/status"
CONTROL_URL = "http://127.0.0.1:8090/status"
HEARTBEAT_MAX_AGE = 30.0
ALERT_REPEAT_SECONDS = 1800
HEAL_COOLDOWN_SECONDS = 300
HEAL_MAX_PER_HOUR = 3

TASKS = {
    "control": "AutoDub-control",
    "dashboard": "AutoDubV1-dashboard",
    "telegram": "AutoDubV1-telegram",
    "scriptbot": "AutoDubV1-scriptbot",
    "downloader": "AutoDubV1-telebot-downloader",
}
LABELS = {
    "dashboard": "Dashboard 8088",
    "control": "Control plane 8090",
    "telegram": "Telegram bot",
    "scriptbot": "Script bot",
    "downloader": "Bot tải video",
    "disk_c": "Ổ C:",
    "disk_d": "Ổ D:",
}
SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}

logger = logging.getLogger("ops_watchdog")
_MUTEX_HANDLE = None


def _state_path(workspace: Path) -> Path:
    return Path(workspace) / "bot_system" / "ops_health.json"


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        return json.load(response)


def _pids_for(script: Path) -> list[int]:
    import psutil

    target = str(script).lower()
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
        except (psutil.Error, OSError):
            continue
        if target in cmdline:
            pids.append(proc.info["pid"])
    return pids


def _pid_alive(pid) -> bool:
    import psutil

    try:
        proc = psutil.Process(int(pid))
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ValueError, TypeError, OSError):
        return False


def _read_telemetry(workspace: Path) -> dict:
    best: dict = {}
    best_at = -1.0
    for path in (
        Path(workspace) / "control" / "v1.json",
        Path(workspace) / "bot_system" / "control" / "v1.json",
    ):
        data = _load_json(path)
        try:
            at = float(data.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        if at > best_at:
            best_at = at
            best = data
    return best


def _read_job_status(workspace: Path) -> dict:
    for path in (
        Path(workspace) / "bot_system" / "job_status.json",
        Path(workspace) / "job_status.json",
    ):
        data = _load_json(path)
        if data:
            return data
    return {}


def _flag_exists(workspace: Path, name: str) -> bool:
    return any(
        candidate.exists()
        for candidate in (
            Path(workspace) / "control" / name,
            Path(workspace) / "bot_system" / "control" / name,
        )
    )


def _v1_paused(workspace: Path) -> bool:
    return _flag_exists(workspace, "v1.pause")


def _dashboard_component(now: float) -> dict:
    try:
        data = _http_json(DASHBOARD_URL)
        return {
            "severity": "ok",
            "status": "ok",
            "active": bool(data.get("active")),
            "detail": "đang phản hồi",
        }
    except Exception as exc:
        return {
            "severity": "critical",
            "status": "down",
            "detail": f"không phản hồi ({type(exc).__name__})",
        }


def _control_component(now: float) -> dict:
    try:
        data = _http_json(CONTROL_URL)
        return {
            "severity": "ok",
            "status": "ok",
            "detail": f"{len(data) if isinstance(data, list) else 1} tool",
        }
    except Exception as exc:
        return {
            "severity": "critical",
            "status": "down",
            "detail": f"không phản hồi ({type(exc).__name__})",
        }


def _telegram_component(workspace: Path, now: float, paused: bool) -> dict:
    pids = _pids_for(ROOT / "telegram_bot.py")
    data = _read_telemetry(workspace)
    try:
        at = float(data.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    age = max(0.0, now - at) if at else None
    fresh = bool(at) and age is not None and age <= HEARTBEAT_MAX_AGE
    pid_match = bool(data.get("pid")) and data.get("pid") in pids
    comp = {
        "pids": pids,
        "heartbeat_age": round(age, 1) if age is not None else None,
        "polling": bool(data.get("polling")),
        "busy": bool(data.get("busy")),
        "queue": data.get("queue"),
    }
    if paused:
        comp.update(severity="ok", status="disabled", detail="tạm dừng theo v1.pause")
    elif not pids:
        comp.update(severity="critical", status="down", detail="không có tiến trình")
    elif not fresh:
        comp.update(
            severity="warning",
            status="degraded",
            detail=f"heartbeat cũ {int(age)}s" if age is not None else "chưa có heartbeat",
        )
    elif not pid_match:
        comp.update(severity="warning", status="degraded", detail="heartbeat không khớp tiến trình")
    elif not comp["polling"]:
        comp.update(severity="warning", status="degraded", detail="không polling")
    else:
        comp.update(severity="ok", status="ok", detail="polling bình thường")
    return comp


def _process_component(script: Path, workspace: Path, disabled_name: str) -> dict:
    if _flag_exists(workspace, disabled_name):
        return {
            "severity": "ok",
            "status": "disabled",
            "pids": [],
            "detail": f"đã tắt theo {disabled_name}",
        }
    pids = _pids_for(script)
    if pids:
        return {"severity": "ok", "status": "ok", "pids": pids, "detail": "đang chạy"}
    return {"severity": "warning", "status": "down", "pids": [], "detail": "không có tiến trình"}


def _disk_components() -> dict:
    import psutil

    components: dict = {}
    for letter in ("C:", "D:"):
        try:
            usage = psutil.disk_usage(letter + os.sep)
        except OSError:
            continue
        free_pct = usage.free / usage.total * 100 if usage.total else 0.0
        if free_pct < 5:
            severity = "critical"
        elif free_pct < 10:
            severity = "warning"
        else:
            severity = "ok"
        name = "disk_" + letter[0].lower()
        components[name] = {
            "severity": severity,
            "status": "ok" if severity == "ok" else "degraded",
            "free_gb": round(usage.free / 1e9, 1),
            "free_pct": round(free_pct, 1),
            "detail": f"còn {usage.free / 1e9:.1f} GB ({free_pct:.1f}%)",
        }
    return components


def _notify(level: str, title: str, body: str = "") -> None:
    logger.info("Cảnh báo: %s — %s", title, body)
    try:
        import notification_service

        notification_service.push(level, title, body, source="ops_watchdog")
    except Exception:
        logger.debug("Không gửi được thông báo", exc_info=True)


def _rank(severity: str) -> int:
    return SEVERITY_RANK.get(severity, 0)


def _alert_body(name: str, comp: dict) -> str:
    parts = [f"Trạng thái: {comp.get('status', '?')}"]
    if comp.get("active") is not None:
        parts.append("batch đang chạy" if comp["active"] else "batch rảnh")
    if comp.get("pids"):
        parts.append(f"PID: {', '.join(str(p) for p in comp['pids'])}")
    if comp.get("heal"):
        parts.append("Tự phục hồi: " + comp["heal"])
    if comp.get("recommendation"):
        parts.append(comp["recommendation"])
    return " | ".join(parts)


def _apply_alerts(state: dict, previous: dict, now: float) -> None:
    previous_components = previous.get("components", {}) if isinstance(previous, dict) else {}
    for name, comp in state["components"].items():
        prev = previous_components.get(name, {}) if isinstance(previous_components, dict) else {}
        severity = comp.get("severity", "ok")
        prev_severity = prev.get("severity", "ok")
        last_alert = 0.0
        try:
            last_alert = float(prev.get("last_alert_at") or 0)
        except (TypeError, ValueError):
            last_alert = 0.0
        if severity != "ok" and (
            _rank(severity) > _rank(prev_severity)
            or now - last_alert >= ALERT_REPEAT_SECONDS
        ):
            level = "critical" if severity == "critical" else "warning"
            label = LABELS.get(name, name)
            _notify(level, f"{label}: {comp.get('detail', '')}", _alert_body(name, comp))
            comp["last_alert_at"] = now
        elif severity == "ok" and _rank(prev_severity) > 0:
            label = LABELS.get(name, name)
            _notify("success", f"{label} đã phục hồi", comp.get("detail", ""))
        elif last_alert:
            comp["last_alert_at"] = last_alert


def _dashboard_restart_block(workspace: Path) -> str:
    if _v1_paused(workspace):
        return "đang tạm dừng theo v1.pause"
    if _flag_exists(workspace, "batch.lock"):
        return "batch.lock còn tồn tại"
    if _read_job_status(workspace).get("active"):
        return "batch đang chạy"
    return ""


def _heal_times(state: dict, name: str, now: float) -> list[float]:
    times = state.setdefault("heal", {}).get(name, [])
    if not isinstance(times, list):
        times = []
    fresh: list[float] = []
    for value in times:
        try:
            at = float(value)
        except (TypeError, ValueError):
            continue
        if now - at < 3600:
            fresh.append(at)
    return fresh


def _run_task(task_name: str) -> tuple[bool, str]:
    schtasks = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    try:
        result = subprocess.run(
            [str(schtasks), "/Run", "/TN", task_name],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        message = (result.stdout or result.stderr or "").strip()
        return result.returncode == 0, message[:300]
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def _apply_heal(state: dict, workspace: Path, now: float, enabled: bool) -> None:
    if _flag_exists(workspace, "ops.pause"):
        state["heal_paused"] = True
        return
    for name in ("control", "dashboard", "telegram", "scriptbot", "downloader"):
        comp = state["components"].get(name)
        if not comp or comp.get("status") != "down":
            continue
        task_name = TASKS.get(name)
        if not task_name:
            continue
        if name == "dashboard":
            block = _dashboard_restart_block(workspace)
            if block:
                comp["heal"] = f"không tự khởi động: {block}"
                continue
        if not enabled:
            comp["recommendation"] = f"có thể tự chạy {task_name} khi bật --heal"
            continue
        times = _heal_times(state, name, now)
        if len(times) >= HEAL_MAX_PER_HOUR:
            comp["heal"] = "đã thử 3 lần trong 1 giờ, tạm dừng tự phục hồi"
            continue
        if times and now - times[-1] < HEAL_COOLDOWN_SECONDS:
            comp["heal"] = "đang cooldown 5 phút sau lần thử trước"
            continue
        ok, message = _run_task(task_name)
        state.setdefault("heal", {})[name] = (times + [now])[-10:]
        if ok:
            comp["heal"] = f"đã chạy {task_name}"
            _notify("info", f"Đã khởi động {LABELS.get(name, name)}", f"Task {task_name}")
        else:
            comp["heal"] = f"chạy {task_name} thất bại: {message}"
            logger.warning("Tự phục hồi %s thất bại: %s", name, message)


def run_check(workspace: Path | None = None, *, now: float | None = None, heal: bool = False) -> dict:
    workspace = Path(workspace) if workspace is not None else WORKSPACE
    now = float(now if now is not None else time.time())
    path = _state_path(workspace)
    previous = _load_json(path)
    paused = _v1_paused(workspace)

    components: dict = {}
    components["dashboard"] = (
        {
            "severity": "ok",
            "status": "disabled",
            "active": False,
            "detail": "tạm dừng theo v1.pause",
        }
        if paused
        else _dashboard_component(now)
    )
    components["control"] = _control_component(now)
    components["telegram"] = _telegram_component(workspace, now, paused)
    components["scriptbot"] = _process_component(
        ROOT / "telegram_script_bot.py", workspace, "scriptbot.disabled"
    )
    components["downloader"] = _process_component(
        PROJECT_ROOT / "telebot_video_downloader.py", workspace, "downloader.disabled"
    )
    components.update(_disk_components())

    worst = max((_rank(comp.get("severity", "ok")) for comp in components.values()), default=0)
    overall = "critical" if worst >= 2 else "warning" if worst == 1 else "ok"
    state = {
        "updated_at": now,
        "overall": overall,
        "paused": paused,
        "components": components,
        "heal": previous.get("heal", {}) if isinstance(previous.get("heal"), dict) else {},
    }
    _apply_heal(state, workspace, now, heal)
    _apply_alerts(state, previous, now)
    _atomic_write(path, state)
    return state


def _acquire_mutex() -> bool:
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    _MUTEX_HANDLE = kernel.CreateMutexW(None, False, "Local\\AutoDubV1_ops_watchdog")
    return bool(_MUTEX_HANDLE) and ctypes.get_last_error() != 183


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="kiểm tra một lần rồi thoát")
    parser.add_argument("--heal", action="store_true", help="tự chạy Scheduled Task đang thiếu")
    parser.add_argument("--json", action="store_true", help="in kết quả JSON")
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args(argv)

    try:
        from environment import load_environment

        load_environment(ROOT)
    except Exception:
        logger.debug("Không nạp được backend/.env", exc_info=True)

    heal = args.heal or os.getenv("AUTODUB_OPS_AUTOHEAL", "0").lower() in ("1", "true", "yes")
    try:
        from core.logging_setup import setup_logging

        setup_logging(
            "ops_watchdog",
            [WORKSPACE / "bot_system" / "service_logs" / "ops_watchdog.log"],
            force=True,
            fmt="%(asctime)s [%(levelname)s] %(message)s",
        )
    except Exception:
        logging.basicConfig(level=logging.INFO)

    if args.once:
        state = run_check(heal=heal)
        if args.json:
            text = json.dumps(state, ensure_ascii=False, indent=2)
            try:
                print(text)
            except UnicodeEncodeError:
                print(json.dumps(state, ensure_ascii=True, indent=2))
        return 0

    if not _acquire_mutex():
        logger.info("Watchdog đã chạy ở tiến trình khác; thoát.")
        return 0
    logger.info("Watchdog bắt đầu (heal=%s, interval=%ss)", heal, args.interval)
    while True:
        try:
            run_check(heal=heal)
        except Exception:
            logger.exception("Chu kỳ kiểm tra thất bại")
        time.sleep(max(5.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
