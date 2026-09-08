"""Discover and resume interrupted v2 jobs after a bot restart."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import PipelineMode, PipelineSettings
from .artifact_store import hash_file
from .manifest import ManifestStore
from .stage_validation import is_real_rvc_model
from .stage_status import StageStatus
from .video_pipeline import (
    V2_STAGE_ORDER,
    VideoPipelineRequest,
    VideoPipelineResult,
    VideoPipelineRunner,
    discover_rvc_model,
)


ResumeProgress = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class ResumableVideoJob:
    job_id: str
    manifest_path: Path
    job_directory: Path
    source_path: Path
    output_path: Path
    delivery_copy_path: Optional[Path]
    target_lang: str
    voice_source: str
    voice_param: str
    rvc_model_path: Optional[Path]
    clean_audio_hint: Optional[bool]
    delogo: bool
    next_stage: str


def _published_outputs_present(manifest: Any) -> bool:
    deliver = manifest.stage("deliver")
    if deliver.status is not StageStatus.COMPLETED:
        return False
    outputs = deliver.metadata.get("published_outputs") or [
        deliver.metadata.get("published_output", {})
    ]
    if not outputs:
        return False
    for item in outputs:
        try:
            candidate = Path(str(item["path"]))
            expected_size = int(item["size_bytes"])
            expected_sha256 = str(item["sha256"])
        except (KeyError, TypeError, ValueError):
            return False
        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            return False
        actual_sha256, _ = hash_file(candidate)
        if actual_sha256 != expected_sha256:
            return False
    return True


def find_resumable_jobs(workspace: Path) -> List[ResumableVideoJob]:
    """Read manifests only; malformed or obsolete jobs never stop bot startup."""

    jobs = []
    for manifest_path in sorted(Path(workspace).glob("*/pipeline_v2/job_manifest.json")):
        try:
            manifest = ManifestStore(manifest_path.parent).load()
            if set(manifest.stages) != set(V2_STAGE_ORDER):
                continue
            if _published_outputs_present(manifest):
                continue
            metadata: Dict[str, Any] = dict(manifest.metadata)
            request = dict(metadata.get("request", {}))
            source_path = Path(str(metadata["source_path"]))
            output_path = Path(str(request["output_path"]))
            if not source_path.is_file():
                continue
            next_stage = manifest.next_resumable_stage(V2_STAGE_ORDER) or "deliver"
            jobs.append(
                ResumableVideoJob(
                    job_id=manifest.job_id,
                    manifest_path=manifest_path,
                    job_directory=manifest_path.parent.parent,
                    source_path=source_path,
                    output_path=output_path,
                    delivery_copy_path=(
                        Path(str(request["delivery_copy_path"]))
                        if request.get("delivery_copy_path")
                        else None
                    ),
                    target_lang=str(request.get("target_lang", "vi")),
                    voice_source=str(request.get("voice_source", "edge")),
                    voice_param=str(
                        request.get("voice_param", "vi-VN-HoaiMyNeural")
                    ),
                    rvc_model_path=(
                        Path(str(request["rvc_model_path"]))
                        if request.get("rvc_model_path")
                        else None
                    ),
                    clean_audio_hint=request.get("clean_audio_hint"),
                    delogo=bool(request.get("delogo", False)),
                    next_stage=next_stage,
                )
            )
        except (OSError, KeyError, TypeError, ValueError):
            continue
    return jobs


async def resume_video_job(
    job: ResumableVideoJob,
    settings: PipelineSettings,
    api_key: str = "",
    tts_api_key: str = "",
    progress: Optional[ResumeProgress] = None,
) -> VideoPipelineResult:
    if settings.mode is not PipelineMode.V2:
        raise RuntimeError("Automatic resume is active only in PIPELINE_MODE=v2")
    # Resume must preserve the voice provider recorded in the manifest.  A
    # globally discoverable RVC model must never turn an Edge/FPT job into an
    # RVC job, and an RVC job must not silently fall back to a different voice.
    voice_source = job.voice_source
    voice_param = job.voice_param
    rvc_model = None
    if voice_source == "rvc":
        rvc_model = job.rvc_model_path
        if not rvc_model or not is_real_rvc_model(rvc_model):
            rvc_model = discover_rvc_model(job.job_directory.parent)
        if not rvc_model:
            raise RuntimeError(
                "Cannot resume RVC job: no real .pth model is available"
            )
        voice_param = str(rvc_model)

    async def report(stage: str, state: str) -> None:
        if progress is None:
            return
        result = progress(job.job_id, stage, state)
        if inspect.isawaitable(result):
            await result

    request = VideoPipelineRequest(
        video_path=job.source_path,
        job_directory=job.job_directory,
        output_path=job.output_path,
        delivery_copy_path=job.delivery_copy_path,
        settings=settings,
        api_key=api_key,
        tts_api_key=tts_api_key,
        target_lang=job.target_lang,
        voice_source=voice_source,
        voice_param=voice_param,
        rvc_model_path=rvc_model,
        clean_audio_hint=job.clean_audio_hint,
        delogo=job.delogo,
        progress=report,
    )
    return await VideoPipelineRunner(request).run()
