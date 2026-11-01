"""Fail-closed validation for heavyweight media stage outputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Union


PathLike = Union[str, Path]


def _require_nonempty_file(path: PathLike, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_file():
        raise RuntimeError("{} output is missing: {}".format(label, candidate))
    if candidate.stat().st_size <= 0:
        raise RuntimeError("{} output is empty: {}".format(label, candidate))
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_are_identical(left: PathLike, right: PathLike) -> bool:
    first = _require_nonempty_file(left, "First")
    second = _require_nonempty_file(right, "Second")
    try:
        if first.resolve() == second.resolve():
            return True
    except OSError:
        pass
    if first.stat().st_size != second.stat().st_size:
        return False
    return _sha256(first) == _sha256(second)


def validate_demucs_outputs(
    input_audio: PathLike, vocals: PathLike, background: PathLike
) -> None:
    source = _require_nonempty_file(input_audio, "Demucs input")
    voice = _require_nonempty_file(vocals, "Demucs vocals")
    music = _require_nonempty_file(background, "Demucs background")
    if files_are_identical(source, voice) or files_are_identical(source, music):
        raise RuntimeError(
            "Demucs returned the source audio as a stem; refusing degraded output"
        )
    if files_are_identical(voice, music):
        raise RuntimeError("Demucs returned identical vocals and background stems")
