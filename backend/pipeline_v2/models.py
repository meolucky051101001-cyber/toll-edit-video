"""Serializable models for checkpointed pipeline jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .stage_status import StageStatus, require_transition


MANIFEST_SCHEMA_VERSION = 1

DEFAULT_STAGE_ORDER = (
    "input",
    "download",
    "extract_audio",
    "demucs",
    "transcribe",
    "ocr",
    "translate",
    "tts",
    "rvc",
    "subtitles",
    "mix",
    "render",
    "qc",
    "deliver",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def fingerprint_json(value: Any) -> str:
    """Return a stable SHA-256 for JSON-compatible configuration data."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FingerprintSet:
    source_sha256: str
    config_sha256: str
    model_sha256: Mapping[str, str] = field(default_factory=dict)

    @property
    def combined_sha256(self) -> str:
        return fingerprint_json(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_sha256": self.source_sha256,
            "config_sha256": self.config_sha256,
            "model_sha256": dict(sorted(self.model_sha256.items())),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FingerprintSet":
        return cls(
            source_sha256=str(data["source_sha256"]),
            config_sha256=str(data["config_sha256"]),
            model_sha256={
                str(key): str(value)
                for key, value in dict(data.get("model_sha256", {})).items()
            },
        )


@dataclass(frozen=True)
class ArtifactRecord:
    key: str
    sha256: str
    size_bytes: int
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArtifactRecord":
        return cls(
            key=str(data["key"]),
            sha256=str(data["sha256"]),
            size_bytes=int(data["size_bytes"]),
            created_at=str(data["created_at"]),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class StageRecord:
    name: str
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    input_fingerprint: Optional[str] = None
    artifact_keys: List[str] = field(default_factory=list)
    error: Optional[Dict[str, str]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def transition(self, target: StageStatus) -> None:
        require_transition(self.status, target)
        self.status = target

    def start(self, input_fingerprint: Optional[str] = None) -> None:
        self.transition(StageStatus.RUNNING)
        self.attempts += 1
        self.started_at = utc_now()
        self.finished_at = None
        self.error = None
        self.input_fingerprint = input_fingerprint
        self.artifact_keys = []

    def complete(self, artifact_keys: Iterable[str] = ()) -> None:
        self.transition(StageStatus.COMPLETED)
        self.finished_at = utc_now()
        self.artifact_keys = list(dict.fromkeys(artifact_keys))

    def fail(self, message: str, error_type: str = "StageError") -> None:
        self.transition(StageStatus.FAILED)
        self.finished_at = utc_now()
        self.error = {"type": error_type, "message": message}

    def skip(self, reason: str) -> None:
        self.transition(StageStatus.SKIPPED)
        self.finished_at = utc_now()
        self.metadata["skip_reason"] = reason

    def reset(self) -> None:
        """Explicitly invalidate this stage without deleting old artifacts."""

        self.status = StageStatus.PENDING
        self.started_at = None
        self.finished_at = None
        self.input_fingerprint = None
        self.artifact_keys = []
        self.error = None
        self.metadata = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "input_fingerprint": self.input_fingerprint,
            "artifact_keys": list(self.artifact_keys),
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "StageRecord":
        return cls(
            name=str(data["name"]),
            status=StageStatus(str(data.get("status", StageStatus.PENDING.value))),
            attempts=int(data.get("attempts", 0)),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            input_fingerprint=data.get("input_fingerprint"),
            artifact_keys=[str(item) for item in data.get("artifact_keys", [])],
            error=dict(data["error"]) if data.get("error") else None,
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class JobManifest:
    job_id: str
    fingerprints: FingerprintSet
    stages: Dict[str, StageRecord]
    artifacts: Dict[str, ArtifactRecord] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def new(
        cls,
        job_id: str,
        fingerprints: FingerprintSet,
        stage_names: Sequence[str] = DEFAULT_STAGE_ORDER,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> "JobManifest":
        if not job_id.strip():
            raise ValueError("job_id must not be empty")
        unique_names = list(dict.fromkeys(stage_names))
        if not unique_names or any(not name.strip() for name in unique_names):
            raise ValueError("stage_names must contain non-empty unique names")
        return cls(
            job_id=job_id,
            fingerprints=fingerprints,
            stages={name: StageRecord(name=name) for name in unique_names},
            metadata=dict(metadata or {}),
        )

    def stage(self, name: str) -> StageRecord:
        try:
            return self.stages[name]
        except KeyError as exc:
            raise KeyError("Unknown pipeline stage: {!r}".format(name)) from exc

    def start_stage(
        self, name: str, input_fingerprint: Optional[str] = None
    ) -> StageRecord:
        record = self.stage(name)
        record.start(input_fingerprint=input_fingerprint)
        self.touch()
        return record

    def complete_stage(
        self, name: str, artifacts: Iterable[ArtifactRecord] = ()
    ) -> StageRecord:
        records = list(artifacts)
        for artifact in records:
            self.artifacts[artifact.key] = artifact
        record = self.stage(name)
        record.complete(artifact.key for artifact in records)
        self.touch()
        return record

    def fail_stage(
        self, name: str, message: str, error_type: str = "StageError"
    ) -> StageRecord:
        record = self.stage(name)
        record.fail(message=message, error_type=error_type)
        self.touch()
        return record

    def skip_stage(self, name: str, reason: str) -> StageRecord:
        record = self.stage(name)
        record.skip(reason=reason)
        self.touch()
        return record

    def recover_interrupted(self) -> List[str]:
        """Convert stages left running by a crash into retryable failures."""

        recovered = []
        for record in self.stages.values():
            if record.status is StageStatus.RUNNING:
                record.fail(
                    message="Stage was interrupted before checkpoint completion",
                    error_type="InterruptedStage",
                )
                recovered.append(record.name)
        if recovered:
            self.touch()
        return recovered

    def invalidate_from(
        self, name: str, stage_order: Optional[Sequence[str]] = None
    ) -> List[str]:
        order = list(stage_order or self.stages.keys())
        if name not in order:
            raise KeyError("Unknown pipeline stage in order: {!r}".format(name))
        invalidated = []
        for stage_name in order[order.index(name) :]:
            record = self.stage(stage_name)
            record.reset()
            invalidated.append(stage_name)
        self.touch()
        return invalidated

    def next_resumable_stage(
        self, stage_order: Optional[Sequence[str]] = None
    ) -> Optional[str]:
        order = list(stage_order or self.stages.keys())
        for name in order:
            status = self.stage(name).status
            if status in {StageStatus.RUNNING, StageStatus.FAILED, StageStatus.PENDING}:
                return name
        return None

    def is_cache_compatible(self, fingerprints: FingerprintSet) -> bool:
        return self.fingerprints.combined_sha256 == fingerprints.combined_sha256

    def touch(self) -> None:
        self.updated_at = utc_now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "job_id": self.job_id,
            "fingerprints": self.fingerprints.to_dict(),
            "stages": {name: stage.to_dict() for name, stage in self.stages.items()},
            "artifacts": {
                key: artifact.to_dict() for key, artifact in self.artifacts.items()
            },
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JobManifest":
        schema_version = int(data.get("schema_version", 0))
        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "Unsupported manifest schema version: {}".format(schema_version)
            )
        stages = {
            str(name): StageRecord.from_dict(stage_data)
            for name, stage_data in dict(data["stages"]).items()
        }
        for name, stage in stages.items():
            if name != stage.name:
                raise ValueError("Stage key and stage name do not match: {!r}".format(name))
        return cls(
            schema_version=schema_version,
            revision=int(data.get("revision", 0)),
            job_id=str(data["job_id"]),
            fingerprints=FingerprintSet.from_dict(data["fingerprints"]),
            stages=stages,
            artifacts={
                str(key): ArtifactRecord.from_dict(artifact_data)
                for key, artifact_data in dict(data.get("artifacts", {})).items()
            },
            metadata=dict(data.get("metadata", {})),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )
