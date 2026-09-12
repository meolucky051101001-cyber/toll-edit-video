"""Crash-safe atomic file writes using a temporary sibling and os.replace."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import logging
from pathlib import Path
from typing import Any, Union


PathLike = Union[str, os.PathLike]


def _replace_with_retry(staged: Path, destination: Path) -> None:
    """Tolerate brief Windows reader/AV locks without removing the old file."""
    for attempt in range(8):
        try:
            os.replace(str(staged), str(destination))
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in {5, 32, 33} or attempt == 7:
                raise
            time.sleep(min(0.05 * (2 ** attempt), 0.5))


def _sync_parent_directory(path: Path) -> None:
    """Best-effort directory sync on platforms that expose O_DIRECTORY."""

    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace_file(staged_path: PathLike, destination_path: PathLike) -> Path:
    """Flush and atomically publish an already-written sibling file."""

    staged = Path(staged_path)
    destination = Path(destination_path)
    if staged.parent.resolve() != destination.parent.resolve():
        raise ValueError("Atomic replacement requires source and destination siblings")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with staged.open("ab") as handle:
        os.fsync(handle.fileno())
    _replace_with_retry(staged, destination)
    _sync_parent_directory(destination.parent)
    return destination


def atomic_write_bytes(path: PathLike, data: bytes) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(destination.name),
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace_file(temporary_path, destination)
        return destination
    except BaseException:
        try:
            temporary_path.unlink()
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                logging.getLogger(__name__).warning('Could not remove temporary file %s: %s', temporary_path, exc)
        raise


def atomic_write_text(
    path: PathLike, text: str, encoding: str = "utf-8", newline: str = "\n"
) -> Path:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        normalized = normalized.replace("\n", newline)
    return atomic_write_bytes(path, normalized.encode(encoding))


def atomic_write_json(path: PathLike, value: Any) -> Path:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    return atomic_write_text(path, payload + "\n")


def atomic_copy_file(source_path: PathLike, destination_path: PathLike) -> Path:
    """Copy a file to a temporary sibling, then publish it atomically."""

    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise FileNotFoundError("Source file does not exist: {}".format(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(destination.name),
        suffix=".copying",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        return atomic_replace_file(temporary, destination)
    except BaseException:
        try:
            temporary.unlink()
        except OSError as exc:
            if not isinstance(exc, FileNotFoundError):
                logging.getLogger(__name__).warning('Could not remove temporary file %s: %s', temporary, exc)
        raise

