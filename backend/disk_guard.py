"""Fail fast when destination drives cannot hold another render.

A full drive mid-render produces truncated audio/video and wastes GPU time, so
jobs check free space before starting heavy work instead of surfacing ENOSPC.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

DEFAULT_MIN_FREE_GB = 3.0


class InsufficientDiskSpace(RuntimeError):
    pass


def _existing_anchor(path: Path) -> Path:
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def ensure_free_space(*paths, min_free_gb: float | None = None, purpose: str = "xử lý video") -> None:
    minimum = float(min_free_gb if min_free_gb is not None else os.getenv("AUTODUB_MIN_FREE_GB", DEFAULT_MIN_FREE_GB))
    free_by_drive: dict[str, int] = {}
    for raw in paths:
        if not raw:
            continue
        anchor = _existing_anchor(Path(raw).expanduser().resolve())
        try:
            free = shutil.disk_usage(anchor).free
        except OSError:
            continue
        drive = anchor.drive or str(anchor)
        free_by_drive[drive] = min(free, free_by_drive.get(drive, free))
    insufficient = {drive: free for drive, free in free_by_drive.items() if free < minimum * 2**30}
    if insufficient:
        detail = ", ".join(f"{drive} còn {free / 2**30:.1f} GB" for drive, free in sorted(insufficient.items()))
        raise InsufficientDiskSpace(
            f"Không đủ dung lượng để {purpose}: {detail}. "
            f"Cần tối thiểu {minimum:.0f} GB trống mỗi ổ — hãy dọn workspace hoặc đổi thư mục đích."
        )
