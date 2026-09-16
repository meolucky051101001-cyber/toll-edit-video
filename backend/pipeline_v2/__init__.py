"""Durable, opt-in pipeline v2 building blocks.

Legacy remains the default; heavyweight modules are loaded only after mode
selection or inside a short-lived worker process.
"""

from .artifact_store import ArtifactStore, ArtifactValidation
from .manifest import ManifestStore
from .models import (
    DEFAULT_STAGE_ORDER,
    ArtifactRecord,
    FingerprintSet,
    JobManifest,
    StageRecord,
    fingerprint_json,
)
from .stage_status import InvalidStageTransition, StageStatus

__all__ = [
    "ArtifactRecord",
    "ArtifactStore",
    "ArtifactValidation",
    "DEFAULT_STAGE_ORDER",
    "FingerprintSet",
    "InvalidStageTransition",
    "JobManifest",
    "ManifestStore",
    "StageRecord",
    "StageStatus",
    "fingerprint_json",
]
