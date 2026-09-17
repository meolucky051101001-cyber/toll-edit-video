"""Fail-closed validation for heavyweight media stage outputs."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union


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


def validate_transformed_audio(
    input_audio: PathLike, output_audio: PathLike, stage_name: str
) -> None:
    source = _require_nonempty_file(input_audio, "{} input".format(stage_name))
    output = _require_nonempty_file(output_audio, "{} output".format(stage_name))
    if files_are_identical(source, output):
        raise RuntimeError(
            "{} returned an unchanged copy of its input".format(stage_name)
        )


def is_real_rvc_model(path: PathLike) -> bool:
    candidate = Path(path)
    if not candidate.is_file() or candidate.stat().st_size <= 1024:
        return False
    try:
        with candidate.open("rb") as handle:
            prefix = handle.read(128)
    except OSError:
        return False
    return b"git-lfs.github.com/spec" not in prefix


def discover_rvc_index_file(model_path: PathLike) -> Optional[Path]:
    """Return the RVC feature index that inference would select, if present."""

    model = Path(model_path)
    exact = model.with_suffix(".index")
    if exact.is_file() and exact.stat().st_size > 1024:
        return exact
    matches = [
        candidate
        for candidate in model.parent.glob("*.index")
        if model.stem.lower() in candidate.stem.lower()
        and candidate.stat().st_size > 1024
    ]
    if not matches:
        return None
    matches.sort(
        key=lambda candidate: (
            0 if candidate.name.lower().startswith("added_") else 1,
            candidate.name.lower(),
        )
    )
    return matches[0]
