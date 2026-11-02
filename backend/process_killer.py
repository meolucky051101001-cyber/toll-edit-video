"""
Process killer utility for Antigravity Video Dubbing Tool.
Safely terminates heavy background worker subprocesses (ffmpeg, ffprobe, yt-dlp, model workers)
without killing the host servers (main.py, telegram_bot.py, background_service.py).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Sequence

try:
    from backend.v1_shutdown import (
        ProcessRecord,
        default_process_terminator,
        enumerate_live_processes,
        select_shutdown_targets,
    )
except ImportError:
    from v1_shutdown import (
        ProcessRecord,
        default_process_terminator,
        enumerate_live_processes,
        select_shutdown_targets,
    )

logger = logging.getLogger("process_killer")

PROTECTED_SCRIPTS = {
    "main.py",
    "telegram_bot.py",
    "telegram_script_bot.py",
    "background_service.py",
    "tool_control.py",
    "ops_watchdog.py",
}

WORKER_PROCESS_NAMES = {
    "ffmpeg.exe",
    "ffprobe.exe",
    "yt-dlp.exe",
}

WORKER_SCRIPT_KEYWORDS = [
    "separator_worker",
    "v1_separator_worker",
    "demucs",
    "model_workers",
    "bs_roformer",
    "v1_ocr_worker",
]


def terminate_worker_processes(
    *,
    project_root: Path | None = None,
    workspace_roots: Sequence[Path] = (),
    caller_pid: int | None = None,
    preview: bool = False,
    process_enumerator: Callable[[], Sequence[ProcessRecord]] = enumerate_live_processes,
    process_terminator: Callable[..., tuple[list[int], list[int], list[int]]] = default_process_terminator,
) -> list[int]:
    """Terminate verified worker subprocesses across Tool V1 without substring collateral damage.

    Returns list of terminated (or preview-selected) PIDs.
    """
    root = (Path(project_root) if project_root else Path(__file__).resolve().parents[1]).resolve()
    ws_roots = [Path(w).resolve() for w in workspace_roots] if workspace_roots else []
    env_ws = os.getenv("AUTODUB_WORKSPACE")
    if env_ws:
        env_ws_path = Path(env_ws).resolve()
        if env_ws_path not in ws_roots:
            ws_roots.append(env_ws_path)
    default_ws = (root / "workspace").resolve()
    if default_ws not in ws_roots:
        ws_roots.append(default_ws)

    current_pid = os.getpid() if caller_pid is None else int(caller_pid)
    records = list(process_enumerator())
    selected, _ = select_shutdown_targets(
        records,
        project_root=root,
        workspace_roots=ws_roots,
        scope="workers",
        caller_pid=current_pid,
        include_caller_children=True,
    )
    if preview:
        return [item.record.pid for item in selected]

    terminated, killed, _ = process_terminator(selected, timeout=1.5)
    all_stopped = list(dict.fromkeys([*terminated, *killed]))
    for pid in all_stopped:
        logger.info("Terminated verified worker process (PID: %s)", pid)
    return all_stopped


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    killed = terminate_worker_processes()
    print(f"Killed worker processes: {killed}")
