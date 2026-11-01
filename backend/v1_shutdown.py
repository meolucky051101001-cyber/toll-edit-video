"""Scoped, verified service and worker process shutdown for Tool V1.

This module intentionally does not import AI/model packages so it can be
imported and tested in isolation without side effects.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Callable, Iterable, Sequence

try:
    import psutil
except ImportError:  # pragma: no cover - fallback when psutil is absent
    psutil = None

logger = logging.getLogger("v1_shutdown")

EXIT_OK = 0
EXIT_BUSY = 2
EXIT_ACCESS_DENIED = 3
EXIT_AMBIGUOUS = 4
EXIT_SURVIVOR = 5

SERVICE_SCRIPTS: dict[str, set[str]] = {
    "control": {"tool_control.py"},
    "dashboard": {"main.py"},
    "telegram": {"telegram_bot.py"},
    "scriptbot": {"telegram_script_bot.py"},
    "watchdog": {"ops_watchdog.py"},
    "supervisor": {"background_service.py"},
    "downloader": {"telebot_video_downloader.py"},
}

SUPERVISOR_SERVICES = {"watchdog", "supervisor", "control"}

WORKER_SCRIPT_NAMES = {
    "v1_ocr_worker.py",
    "separator_worker.py",
    "v1_separator_worker.py",
    "gpu_worker.py",
    "model_runner.py",
    "model_runtime_runner.py",
    "batch_processor.py",
}

WORKER_MODULE_NAMES = {
    "separator_worker",
    "v1_separator_worker",
    "backend.separator_worker",
    "backend.v1_separator_worker",
    "demucs",
    "demucs.separate",
    "model_workers",
    "bs_roformer",
    "v1_ocr_worker",
    "backend.v1_ocr_worker",
}

MEDIA_WORKER_EXECUTABLES = {
    "ffmpeg.exe",
    "ffprobe.exe",
    "yt-dlp.exe",
    "ffmpeg",
    "ffprobe",
    "yt-dlp",
}

PYTHON_EXECUTABLE_PREFIXES = ("python", "pythonw", "py")

SCHEDULED_TASKS_BY_SCOPE: dict[str, tuple[str, ...]] = {
    "telegram": ("AutoDubV1-telegram",),
    "downloader": ("AutoDubV1-telebot-downloader",),
    "all": (
        "AutoDubV1-ops-watchdog",
        "AutoDub-control",
        "AutoDubV1-telegram",
        "AutoDubV1-scriptbot",
        "AutoDubV1-dashboard",
    ),
    "cutover": (
        "AutoDubV1-ops-watchdog",
        "AutoDub-control",
        "AutoDubV1-telegram",
        "AutoDubV1-scriptbot",
        "AutoDubV1-dashboard",
        "AutoDubV1-telebot-downloader",
    ),
}


@dataclass(frozen=True)
class ProcessRecord:
    pid: int
    ppid: int
    name: str
    exe: str = ""
    cmdline: tuple[str, ...] = ()
    cwd: str = ""
    create_time: float = 0.0


@dataclass
class SelectedTarget:
    record: ProcessRecord
    role: str
    reason: str
    is_supervisor: bool = False


@dataclass
class ShutdownResult:
    exit_code: int
    scope: str
    mode: str
    preview: bool
    selected: list[SelectedTarget] = field(default_factory=list)
    terminated_pids: list[int] = field(default_factory=list)
    killed_pids: list[int] = field(default_factory=list)
    survivor_pids: list[int] = field(default_factory=list)
    listening_ports: list[int] = field(default_factory=list)
    tasks_stopped: list[str] = field(default_factory=list)
    flags_written: list[str] = field(default_factory=list)
    busy_reasons: list[str] = field(default_factory=list)
    access_denied_pids: list[int] = field(default_factory=list)
    telemetry_rejected_reason: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "scope": self.scope,
            "mode": self.mode,
            "preview": self.preview,
            "selected": [
                {
                    "pid": item.record.pid,
                    "ppid": item.record.ppid,
                    "name": item.record.name,
                    "role": item.role,
                    "reason": item.reason,
                    "is_supervisor": item.is_supervisor,
                }
                for item in self.selected
            ],
            "terminated_pids": self.terminated_pids,
            "killed_pids": self.killed_pids,
            "survivor_pids": self.survivor_pids,
            "listening_ports": self.listening_ports,
            "tasks_stopped": self.tasks_stopped,
            "flags_written": self.flags_written,
            "busy_reasons": self.busy_reasons,
            "access_denied_pids": self.access_denied_pids,
            "telemetry_rejected_reason": self.telemetry_rejected_reason,
            "message": self.message,
        }


def split_windows_command_line(command_line: str | Sequence[str] | None) -> list[str]:
    """Parse a Windows command line string into argv tokens preserving spaces in quotes."""
    if command_line is None:
        return []
    if isinstance(command_line, (list, tuple)):
        return [str(part) for part in command_line if str(part) != ""]
    raw = str(command_line).strip()
    if not raw:
        return []
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    quote_char = '"'
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n and raw[i + 1] == quote_char:
            current.append(quote_char)
            i += 2
            continue
        if ch in ('"', "'"):
            if not in_quotes:
                in_quotes = True
                quote_char = ch
                i += 1
                continue
            if ch == quote_char:
                in_quotes = False
                i += 1
                continue
        if ch.isspace() and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _norm_case_path(value: str | Path) -> str:
    raw = str(value).strip().strip('"').strip("'")
    if not raw:
        return ""
    normalized = os.path.normpath(raw.replace("/", "\\"))
    return os.path.normcase(normalized)


def _is_within_root(candidate: str | Path, root: str | Path) -> bool:
    cand_norm = _norm_case_path(candidate)
    root_norm = _norm_case_path(root)
    if not cand_norm or not root_norm:
        return False
    if cand_norm == root_norm:
        return True
    prefix = root_norm.rstrip("\\") + "\\"
    return cand_norm.startswith(prefix)


def _is_python_process_name(name: str) -> bool:
    lowered = (name or "").strip().lower()
    if lowered.endswith(".exe"):
        lowered = lowered[:-4]
    if lowered in PYTHON_EXECUTABLE_PREFIXES:
        return True
    return any(
        lowered.startswith(prefix) and lowered[len(prefix):].replace(".", "").isdigit()
        for prefix in ("python", "pythonw")
    )


def _is_media_worker_name(name: str) -> bool:
    return (name or "").strip().lower() in MEDIA_WORKER_EXECUTABLES


def _extract_python_invocation(
    argv: Sequence[str], cwd: str, project_root: Path
) -> tuple[str | None, str | None, list[str]]:
    """Return (resolved_script_path_or_none, module_name_or_none, remaining_args)."""
    tokens = list(argv)
    if not tokens:
        return None, None, []
    first_name = Path(tokens[0].strip('"')).name.lower()
    idx = 1 if _is_python_process_name(first_name) else 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok == "-m" and idx + 1 < len(tokens):
            return None, tokens[idx + 1].strip(), tokens[idx + 2 :]
        if tok in ("-c", "-"):
            return None, None, tokens[idx + 1 :]
        if tok in ("-W", "-X", "--check-hash-based-pycs") and idx + 1 < len(tokens):
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        script_arg = tok.strip('"').strip("'")
        script_path = Path(script_arg)
        if not script_path.is_absolute():
            base = Path(cwd) if cwd else (project_root / "backend")
            script_path = base / script_path
        return _norm_case_path(script_path), None, tokens[idx + 1 :]
    return None, None, []


def _classify_project_process(
    record: ProcessRecord,
    project_root: Path,
    extra_roots: Sequence[Path] = (),
) -> tuple[str | None, str, bool]:
    """Return (role, reason, is_supervisor) if record is a verified direct Tool V1 process."""
    backend_root = project_root / "backend"
    allowed_roots = [project_root, *extra_roots]

    if not _is_python_process_name(record.name):
        return None, "", False

    argv = split_windows_command_line(record.cmdline)
    script_norm, module_name, rem_args = _extract_python_invocation(
        argv, record.cwd, project_root
    )
    exe_in_project = bool(record.exe) and any(
        _is_within_root(record.exe, root) for root in allowed_roots
    )

    if script_norm:
        script_in_project = any(
            _is_within_root(script_norm, root) for root in allowed_roots
        )
        if not script_in_project:
            return None, "", False
        script_basename = Path(script_norm).name.lower()

        if script_basename == "background_service.py":
            svc_target = "supervisor"
            for idx, tok in enumerate(rem_args):
                if tok == "--service" and idx + 1 < len(rem_args):
                    svc_target = f"supervisor:{rem_args[idx + 1].strip().lower()}"
                    break
            return svc_target, f"script={script_basename}", True

        for role, names in SERVICE_SCRIPTS.items():
            if script_basename in names:
                expected_dir = project_root if role == "downloader" else backend_root
                if _is_within_root(script_norm, expected_dir) or _is_within_root(
                    script_norm, project_root
                ):
                    return (
                        role,
                        f"script={script_basename}",
                        role in SUPERVISOR_SERVICES,
                    )

        if script_basename in WORKER_SCRIPT_NAMES:
            return "worker", f"worker_script={script_basename}", False

    if module_name:
        mod_lower = module_name.lower()
        if mod_lower in WORKER_MODULE_NAMES and (
            exe_in_project
            or (record.cwd and any(_is_within_root(record.cwd, r) for r in allowed_roots))
        ):
            return "worker", f"worker_module={mod_lower}", False
        if mod_lower == "uvicorn" and exe_in_project:
            if any("main:app" in arg.lower() for arg in rem_args):
                return "dashboard", "uvicorn=main:app", False

    return None, "", False


def _media_worker_belongs_to_project(
    record: ProcessRecord,
    project_root: Path,
    workspace_roots: Sequence[Path],
) -> tuple[bool, str]:
    if not _is_media_worker_name(record.name):
        return False, ""
    all_roots = [project_root, *workspace_roots]
    if record.exe and any(_is_within_root(record.exe, r) for r in all_roots):
        return True, "media_exe_in_project"
    if record.cwd and any(_is_within_root(record.cwd, r) for r in all_roots):
        return True, "media_cwd_in_project"
    argv = split_windows_command_line(record.cmdline)
    for token in argv[1:]:
        cleaned = token.strip().strip('"').strip("'")
        if not cleaned or cleaned.startswith("-"):
            continue
        if (":" in cleaned or "\\" in cleaned or "/" in cleaned) and any(
            _is_within_root(cleaned, r) for r in all_roots
        ):
            return True, f"media_arg_in_project={Path(cleaned).name}"
    return False, ""


def _scope_matches_role(scope: str, role: str, include_downloader: bool = False) -> bool:
    if scope == "telegram":
        return role in ("telegram", "supervisor:telegram")
    if scope == "downloader":
        return role == "downloader"
    if scope == "workers":
        return role == "worker"
    if scope == "cutover":
        return True
    if scope == "all":
        if role == "downloader":
            return bool(include_downloader)
        return True
    return False


def verify_telemetry_pid(
    telemetry: dict[str, Any] | None,
    records_by_pid: dict[int, ProcessRecord],
    project_root: Path,
    expected_role: str = "telegram",
) -> tuple[int | None, str]:
    """Verify a telemetry PID against live process executable, script, and creation time."""
    if not isinstance(telemetry, dict):
        return None, ""
    raw_pid = telemetry.get("pid")
    if not raw_pid:
        return None, ""
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None, "invalid_telemetry_pid"
    if pid <= 0:
        return None, ""
    record = records_by_pid.get(pid)
    if record is None:
        return None, f"telemetry_pid_not_running:{pid}"
    if not _is_python_process_name(record.name):
        return None, f"telemetry_pid_reused_non_python:{pid}"
    role, _, _ = _classify_project_process(record, project_root)
    if role != expected_role:
        return None, f"telemetry_pid_role_mismatch:{pid}:{role or 'unknown'}"

    telemetry_started = telemetry.get("started_at") or telemetry.get("create_time")
    if telemetry_started is not None:
        try:
            started_f = float(telemetry_started)
            if record.create_time > 0 and abs(record.create_time - started_f) > 5.0:
                return None, f"telemetry_pid_create_time_mismatch:{pid}"
        except (TypeError, ValueError):
            return None, f"telemetry_pid_invalid_create_time:{pid}"
    else:
        at_val = telemetry.get("at")
        if at_val is not None and record.create_time > 0:
            try:
                if record.create_time > float(at_val) + 2.0:
                    return None, f"telemetry_pid_created_after_heartbeat:{pid}"
            except (TypeError, ValueError):
                pass
    return pid, "verified_telemetry_pid"


def select_shutdown_targets(
    records: Sequence[ProcessRecord],
    *,
    project_root: Path,
    workspace_roots: Sequence[Path] = (),
    scope: str = "all",
    include_downloader: bool = False,
    caller_pid: int | None = None,
    telemetry: dict[str, Any] | None = None,
    include_caller_children: bool = False,
) -> tuple[list[SelectedTarget], str]:
    """Select verified target processes for the requested shutdown scope.

    Returns (ordered_targets_supervisors_first, telemetry_rejection_reason).
    """
    project_root = Path(project_root).resolve()
    ws_roots = [Path(w).resolve() for w in workspace_roots]
    if not ws_roots:
        ws_roots = [project_root / "workspace"]

    current_pid = os.getpid() if caller_pid is None else int(caller_pid)
    records_by_pid = {r.pid: r for r in records}
    children_by_ppid: dict[int, list[ProcessRecord]] = {}
    for rec in records:
        children_by_ppid.setdefault(rec.ppid, []).append(rec)

    selected_by_pid: dict[int, SelectedTarget] = {}
    telemetry_rejection = ""

    if telemetry and scope in ("telegram", "all", "cutover"):
        verified_pid, reason = verify_telemetry_pid(
            telemetry, records_by_pid, project_root, expected_role="telegram"
        )
        if verified_pid is not None:
            rec = records_by_pid[verified_pid]
            selected_by_pid[verified_pid] = SelectedTarget(
                record=rec,
                role="telegram",
                reason=reason,
                is_supervisor=False,
            )
        elif reason:
            telemetry_rejection = reason

    # 1. Direct verified service/worker roots
    seed_pids: set[int] = set(selected_by_pid.keys())
    if include_caller_children and current_pid in records_by_pid:
        for child in children_by_ppid.get(current_pid, []):
            if child.pid != current_pid:
                role, reason, _ = _classify_project_process(
                    child, project_root, ws_roots
                )
                if role in SERVICE_SCRIPTS or (
                    role and role.startswith("supervisor")
                ):
                    continue
                selected_by_pid[child.pid] = SelectedTarget(
                    record=child,
                    role="worker",
                    reason="direct_child_of_caller",
                    is_supervisor=False,
                )
                seed_pids.add(child.pid)

    for rec in records:
        if rec.pid == current_pid or rec.pid in selected_by_pid:
            continue
        role, reason, is_sup = _classify_project_process(rec, project_root, ws_roots)
        if role and _scope_matches_role(scope, role, include_downloader=include_downloader):
            selected_by_pid[rec.pid] = SelectedTarget(
                record=rec,
                role=role,
                reason=reason,
                is_supervisor=is_sup or role.startswith("supervisor"),
            )
            seed_pids.add(rec.pid)
            continue

        if scope in ("workers", "all", "cutover"):
            ok_media, media_reason = _media_worker_belongs_to_project(
                rec, project_root, ws_roots
            )
            if ok_media:
                selected_by_pid[rec.pid] = SelectedTarget(
                    record=rec,
                    role="worker",
                    reason=media_reason,
                    is_supervisor=False,
                )
                seed_pids.add(rec.pid)

    # 2. Expand parent/child tree from verified project seeds (venv wrapper -> base python -> worker)
    # Also include direct venv pythonw wrapper parents when their only purpose is launching the child.
    for pid in list(seed_pids):
        rec = records_by_pid.get(pid)
        if rec is None:
            continue
        parent = records_by_pid.get(rec.ppid)
        if (
            parent is not None
            and parent.pid != current_pid
            and parent.pid not in selected_by_pid
            and _is_python_process_name(parent.name)
        ):
            parent_exe_in_proj = bool(parent.exe) and _is_within_root(
                parent.exe, project_root
            )
            parent_script, _, _ = _extract_python_invocation(
                split_windows_command_line(parent.cmdline), parent.cwd, project_root
            )
            child_script, _, _ = _extract_python_invocation(
                split_windows_command_line(rec.cmdline), rec.cwd, project_root
            )
            if parent_exe_in_proj and parent_script and parent_script == child_script:
                child_target = selected_by_pid[pid]
                selected_by_pid[parent.pid] = SelectedTarget(
                    record=parent,
                    role=child_target.role,
                    reason=f"venv_launcher_parent_of:{pid}",
                    is_supervisor=child_target.is_supervisor,
                )
                seed_pids.add(parent.pid)

    queue = list(seed_pids)
    while queue:
        parent_pid = queue.pop(0)
        parent_target = selected_by_pid.get(parent_pid)
        for child in children_by_ppid.get(parent_pid, []):
            if child.pid == current_pid or child.pid in selected_by_pid:
                continue
            child_name_lower = (child.name or "").lower()
            if child_name_lower == "conhost.exe":
                continue
            if not (
                _is_python_process_name(child.name)
                or _is_media_worker_name(child.name)
            ):
                continue
            child_role, child_reason, child_sup = _classify_project_process(
                child, project_root, ws_roots
            )
            if scope == "workers" and (
                child_role in SERVICE_SCRIPTS
                or (child_role and child_role.startswith("supervisor"))
            ):
                continue
            selected_by_pid[child.pid] = SelectedTarget(
                record=child,
                role=child_role or (parent_target.role if parent_target else "worker"),
                reason=child_reason or f"descendant_of_verified_pid:{parent_pid}",
                is_supervisor=child_sup,
            )
            queue.append(child.pid)

    ordered = sorted(
        selected_by_pid.values(),
        key=lambda item: (
            0 if item.is_supervisor else 1,
            -item.record.create_time,
            item.record.pid,
        ),
    )
    return ordered, telemetry_rejection


def enumerate_live_processes() -> list[ProcessRecord]:
    if psutil is None:
        return []
    records: list[ProcessRecord] = []
    for proc in psutil.process_iter(
        ["pid", "ppid", "name", "exe", "cmdline", "cwd", "create_time"]
    ):
        try:
            info = proc.info
            cmdline_raw = info.get("cmdline") or ()
            records.append(
                ProcessRecord(
                    pid=int(info["pid"]),
                    ppid=int(info.get("ppid") or 0),
                    name=str(info.get("name") or ""),
                    exe=str(info.get("exe") or ""),
                    cmdline=tuple(str(x) for x in cmdline_raw),
                    cwd=str(info.get("cwd") or ""),
                    create_time=float(info.get("create_time") or 0.0),
                )
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return records


def _load_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def resolve_control_dir(workspace: Path) -> Path:
    bs_control = workspace / "bot_system" / "control"
    if bs_control.is_dir() or (workspace / "bot_system").is_dir():
        return bs_control
    return workspace / "control"


def read_telegram_telemetry(workspace: Path) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_at = -1.0
    for candidate in (
        workspace / "bot_system" / "control" / "v1.json",
        workspace / "control" / "v1.json",
    ):
        data = _load_json_file(candidate)
        try:
            at = float(data.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        if data and at >= best_at:
            best_at = at
            best = data
    return best


def check_busy_state(
    project_root: Path,
    workspace: Path,
    scope: str,
) -> list[str]:
    """Return a list of busy reasons if an active job/batch is currently running."""
    reasons: list[str] = []
    if scope in ("downloader", "workers"):
        return reasons

    if scope in ("all", "cutover"):
        for status_path in (
            workspace / "bot_system" / "job_status.json",
            workspace / "job_status.json",
        ):
            status_data = _load_json_file(status_path)
            if status_data.get("active") is True:
                video_name = status_data.get("video_name") or status_data.get("job_id") or "batch"
                reasons.append(f"dashboard_job_active:{video_name}")
                break

        for lock_path in (
            workspace / "bot_system" / "batch.lock",
            workspace / "batch.lock",
        ):
            if lock_path.is_file():
                lock_data = _load_json_file(lock_path)
                lock_pid = lock_data.get("pid")
                if lock_pid and psutil is not None:
                    try:
                        if psutil.pid_exists(int(lock_pid)):
                            reasons.append(f"batch_lock_held:pid={lock_pid}")
                            break
                    except Exception:
                        pass

    if scope in ("telegram", "all", "cutover"):
        telemetry = read_telegram_telemetry(workspace)
        try:
            at = float(telemetry.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        fresh = at > 0 and (time.time() - at) < 30.0
        if fresh and telemetry.get("busy") is True:
            reasons.append("telegram_bot_busy")

    return reasons


def default_port_checker(ports: Sequence[int]) -> list[int]:
    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            if sock.connect_ex(("127.0.0.1", int(port))) == 0:
                listening.append(int(port))
    return listening


def default_task_Controller(
    task_names: Sequence[str],
    *,
    mode: str,
) -> list[str]:
    """Stop Scheduled Tasks without losing their Enabled configuration."""
    if os.name != "nt" or not task_names:
        return []
    stopped: list[str] = []
    schtasks = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe"
    for name in task_names:
        try:
            res = subprocess.run(
                [str(schtasks), "/End", "/TN", name],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if res.returncode == 0:
                stopped.append(name)
        except Exception:
            continue
    return stopped


def default_process_terminator(
    targets: Sequence[SelectedTarget],
    *,
    timeout: float = 3.0,
) -> tuple[list[int], list[int], list[int]]:
    """Terminate targets (supervisors first, then workers) verifying create_time against PID reuse.

    Returns (terminated_pids, killed_pids, access_denied_pids).
    """
    if psutil is None:
        return [], [], []

    terminated: list[int] = []
    killed: list[int] = []
    access_denied: list[int] = []

    supervisors = [t for t in targets if t.is_supervisor]
    non_supervisors = [t for t in targets if not t.is_supervisor]

    for group in (supervisors, non_supervisors):
        if not group:
            continue
        live_procs: list[Any] = []
        for item in group:
            pid = item.record.pid
            try:
                proc = psutil.Process(pid)
                if item.record.create_time > 0:
                    live_ctime = float(proc.create_time() or 0.0)
                    if abs(live_ctime - item.record.create_time) > 2.0:
                        logger.warning("Skipping PID %s due to creation time mismatch (PID reuse)", pid)
                        continue
                proc.terminate()
                terminated.append(pid)
                live_procs.append(proc)
            except psutil.NoSuchProcess:
                # Exited between inventory and terminate -> treated as stopped
                terminated.append(pid)
            except psutil.AccessDenied:
                access_denied.append(pid)
            except Exception:
                continue

        if live_procs:
            _, alive = psutil.wait_procs(live_procs, timeout=timeout)
            for proc in alive:
                try:
                    proc.kill()
                    killed.append(proc.pid)
                except psutil.NoSuchProcess:
                    pass
                except psutil.AccessDenied:
                    if proc.pid not in access_denied:
                        access_denied.append(proc.pid)
                except Exception:
                    pass
            if alive:
                psutil.wait_procs(alive, timeout=1.0)

    return terminated, killed, access_denied


def _write_stop_intent_flags(
    workspace: Path,
    *,
    scope: str,
    mode: str,
    include_downloader: bool,
) -> list[str]:
    """Write pause/disabled flags for intentional stops so watchdog/supervisors do not auto-restart."""
    if mode == "restart" or scope == "workers":
        return []
    written: list[str] = []
    control_dirs = [workspace / "control"]
    if (workspace / "bot_system").is_dir() or (workspace / "bot_system" / "control").is_dir():
        control_dirs.insert(0, workspace / "bot_system" / "control")

    flag_names: list[str] = []
    if scope == "telegram":
        flag_names.append("telegram.disabled")
    elif scope == "downloader":
        flag_names.append("downloader.disabled")
    elif scope in ("all", "cutover"):
        flag_names.extend(["v1.pause", "ops.pause", "telegram.disabled", "scriptbot.disabled"])
        if include_downloader or scope == "cutover":
            flag_names.append("downloader.disabled")

    for cdir in control_dirs:
        try:
            cdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for fname in flag_names:
            fpath = cdir / fname
            try:
                fpath.write_text("paused", encoding="utf-8")
                written.append(str(fpath))
            except OSError:
                continue
    return written


def _update_stopped_telemetry(workspace: Path, scope: str) -> None:
    if scope not in ("telegram", "all", "cutover"):
        return
    for candidate in (
        workspace / "bot_system" / "control" / "v1.json",
        workspace / "control" / "v1.json",
    ):
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    data["pid"] = None
                    data["polling"] = False
                    data["busy"] = False
                    if scope in ("all", "cutover"):
                        data["paused"] = True
                    candidate.write_text(json.dumps(data), encoding="utf-8")
            except Exception:
                pass


def execute_shutdown(
    *,
    project_root: Path | None = None,
    workspace: Path | None = None,
    scope: str = "all",
    mode: str = "stop",
    preview: bool = False,
    force: bool = False,
    include_downloader: bool = False,
    caller_pid: int | None = None,
    include_caller_children: bool = False,
    process_enumerator: Callable[[], Sequence[ProcessRecord]] = enumerate_live_processes,
    process_terminator: Callable[..., tuple[list[int], list[int], list[int]]] = default_process_terminator,
    task_controller: Callable[..., list[str]] = default_task_Controller,
    port_checker: Callable[[Sequence[int]], list[int]] = default_port_checker,
) -> ShutdownResult:
    """Unified shutdown flow: inventory -> select targets -> preview/busy check -> execute -> verify."""
    root = (Path(project_root) if project_root else Path(__file__).resolve().parents[1]).resolve()
    ws = (
        Path(workspace).resolve()
        if workspace
        else Path(os.getenv("AUTODUB_WORKSPACE", str(root / "workspace"))).resolve()
    )
    ws_roots = [ws]
    default_ws = (root / "workspace").resolve()
    if default_ws not in ws_roots:
        ws_roots.append(default_ws)

    records = list(process_enumerator())
    telemetry = read_telegram_telemetry(ws) if scope in ("telegram", "all", "cutover") else None

    selected, telemetry_rejected = select_shutdown_targets(
        records,
        project_root=root,
        workspace_roots=ws_roots,
        scope=scope,
        include_downloader=include_downloader or (scope == "cutover"),
        caller_pid=caller_pid,
        telemetry=telemetry,
        include_caller_children=include_caller_children,
    )

    result = ShutdownResult(
        exit_code=EXIT_OK,
        scope=scope,
        mode=mode,
        preview=preview,
        selected=selected,
        telemetry_rejected_reason=telemetry_rejected,
    )

    busy_reasons = check_busy_state(root, ws, scope)
    result.busy_reasons = busy_reasons

    # Preview mode never writes flags, never stops tasks, and never terminates processes
    if preview:
        if busy_reasons and not force:
            result.exit_code = EXIT_BUSY
            result.message = f"[PREVIEW] Busy ({', '.join(busy_reasons)}); would refuse shutdown without --force."
        else:
            result.exit_code = EXIT_OK
            result.message = f"[PREVIEW] Selected {len(selected)} verified process(es) for scope={scope}."
        return result

    if busy_reasons and not force:
        result.exit_code = EXIT_BUSY
        result.message = (
            f"Refusing shutdown because Tool V1 is busy ({', '.join(busy_reasons)}). "
            "Wait for active jobs to finish or pass --force explicitly."
        )
        return result

    if not selected and telemetry_rejected:
        result.exit_code = EXIT_AMBIGUOUS
        result.message = f"Refusing unverified telemetry PID ({telemetry_rejected})."
        return result

    # 1. Record stop intent flags BEFORE stopping tasks/supervisors so watchdogs do not respawn
    result.flags_written = _write_stop_intent_flags(
        ws,
        scope=scope,
        mode=mode,
        include_downloader=include_downloader,
    )

    # 2. Stop relevant Scheduled Tasks (preserving Enabled state)
    task_names = list(SCHEDULED_TASKS_BY_SCOPE.get(scope, ()))
    if scope == "all" and include_downloader and "AutoDubV1-telebot-downloader" not in task_names:
        task_names.append("AutoDubV1-telebot-downloader")
    if task_names:
        result.tasks_stopped = task_controller(task_names, mode=mode)

    # 3. Terminate selected processes (supervisors first, then workers)
    terminated, killed, access_denied = process_terminator(selected)
    result.terminated_pids = terminated
    result.killed_pids = killed
    result.access_denied_pids = access_denied

    # 4. Update telemetry snapshot
    _update_stopped_telemetry(ws, scope)

    if access_denied:
        result.exit_code = EXIT_ACCESS_DENIED
        result.message = f"Access denied when stopping PID(s): {access_denied}."
        return result

    # 5. Final survivor & port verification
    post_records = list(process_enumerator())
    survivors, _ = select_shutdown_targets(
        post_records,
        project_root=root,
        workspace_roots=ws_roots,
        scope=scope,
        include_downloader=include_downloader or (scope == "cutover"),
        caller_pid=caller_pid,
        telemetry=None,
        include_caller_children=include_caller_children,
    )
    result.survivor_pids = [item.record.pid for item in survivors]

    ports_to_check = (8088, 8090) if scope in ("all", "cutover") else ()
    if ports_to_check:
        result.listening_ports = port_checker(ports_to_check)

    if result.survivor_pids or result.listening_ports:
        result.exit_code = EXIT_SURVIVOR
        result.message = (
            f"Shutdown incomplete: survivors={result.survivor_pids}, "
            f"ports={result.listening_ports}."
        )
        return result

    result.exit_code = EXIT_OK
    result.message = (
        f"Stopped {len(terminated)} verified process(es) for scope={scope}."
        if terminated
        else f"No active verified processes found for scope={scope}."
    )
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scoped, verified shutdown utility for Tool V1."
    )
    parser.add_argument(
        "--scope",
        choices=("all", "telegram", "downloader", "workers", "cutover"),
        default="all",
    )
    parser.add_argument(
        "--mode",
        choices=("stop", "restart"),
        default="stop",
        help="'stop' writes pause/disabled flags; 'restart' stops processes without disabling.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument(
        "--preview",
        "--whatif",
        "-WhatIf",
        dest="preview",
        action="store_true",
        help="Preview selected processes without stopping tasks, writing flags, or killing processes.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Stop even when a batch or Telegram job is reported busy.",
    )
    parser.add_argument(
        "--include-downloader",
        action="store_true",
        help="Include standalone telebot_video_downloader.py when --scope=all.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print structured JSON summary.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    res = execute_shutdown(
        project_root=args.project_root,
        workspace=args.workspace,
        scope=args.scope,
        mode=args.mode,
        preview=args.preview,
        force=args.force,
        include_downloader=args.include_downloader,
    )
    if args.json:
        print(json.dumps(res.to_dict(), ensure_ascii=False))
    else:
        print(res.message)
        for item in res.selected:
            print(f"  - PID {item.record.pid} ({item.record.name}) [{item.role}] ({item.reason})")
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
