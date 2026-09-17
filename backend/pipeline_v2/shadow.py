"""Read-only observation helpers for legacy pipeline runs."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from .artifact_store import hash_file
from .manifest import ManifestStore
from .models import FingerprintSet, JobManifest, fingerprint_json
from .stage_status import StageStatus


class ShadowRecorder:
    """Record legacy stage outcomes without changing legacy artifacts."""

    def __init__(
        self,
        directory: Path,
        job_id: str,
        source_path: Path,
        stage_names: Sequence[str],
        config: Optional[Mapping[str, Any]] = None,
    ):
        self.directory = Path(directory)
        self.store = ManifestStore(self.directory)
        source_hash, _ = hash_file(source_path)
        fingerprints = FingerprintSet(
            source_sha256=source_hash,
            config_sha256=fingerprint_json(dict(config or {})),
            model_sha256={},
        )
        if self.store.exists():
            self.manifest = self.store.load()
        else:
            self.manifest = self.store.create(
                job_id=job_id,
                fingerprints=fingerprints,
                stage_names=stage_names,
                metadata={"mode": "shadow", "authoritative_output": "legacy"},
            )

    def start(self, stage: str) -> None:
        record = self.manifest.stage(stage)
        if record.status is StageStatus.RUNNING:
            self.manifest.recover_interrupted()
        if record.status in {StageStatus.COMPLETED, StageStatus.SKIPPED}:
            record.reset()
        self.manifest.start_stage(stage)
        self.store.save(self.manifest)

    def complete(
        self,
        stage: str,
        paths: Optional[Mapping[str, Path]] = None,
        timing: Optional[Mapping[str, Any]] = None,
    ) -> None:
        snapshot: Dict[str, Any] = {}
        for label, path in (paths or {}).items():
            candidate = Path(path)
            if candidate.is_file():
                sha256, size = hash_file(candidate)
                snapshot[label] = {
                    "path": str(candidate),
                    "sha256": sha256,
                    "size_bytes": size,
                }
            elif candidate.is_dir():
                snapshot[label] = {
                    "path": str(candidate),
                    "file_count": sum(1 for item in candidate.rglob("*") if item.is_file()),
                    "directory": True,
                }
            else:
                snapshot[label] = {"path": str(candidate), "missing": True}
        record = self.manifest.stage(stage)
        record.metadata["legacy_artifacts"] = snapshot
        if timing:
            record.metadata["legacy_timing"] = dict(timing)
        self.manifest.complete_stage(stage)
        self.store.save(self.manifest)

    def fail(self, stage: str, error: BaseException) -> None:
        self.manifest.fail_stage(stage, str(error), type(error).__name__)
        self.store.save(self.manifest)


def snapshot_completed_legacy_run(
    source_path: Path,
    shadow_directory: Path,
    stage_artifacts: Mapping[str, Mapping[str, Path]],
    run_started_at_epoch: Optional[float] = None,
) -> Path:
    """Build a post-run shadow manifest without importing it into legacy code."""

    recorder = ShadowRecorder(
        directory=Path(shadow_directory),
        job_id=Path(shadow_directory).parent.name,
        source_path=Path(source_path),
        stage_names=tuple(stage_artifacts.keys()),
        config={"mode": "shadow", "authoritative_output": "legacy"},
    )
    previous_epoch = float(run_started_at_epoch or time.time())
    for stage, paths in stage_artifacts.items():
        record = recorder.manifest.stage(stage)
        if record.status is StageStatus.COMPLETED:
            continue
        recorder.start(stage)
        missing = [str(path) for path in paths.values() if not Path(path).exists()]
        if missing:
            recorder.fail(stage, FileNotFoundError(", ".join(missing)))
        else:
            modified_epochs = []
            for path in paths.values():
                candidate = Path(path)
                if candidate.is_file():
                    modified_epochs.append(candidate.stat().st_mtime)
                elif candidate.is_dir():
                    modified_epochs.extend(
                        item.stat().st_mtime
                        for item in candidate.rglob("*")
                        if item.is_file()
                    )
            completed_epoch = max(modified_epochs, default=time.time())
            recorder.complete(
                stage,
                paths,
                timing={
                    "elapsed_seconds": max(0.0, completed_epoch - previous_epoch),
                    "completed_at_epoch": completed_epoch,
                    "source": "artifact_mtime_delta",
                },
            )
            previous_epoch = max(previous_epoch, completed_epoch)
    if run_started_at_epoch is not None:
        recorder.manifest.metadata["legacy_total_elapsed_seconds"] = max(
            0.0, time.time() - float(run_started_at_epoch)
        )
        recorder.store.save(recorder.manifest)
    return recorder.store.path


