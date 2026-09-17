"""Checkpoint-aware, dependency-ordered pipeline orchestration."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .artifact_store import ArtifactStore
from .manifest import ManifestStore
from .models import ArtifactRecord, JobManifest
from .stage_status import StageStatus


@dataclass
class StageOutcome:
    artifacts: List[ArtifactRecord] = field(default_factory=list)
    values: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineContext:
    manifest: JobManifest
    manifest_store: ManifestStore
    artifact_store: ArtifactStore
    values: Dict[str, Any] = field(default_factory=dict)
    progress: Optional[Callable[[str, str], Any]] = None

    async def notify(self, stage: str, message: str) -> None:
        if self.progress is None:
            return
        result = self.progress(stage, message)
        if inspect.isawaitable(result):
            await result


StageHandler = Callable[[PipelineContext], Any]
StagePredicate = Callable[[PipelineContext], bool]


@dataclass(frozen=True)
class StageDefinition:
    name: str
    handler: StageHandler
    dependencies: Sequence[str] = ()
    enabled: Optional[StagePredicate] = None
    cacheable: bool = True
    skip_reason: str = "Disabled by pipeline configuration"


class PipelineOrchestrator:
    def __init__(self, stages: Iterable[StageDefinition]):
        self.stages = list(stages)
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("Pipeline stage names must be unique")
        known = set(names)
        for stage in self.stages:
            missing = set(stage.dependencies) - known
            if missing:
                raise ValueError(
                    "Stage {!r} has unknown dependencies: {}".format(
                        stage.name, sorted(missing)
                    )
                )

    async def run(self, context: PipelineContext) -> PipelineContext:
        recovered = context.manifest.recover_interrupted()
        if recovered:
            context.manifest_store.save(context.manifest)

        stage_order = [stage.name for stage in self.stages]
        for definition in self.stages:
            record = context.manifest.stage(definition.name)
            if record.status is StageStatus.COMPLETED:
                if not definition.cacheable or context.manifest_store.stage_cache_is_valid(
                    context.manifest, definition.name, context.artifact_store
                ):
                    await context.notify(definition.name, "cache_hit")
                    continue
                context.manifest.invalidate_from(definition.name, stage_order)
                context.manifest_store.save(context.manifest)
                record = context.manifest.stage(definition.name)
            elif record.status is StageStatus.SKIPPED:
                continue

            self._require_dependencies(context.manifest, definition)
            if definition.enabled is not None and not definition.enabled(context):
                if record.status is StageStatus.FAILED:
                    record.reset()
                context.manifest.skip_stage(definition.name, definition.skip_reason)
                context.manifest_store.save(context.manifest)
                await context.notify(definition.name, "skipped")
                continue

            if record.status is StageStatus.RUNNING:
                context.manifest.recover_interrupted()
                context.manifest_store.save(context.manifest)
            context.manifest.start_stage(definition.name)
            context.manifest_store.save(context.manifest)
            await context.notify(definition.name, "running")
            try:
                outcome = definition.handler(context)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                if outcome is None:
                    outcome = StageOutcome()
                if not isinstance(outcome, StageOutcome):
                    raise TypeError(
                        "Stage {!r} must return StageOutcome or None".format(
                            definition.name
                        )
                    )
                context.values.update(outcome.values)
                context.manifest.complete_stage(definition.name, outcome.artifacts)
                context.manifest_store.save(context.manifest)
                await context.notify(definition.name, "completed")
            except BaseException as exc:
                context.manifest.fail_stage(
                    definition.name, str(exc), error_type=type(exc).__name__
                )
                context.manifest_store.save(context.manifest)
                await context.notify(definition.name, "failed")
                raise
        return context

    @staticmethod
    def _require_dependencies(
        manifest: JobManifest, definition: StageDefinition
    ) -> None:
        incomplete = [
            name
            for name in definition.dependencies
            if manifest.stage(name).status
            not in {StageStatus.COMPLETED, StageStatus.SKIPPED}
        ]
        if incomplete:
            raise RuntimeError(
                "Stage {!r} has incomplete dependencies: {}".format(
                    definition.name, incomplete
                )
            )


