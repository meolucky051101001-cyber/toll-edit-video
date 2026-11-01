"""Sổ truy vết file thành phẩm và wrapper xoá file an toàn.

Mỗi sự kiện được ghi nối tiếp vào `bot_system/delivery_ledger.jsonl` để biết
một file đã được xác minh, bị dọn hay không thể xoá vì nằm ngoài phạm vi.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))
LEDGER_MAX_BYTES = 5_000_000


def _ledger_path(
    workspace: str | Path | None,
    status_file: str | Path | None,
) -> Path:
    if status_file is not None:
        return Path(status_file).parent / "delivery_ledger.jsonl"
    ws = Path(workspace) if workspace is not None else DEFAULT_WORKSPACE
    return ws / "bot_system" / "delivery_ledger.jsonl"


def record(
    event: str,
    *,
    path: str | Path | None = None,
    job_id: str | None = None,
    reason: str = "",
    workspace: str | Path | None = None,
    status_file: str | Path | None = None,
    extra: dict | None = None,
) -> bool:
    target = _ledger_path(workspace, status_file)
    entry: dict = {"event": event, "at": time.time(), "job_id": job_id, "reason": reason}
    if path is not None:
        file_path = Path(path)
        entry["path"] = str(file_path)
        try:
            stat = file_path.stat()
            entry["size"] = stat.st_size
            entry["mtime"] = stat.st_mtime
        except OSError:
            entry["missing"] = True
    if extra:
        entry.update(extra)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > LEDGER_MAX_BYTES:
            os.replace(target, target.with_name(target.name + ".1"))
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def safe_delete(
    path: str | Path,
    *,
    roots: Iterable[str | Path],
    reason: str,
    job_id: str | None = None,
    workspace: str | Path | None = None,
    status_file: str | Path | None = None,
    expected_size: int | None = None,
) -> bool:
    file_path = Path(path)
    try:
        resolved = file_path.resolve()
    except OSError:
        return False

    allowed = []
    for root in roots:
        if root is None:
            continue
        try:
            allowed.append(Path(root).resolve())
        except OSError:
            continue
    if not allowed or not any(resolved.is_relative_to(root) for root in allowed):
        record("delete_refused", path=file_path, job_id=job_id, reason=f"ngoài phạm vi: {reason}",
               workspace=workspace, status_file=status_file)
        return False

    if not resolved.is_file():
        return False

    if expected_size is not None:
        try:
            actual_size = resolved.stat().st_size
        except OSError:
            return False
        if actual_size != expected_size:
            record("delete_refused", path=file_path, job_id=job_id,
                   reason=f"kích thước lệch: {reason}",
                   workspace=workspace, status_file=status_file,
                   extra={"expected_size": expected_size})
            return False

    record("delete", path=file_path, job_id=job_id, reason=reason,
           workspace=workspace, status_file=status_file)
    try:
        resolved.unlink()
    except OSError as exc:
        record("delete_failed", path=file_path, job_id=job_id, reason=f"{reason}: {exc}",
               workspace=workspace, status_file=status_file)
        return False
    record("deleted", path=file_path, job_id=job_id, reason=reason,
           workspace=workspace, status_file=status_file)
    return True
