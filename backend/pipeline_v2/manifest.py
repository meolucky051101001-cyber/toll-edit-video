"""Atomic persistence and cache validation for job manifests."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TypeVar, Union

from .artifact_store import ArtifactStore
from .atomic_io import atomic_write_json
from .models import DEFAULT_STAGE_ORDER, FingerprintSet, JobManifest, utc_now
from .stage_status import StageStatus


PathLike = Union[str, Path]
T = TypeVar("T")


class ManifestStore:
    """Persist one ``job_manifest.json`` with atomic replace semantics."""

    def __init__(self, job_directory: PathLike, filename: str = "job_manifest.json"):
        self.job_directory = Path(job_directory)
        self.path = self.job_directory / filename
        self._lock = threading.RLock()

    def exists(self) -> bool:
        return self.path.is_file()

    def create(
        self,
        job_id: str,
        fingerprints: FingerprintSet,
        stage_names: Sequence[str] = DEFAULT_STAGE_ORDER,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> JobManifest:
        with self._lock:
            if self.exists():
                raise FileExistsError("Job manifest already exists: {}".format(self.path))
            manifest = JobManifest.new(
                job_id=job_id,
                fingerprints=fingerprints,
                stage_names=stage_names,
                metadata=metadata,
            )
            self.save(manifest)
            return manifest

    def load(self) -> JobManifest:
        with self._lock:
            with self.path.open("r", encoding="utf-8") as handle:
                return JobManifest.from_dict(json.load(handle))

    def save(self, manifest: JobManifest) -> None:
        with self._lock:
            old_revision = manifest.revision
            old_updated_at = manifest.updated_at
            manifest.revision = old_revision + 1
            manifest.updated_at = utc_now()
            try:
                atomic_write_json(self.path, manifest.to_dict())
            except BaseException:
                manifest.revision = old_revision
                manifest.updated_at = old_updated_at
                raise

    def mutate(self, callback: Callable[[JobManifest], T]) -> T:
        """Load, mutate and atomically save under the in-process lock."""

        with self._lock:
            manifest = self.load()
            result = callback(manifest)
            self.save(manifest)
            return result

    def recover_interrupted(self) -> Sequence[str]:
        with self._lock:
            manifest = self.load()
            recovered = manifest.recover_interrupted()
            if recovered:
                self.save(manifest)
            return recovered

    def stage_cache_is_valid(
        self, manifest: JobManifest, stage_name: str, artifacts: ArtifactStore
    ) -> bool:
        stage = manifest.stage(stage_name)
        if stage.status is not StageStatus.COMPLETED or not stage.artifact_keys:
            return False
        for key in stage.artifact_keys:
            record = manifest.artifacts.get(key)
            if record is None or not artifacts.validate(record).valid:
                return False
        return True
