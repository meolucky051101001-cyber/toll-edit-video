"""Điểm ghi/đọc duy nhất cho cờ dừng xuyên tiến trình của Tool V1.

Mọi thành phần (dashboard, Telegram bot, batch worker) gọi module này thay vì
tự tạo/xóa `control/video.stop` và `bot_system/stop_request.json`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))
AUDIT_MAX_BYTES = 1_000_000


def _workspace(workspace: str | Path | None) -> Path:
    return Path(workspace) if workspace is not None else DEFAULT_WORKSPACE


def _status_dir(workspace: Path, status_file: str | Path | None) -> Path:
    ws = workspace.resolve()
    if status_file is not None:
        parent = Path(status_file).parent
        try:
            if parent.resolve().is_relative_to(ws):
                return parent
        except OSError:
            pass
    return ws / "bot_system"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _audit(
    workspace: Path,
    status_file: str | Path | None,
    event: str,
    source: str,
    reason: str,
    job_id: str | None,
) -> None:
    path = _status_dir(workspace, status_file) / "stop_audit.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > AUDIT_MAX_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
        record = {
            "event": event,
            "at": time.time(),
            "source": source,
            "reason": reason,
            "job_id": job_id,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def request_stop(
    workspace: str | Path | None = None,
    *,
    status_file: str | Path | None = None,
    source: str = "",
    reason: str = "",
    job_id: str | None = None,
) -> None:
    ws = _workspace(workspace)
    now = time.time()
    control = ws / "control" / "video.stop"
    try:
        control.parent.mkdir(parents=True, exist_ok=True)
        control.write_text(str(now), encoding="utf-8")
    except OSError:
        pass

    try:
        _atomic_write_json(
            _status_dir(ws, status_file) / "stop_request.json",
            {"job_id": job_id, "requested_at": now, "source": source, "reason": reason},
        )
    except OSError:
        pass

    _audit(ws, status_file, "request", source, reason, job_id)


def clear_stop(
    workspace: str | Path | None = None,
    *,
    status_file: str | Path | None = None,
    source: str = "",
    reason: str = "",
) -> None:
    ws = _workspace(workspace)
    try:
        (ws / "control" / "video.stop").unlink(missing_ok=True)
    except OSError:
        pass
    try:
        (_status_dir(ws, status_file) / "stop_request.json").unlink(missing_ok=True)
    except OSError:
        pass
    _audit(ws, status_file, "clear", source, reason, None)


def is_stop_requested(
    workspace: str | Path | None = None,
    *,
    status_file: str | Path | None = None,
) -> bool:
    ws = _workspace(workspace)
    if (ws / "control" / "video.stop").exists():
        return True
    return (_status_dir(ws, status_file) / "stop_request.json").exists()


def stop_details(
    workspace: str | Path | None = None,
    *,
    status_file: str | Path | None = None,
) -> dict | None:
    path = _status_dir(_workspace(workspace), status_file) / "stop_request.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
