"""Commit a prepared segment revision, rolling back all completed writes on failure."""
from __future__ import annotations

import shutil
from pathlib import Path

from pipeline_v2.atomic_io import atomic_copy_file, atomic_write_json
from v1_integrity_errors import SegmentCommitError


def commit_segment_files(prepared: dict[Path, Path], backup_dir: Path) -> None:
    """Each replace is atomic; a journal/backups survive if rollback itself fails."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    originals: dict[Path, Path | None] = {}
    try:
        for index, destination in enumerate(prepared):
            if destination.is_symlink() or destination.exists() and not destination.is_file():
                raise SegmentCommitError("Đường dẫn artifact không phải file thông thường.")
            backup = backup_dir / f"{index}.original" if destination.is_file() else None
            if backup is not None:
                shutil.copy2(destination, backup)
            originals[destination] = backup
        atomic_write_json(backup_dir / "recovery.json", {
            "version": 1,
            "files": [{"destination": str(p), "original": str(b) if b is not None else None}
                      for p, b in originals.items()],
        })
    except Exception as exc:
        raise SegmentCommitError("Không tạo được bản sao khôi phục; chưa ghi revision mới.") from exc

    applied = []
    try:
        for destination, staged in prepared.items():
            # A copy helper may publish successfully and then fail during final sync.
            # Restore the attempted destination as well as earlier completed writes.
            applied.append(destination)
            atomic_copy_file(staged, destination)
    except BaseException as exc:
        failed = False
        for destination in reversed(applied):
            try:
                original = originals[destination]
                if original is None:
                    destination.unlink(missing_ok=True)
                else:
                    atomic_copy_file(original, destination)
            except Exception:
                failed = True
        if failed:
            error = SegmentCommitError("Ghi revision lỗi và chưa khôi phục đủ; giữ lại bản sao recovery, chưa báo thành công.")
            error.recovery_dir = str(backup_dir.parent)
            raise error from exc
        if not isinstance(exc, Exception):
            raise
        raise SegmentCommitError("Ghi revision thất bại; các artifact đã ghi được khôi phục nguyên trạng.") from exc
