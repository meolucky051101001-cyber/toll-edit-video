"""Atomic artifact publishing and integrity checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Union

from .atomic_io import atomic_replace_file, atomic_write_bytes
from .models import ArtifactRecord


PathLike = Union[str, os.PathLike]


def hash_file(path: PathLike, chunk_size: int = 1024 * 1024) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class ArtifactValidation:
    valid: bool
    reason: str
    actual_sha256: Optional[str] = None
    actual_size_bytes: Optional[int] = None


class ArtifactStore:
    def __init__(self, root: PathLike):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        if not key or "\x00" in key:
            raise ValueError("Artifact key must not be empty or contain NUL")
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Artifact key escapes store root: {!r}".format(key)) from exc
        if candidate == self.root:
            raise ValueError("Artifact key must identify a file")
        return candidate

    def staging_path(self, key: str) -> Path:
        destination = self.path_for(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination.parent / ".{}.{}.partial".format(
            destination.name, uuid.uuid4().hex
        )

    def commit_staged(
        self,
        staged_path: PathLike,
        key: str,
        metadata: Optional[Mapping[str, Any]] = None,
        expected_sha256: Optional[str] = None,
    ) -> ArtifactRecord:
        destination = self.path_for(key)
        staged = Path(staged_path).resolve()
        if staged.parent != destination.parent.resolve():
            raise ValueError("Staged artifact must be a sibling of its destination")
        if not staged.is_file():
            raise FileNotFoundError("Staged artifact does not exist: {}".format(staged))

        sha256, size = hash_file(staged)
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ValueError("Staged artifact SHA-256 does not match expected value")

        atomic_replace_file(staged, destination)
        return ArtifactRecord(
            key=key.replace("\\", "/"),
            sha256=sha256,
            size_bytes=size,
            metadata=dict(metadata or {}),
        )

    def put_bytes(
        self,
        key: str,
        data: bytes,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        destination = self.path_for(key)
        atomic_write_bytes(destination, data)
        sha256, size = hash_file(destination)
        return ArtifactRecord(
            key=key.replace("\\", "/"),
            sha256=sha256,
            size_bytes=size,
            metadata=dict(metadata or {}),
        )

    def put_file(
        self,
        key: str,
        source_path: PathLike,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        source = Path(source_path)
        if not source.is_file():
            raise FileNotFoundError("Artifact source is missing: {}".format(source))
        staged = self.staging_path(key)
        try:
            shutil.copy2(source, staged)
            return self.commit_staged(staged, key, metadata=metadata)
        except BaseException:
            try:
                staged.unlink()
            except FileNotFoundError:
                pass
            raise

    def put_text(
        self,
        key: str,
        text: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        return self.put_bytes(key, text.encode("utf-8"), metadata=metadata)

    def put_json(
        self,
        key: str,
        value: Any,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
        return self.put_text(key, payload, metadata=metadata)

    def validate(self, record: ArtifactRecord) -> ArtifactValidation:
        try:
            path = self.path_for(record.key)
        except ValueError as exc:
            return ArtifactValidation(valid=False, reason=str(exc))
        if not path.is_file():
            return ArtifactValidation(valid=False, reason="artifact_missing")
        actual_sha256, actual_size = hash_file(path)
        if actual_size != record.size_bytes:
            return ArtifactValidation(
                valid=False,
                reason="size_mismatch",
                actual_sha256=actual_sha256,
                actual_size_bytes=actual_size,
            )
        if actual_sha256 != record.sha256:
            return ArtifactValidation(
                valid=False,
                reason="sha256_mismatch",
                actual_sha256=actual_sha256,
                actual_size_bytes=actual_size,
            )
        return ArtifactValidation(
            valid=True,
            reason="ok",
            actual_sha256=actual_sha256,
            actual_size_bytes=actual_size,
        )

    def record_existing(
        self,
        key: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> ArtifactRecord:
        """Create an integrity record for an already atomically published artifact."""

        path = self.path_for(key)
        if not path.is_file():
            raise FileNotFoundError("Artifact is missing: {}".format(path))
        sha256, size = hash_file(path)
        return ArtifactRecord(
            key=key.replace("\\", "/"),
            sha256=sha256,
            size_bytes=size,
            metadata=dict(metadata or {}),
        )


