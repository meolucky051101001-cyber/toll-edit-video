"""End-to-end opt-in video pipeline assembled from the v2 building blocks."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from .adaptive import (
    decide_demucs,
    decide_ocr,
    probe_video_dimensions,
    probe_video_duration,
)
from .artifact_store import ArtifactStore, hash_file
from .atomic_io import atomic_copy_file, atomic_replace_file
from .batching import bounded_batches, chunked
from .config import PipelineSettings
from .gpu_executor import GPUStageExecutor
from .manifest import ManifestStore
from .mixer import FFmpegMixSettings, mix_audio_ffmpeg
from .models import ArtifactRecord, FingerprintSet, JobManifest, fingerprint_json
from .qc import QCSettings, evaluate_qc_gate, run_report_only_qc
from .segments import RuntimeSegment, segment_from_dict, segment_to_dict, segments_from_dicts, segments_to_dicts
from .stage_validation import (
    discover_rvc_index_file,
    is_real_rvc_model,
    validate_demucs_outputs,
)
from .stage_status import StageStatus
from .timing import (
    GeminiTimingRewriter,
    TimingPolicy,
    fit_audio_to_window,
    plan_actual_timing_rewrites,
    solve_segment_timing,
)
from .tts import generate_tts_audio_v2


V2_STAGE_ORDER = (
    "input",
    "extract_audio",
    "demucs",
    "transcribe",
    "ocr",
    "translate",
    "timing",
    "tts",
    "rvc",
    "subtitles",
    "mix_legacy",
    "mix_v2",
    "render",
    "qc",
    "deliver",
)

# Bump this value whenever artifact semantics change.  It participates in the
# manifest fingerprint so an upgraded runner cannot silently reuse output from
# an older implementation that happened to have the same environment flags.
PIPELINE_IMPLEMENTATION_VERSION = "2.2.1"


class QCGateBlocked(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoPipelineRequest:
    video_path: Path
    job_directory: Path
    output_path: Path
    settings: PipelineSettings
    delivery_copy_path: Optional[Path] = None
    api_key: str = ""
    tts_api_key: str = ""
    target_lang: str = "vi"
    voice_source: str = "edge"
    voice_param: str = "vi-VN-HoaiMyNeural"
    rvc_model_path: Optional[Path] = None
    clean_audio_hint: Optional[bool] = None
    delogo: bool = False
    progress: Optional[Callable[[str, str], Any]] = None


@dataclass(frozen=True)
class VideoPipelineResult:
    final_video: Path
    manifest_path: Path
    qc_report_path: Path
    qc_allowed: bool
    qc_reason: str


def _prepare_legacy_imports() -> None:
    backend_directory = Path(__file__).resolve().parents[1]
    if str(backend_directory) not in sys.path:
        sys.path.insert(0, str(backend_directory))


def _format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return "{:02d}:{:02d}:{:02d},{:03d}".format(hours, minutes, secs, millis)


def compose_srt(segments: Iterable[Any]) -> str:
    blocks = []
    for position, segment in enumerate(segments, 1):
        blocks.append(
            "{}\n{} --> {}\n{}".format(
                int(getattr(segment, "index", position)),
                _format_srt_timestamp(segment.start.total_seconds()),
                _format_srt_timestamp(segment.end.total_seconds()),
                str(segment.content).strip(),
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def validate_translated_batch(
    source: Sequence[RuntimeSegment], translated: Sequence[RuntimeSegment]
) -> None:
    """Reject structurally incomplete or unchanged-CJK translation checkpoints."""

    if len(source) != len(translated):
        raise RuntimeError("Translation batch changed the segment count")
    for original, result in zip(source, translated):
        if int(original.index) != int(result.index):
            raise RuntimeError("Translation batch changed segment identity")
        source_text = str(original.content).strip()
        translated_text = str(result.content).strip()
        if not translated_text:
            raise RuntimeError(
                "Translation returned empty text for segment {}".format(original.index)
            )
        contains_cjk = any("\u4e00" <= character <= "\u9fff" for character in source_text)
        if contains_cjk and translated_text == source_text:
            raise RuntimeError(
                "Translation left CJK source unchanged for segment {}".format(
                    original.index
                )
            )


def merge_ocr_geometry(
    translated: Iterable[Any], ocr_segments: Iterable[Any]
) -> List[RuntimeSegment]:
    geometry = {
        int(segment.index): segment_from_dict(segment_to_dict(segment))
        for segment in ocr_segments
    }
    merged = []
    for translated_segment in translated:
        segment = segment_from_dict(segment_to_dict(translated_segment))
        source = geometry.get(int(segment.source_segment_id or segment.index)) or geometry.get(
            int(segment.index)
        )
        if source is not None:
            segment.y_pct = source.y_pct
            segment.max_y_pct = source.max_y_pct
            segment.best_block = source.best_block
            segment.tracking_blocks = source.tracking_blocks
        merged.append(segment)
    return merged


def discover_rvc_model(workspace: Path) -> Optional[Path]:
    workspace_path = Path(workspace).resolve()
    search_dirs = [
        workspace_path.parent / "MyVoiceModel_v2",
        workspace_path / "MyVoiceModel_v2",
        workspace_path.parent / "models" / "rvc",
        workspace_path / "models" / "rvc",
    ]
    for model_directory in search_dirs:
        if not model_directory.is_dir():
            continue
        for candidate in sorted(model_directory.glob("*.pth")):
            if is_real_rvc_model(candidate):
                return candidate
    return None


class VideoPipelineRunner:
    def __init__(self, request: VideoPipelineRequest):
        self.request = request
        self.video_path = Path(request.video_path).resolve()
        output_path = Path(request.output_path).resolve()
        if output_path == self.video_path:
            raise ValueError("Pipeline output_path must not overwrite the input video")
        if (
            request.delivery_copy_path
            and Path(request.delivery_copy_path).resolve() == self.video_path
        ):
            raise ValueError("Pipeline delivery_copy_path must not overwrite the input video")
        if request.voice_source not in {"edge", "fpt", "rvc"}:
            raise ValueError("voice_source must be edge, fpt or rvc")
        if request.voice_source == "fpt" and not (
            request.tts_api_key or request.api_key
        ):
            raise ValueError("FPT voice requires an API key")
        if request.voice_source == "rvc" and (
            not request.settings.enable_rvc
            or not request.rvc_model_path
            or not is_real_rvc_model(request.rvc_model_path)
        ):
            raise ValueError("RVC voice requires an enabled, real .pth model file")
        self.job_directory = Path(request.job_directory).resolve()
        self.v2_directory = self.job_directory / "pipeline_v2"
        self.artifact_store = ArtifactStore(self.v2_directory / "artifacts")
        self.manifest_store = ManifestStore(self.v2_directory)
        self.work_directory = self.v2_directory / "work"
        self.work_directory.mkdir(parents=True, exist_ok=True)
        self.gpu_executor = GPUStageExecutor(
            self.v2_directory / "control",
            self.job_directory.parent / "pipeline_v2_gpu.lock",
            lock_timeout_seconds=request.settings.gpu_lock_timeout_seconds,
            stage_timeout_seconds=request.settings.stage_timeout_seconds,
        )
        self.manifest: Optional[JobManifest] = None

    async def run(self) -> VideoPipelineResult:
        if not self.video_path.is_file():
            raise FileNotFoundError("Input video is missing: {}".format(self.video_path))
        self.manifest = self._load_or_create_manifest()
        recovered = self.manifest.recover_interrupted()
        if recovered:
            self.manifest_store.save(self.manifest)

        await self._execute("input", self._input_stage, allow_empty=True)
        await self._execute("extract_audio", self._extract_audio_stage)

        original_audio = self._artifact_path("audio/original.wav")
        if self.request.settings.enable_adaptive_demucs:
            demucs_decision = await asyncio.to_thread(
                decide_demucs, original_audio, self.request.clean_audio_hint
            )
            self.manifest.stage("demucs").metadata["adaptive_decision"] = {
                "should_run": demucs_decision.should_run,
                "reason": demucs_decision.reason,
                "confidence": demucs_decision.confidence,
                "metrics": dict(demucs_decision.metrics),
            }
            if not demucs_decision.should_run:
                self._skip("demucs", demucs_decision.reason)
            else:
                await self._execute("demucs", self._demucs_stage)
        else:
            await self._execute("demucs", self._demucs_stage)

        await self._execute("transcribe", self._transcribe_stage)
        transcript = self._load_segments("transcript/segments.json")

        ocr_should_run = True
        if self.request.settings.enable_adaptive_ocr:
            ocr_decision = await asyncio.to_thread(decide_ocr, self.video_path)
            self.manifest.stage("ocr").metadata["adaptive_decision"] = {
                "should_run": ocr_decision.should_run,
                "reason": ocr_decision.reason,
                "confidence": ocr_decision.confidence,
                "metrics": dict(ocr_decision.metrics),
            }
            ocr_should_run = ocr_decision.should_run
            if not ocr_should_run:
                self._skip("ocr", ocr_decision.reason)

        if (
            ocr_should_run
            and self.request.settings.enable_parallel_ocr_gemini
            and not self._completed_valid("ocr")
            and not self._completed_valid("translate")
        ):
            await self._execute_parallel_context(transcript)
        else:
            if ocr_should_run:
                await self._execute("ocr", lambda: self._ocr_stage(transcript))
            await self._execute("translate", lambda: self._translate_stage(transcript))

        merged = self._merged_translated_segments(transcript)
        if self.request.settings.enable_timing_solver:
            await self._execute("timing", lambda: self._timing_stage(merged))
            timed_segments = self._load_segments("translation/timed_segments.json")
        else:
            self._skip("timing", "ENABLE_TIMING_SOLVER is false")
            timed_segments = merged

        await self._execute("tts", lambda: self._tts_stage(timed_segments))
        final_segments = self._segments_after_tts(timed_segments)
        if self._rvc_enabled():
            await self._execute("rvc", lambda: self._rvc_stage(final_segments))
        else:
            self._skip("rvc", "RVC model unavailable or disabled")

        await self._execute("subtitles", lambda: self._subtitles_stage(final_segments))
        if self.request.settings.enable_ffmpeg_mix_v2:
            if self.request.settings.enable_legacy_mix_ab:
                await self._execute("mix_legacy", self._mix_legacy_stage)
            else:
                self._skip("mix_legacy", "ENABLE_LEGACY_MIX_AB is false")
            await self._execute("mix_v2", self._mix_v2_stage)
        else:
            await self._execute("mix_legacy", self._mix_legacy_stage)
            self._skip("mix_v2", "ENABLE_FFMPEG_MIX_V2 is false")
        await self._execute("render", self._render_stage)
        await self._execute("qc", lambda: self._qc_stage(final_segments))

        report_payload = self._load_json("qc/qc_report.json")
        gate = evaluate_qc_gate(report_payload, self.request.settings.qc_gate_policy)
        if not gate.allowed:
            raise QCGateBlocked(
                "{}: {}".format(gate.reason, ", ".join(gate.blocking_checks))
            )
        await self._execute("deliver", self._deliver_stage, allow_empty=True)
        return VideoPipelineResult(
            final_video=Path(self.request.output_path),
            manifest_path=self.manifest_store.path,
            qc_report_path=self._artifact_path("qc/qc_report.json"),
            qc_allowed=gate.allowed,
            qc_reason=gate.reason,
        )

    def _load_or_create_manifest(self) -> JobManifest:
        source_hash, source_size = hash_file(self.video_path)
        model_fingerprints = {
            "whisper": fingerprint_json("large-v3:int8_float16"),
            "demucs": fingerprint_json("htdemucs:two-stems:segment=6"),
        }
        if self.request.rvc_model_path and is_real_rvc_model(self.request.rvc_model_path):
            model_fingerprints["rvc"], _ = hash_file(self.request.rvc_model_path)
            rvc_index = discover_rvc_index_file(self.request.rvc_model_path)
            if rvc_index is not None:
                model_fingerprints["rvc_index"], _ = hash_file(rvc_index)
        fingerprints = FingerprintSet(
            source_sha256=source_hash,
            config_sha256=fingerprint_json(
                {
                    **self.request.settings.cache_payload(),
                    "pipeline_implementation_version": PIPELINE_IMPLEMENTATION_VERSION,
                    "target_lang": self.request.target_lang,
                    "voice_source": self.request.voice_source,
                    "voice_param": self.request.voice_param,
                    "clean_audio_hint": self.request.clean_audio_hint,
                    "delogo": self.request.delogo,
                }
            ),
            model_sha256=model_fingerprints,
        )
        if self.manifest_store.exists():
            existing = self.manifest_store.load()
            compatible_shape = set(existing.stages.keys()) == set(V2_STAGE_ORDER)
            fully_delivered = (
                "deliver" in existing.stages
                and existing.stage("deliver").status is StageStatus.COMPLETED
            )
            if (
                compatible_shape
                and existing.is_cache_compatible(fingerprints)
                and (self.request.settings.enable_stage_cache or not fully_delivered)
            ):
                current_request = dict(existing.metadata.get("request", {}))
                requested_output = str(Path(self.request.output_path).resolve())
                requested_copy = (
                    str(Path(self.request.delivery_copy_path).resolve())
                    if self.request.delivery_copy_path
                    else None
                )
                if (
                    current_request.get("output_path") != requested_output
                    or current_request.get("delivery_copy_path") != requested_copy
                ):
                    existing.invalidate_from("deliver", V2_STAGE_ORDER)
                    current_request["output_path"] = requested_output
                    current_request["delivery_copy_path"] = requested_copy
                    existing.metadata["request"] = current_request
                    self.manifest_store.save(existing)
                return existing
            archive = self.manifest_store.path.with_name(
                "job_manifest.{}.json".format(uuid.uuid4().hex)
            )
            atomic_replace_file(self.manifest_store.path, archive)
        return self.manifest_store.create(
            job_id=self.job_directory.name,
            fingerprints=fingerprints,
            stage_names=V2_STAGE_ORDER,
            metadata={
                "mode": "v2",
                "pipeline_implementation_version": PIPELINE_IMPLEMENTATION_VERSION,
                "source_path": str(self.video_path),
                "source_size_bytes": source_size,
                "legacy_default_unchanged": True,
                "request": {
                    "output_path": str(Path(self.request.output_path).resolve()),
                    "delivery_copy_path": (
                        str(Path(self.request.delivery_copy_path).resolve())
                        if self.request.delivery_copy_path
                        else None
                    ),
                    "target_lang": self.request.target_lang,
                    "voice_source": self.request.voice_source,
                    "voice_param": self.request.voice_param,
                    "rvc_model_path": (
                        str(Path(self.request.rvc_model_path).resolve())
                        if self.request.rvc_model_path
                        else None
                    ),
                    "clean_audio_hint": self.request.clean_audio_hint,
                    "delogo": self.request.delogo,
                },
            },
        )

    async def _execute(
        self,
        name: str,
        callback: Callable[[], Any],
        allow_empty: bool = False,
    ) -> List[ArtifactRecord]:
        assert self.manifest is not None
        record = self.manifest.stage(name)
        if record.status is StageStatus.SKIPPED:
            return []
        if self._completed_valid(name, allow_empty=allow_empty):
            await self._notify(name, "cache_hit")
            return [self.manifest.artifacts[key] for key in record.artifact_keys]
        if record.status is StageStatus.COMPLETED:
            self.manifest.invalidate_from(name, V2_STAGE_ORDER)
            self.manifest_store.save(self.manifest)
        self._check_stopped()
        self.manifest.start_stage(name)
        self.manifest_store.save(self.manifest)
        await self._notify(name, "running")
        try:
            result = callback()
            if inspect.isawaitable(result):
                result = await result
            artifacts = list(result or [])
            if not allow_empty and not artifacts:
                raise RuntimeError("Stage {!r} produced no artifacts".format(name))
            self.manifest.complete_stage(name, artifacts)
            self.manifest_store.save(self.manifest)
            await self._notify(name, "completed")
            return artifacts
        except BaseException as exc:
            self.manifest.fail_stage(name, str(exc), type(exc).__name__)
            self.manifest_store.save(self.manifest)
            await self._notify(name, "failed")
            raise

    async def _execute_parallel_context(
        self, transcript: Sequence[RuntimeSegment]
    ) -> None:
        assert self.manifest is not None
        names = ("ocr", "translate")
        # A completed stage whose artifacts failed validation must be reset
        # together with all downstream consumers before it can transition back
        # to running.  The serial executor already does this; keep the parallel
        # fast path identical so two corrupt checkpoints remain resumable.
        for name in names:
            record = self.manifest.stage(name)
            if record.status is StageStatus.COMPLETED:
                self.manifest.invalidate_from(name, V2_STAGE_ORDER)
        for name in names:
            self.manifest.start_stage(name)
        self.manifest_store.save(self.manifest)
        await asyncio.gather(*(self._notify(name, "running") for name in names))
        results = await asyncio.gather(
            self._ocr_stage(transcript),
            self._translate_stage(transcript),
            return_exceptions=True,
        )
        first_error = None
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                self.manifest.fail_stage(name, str(result), type(result).__name__)
                first_error = first_error or result
                await self._notify(name, "failed")
            else:
                artifacts = list(result or [])
                if not artifacts:
                    error = RuntimeError(
                        "Stage {!r} produced no artifacts".format(name)
                    )
                    self.manifest.fail_stage(name, str(error), type(error).__name__)
                    first_error = first_error or error
                    await self._notify(name, "failed")
                else:
                    self.manifest.complete_stage(name, artifacts)
                    await self._notify(name, "completed")
        self.manifest_store.save(self.manifest)
        if first_error is not None:
            raise first_error

    def _completed_valid(self, name: str, allow_empty: bool = False) -> bool:
        assert self.manifest is not None
        record = self.manifest.stage(name)
        if record.status is not StageStatus.COMPLETED:
            return False
        if name == "deliver":
            outputs = record.metadata.get("published_outputs") or [
                record.metadata.get("published_output", {})
            ]
            if not outputs or not all(self._published_output_valid(item) for item in outputs):
                return False
        if not record.artifact_keys:
            return allow_empty
        return all(
            key in self.manifest.artifacts
            and self.artifact_store.validate(self.manifest.artifacts[key]).valid
            for key in record.artifact_keys
        )

    @staticmethod
    def _published_output_valid(payload: Mapping[str, Any]) -> bool:
        try:
            path = Path(str(payload["path"]))
            expected_size = int(payload["size_bytes"])
            expected_sha256 = str(payload["sha256"])
        except (KeyError, TypeError, ValueError):
            return False
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        actual_sha256, _ = hash_file(path)
        return actual_sha256 == expected_sha256

    def _skip(self, name: str, reason: str) -> None:
        assert self.manifest is not None
        record = self.manifest.stage(name)
        if record.status is StageStatus.SKIPPED:
            return
        if record.status is StageStatus.COMPLETED:
            return
        if record.status in {StageStatus.FAILED, StageStatus.RUNNING}:
            record.reset()
        self.manifest.skip_stage(name, reason)
        self.manifest_store.save(self.manifest)

    async def _notify(self, stage: str, state: str) -> None:
        callback = self.request.progress
        if callback is None:
            return
        try:
            result = callback(stage, state)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # Status delivery is observability, not media correctness. A
            # transient Telegram/UI failure must not corrupt stage state.
            if self.manifest is not None:
                warnings = self.manifest.metadata.setdefault("progress_warnings", [])
                self.manifest.metadata["progress_warning_count"] = (
                    int(self.manifest.metadata.get("progress_warning_count", 0)) + 1
                )
                warnings.append(
                    {
                        "stage": stage,
                        "state": state,
                        "error": "{}: {}".format(type(exc).__name__, exc),
                    }
                )
                del warnings[:-50]

    @staticmethod
    def _check_stopped() -> None:
        try:
            _prepare_legacy_imports()
            import shared_state

            if getattr(shared_state, "stop_requested", False):
                raise RuntimeError("Bị hủy bởi lệnh /stop")
        except ImportError:
            return

    def _artifact_path(self, key: str) -> Path:
        return self.artifact_store.path_for(key)

    def _load_json(self, key: str) -> Any:
        return json.loads(self._artifact_path(key).read_text(encoding="utf-8"))

    def _load_segments(self, key: str) -> List[RuntimeSegment]:
        payload = self._load_json(key)
        return segments_from_dicts(payload.get("segments", payload))

    def _input_stage(self) -> Sequence[ArtifactRecord]:
        assert self.manifest is not None
        try:
            width, height = probe_video_dimensions(self.video_path)
            duration = probe_video_duration(self.video_path)
            self.manifest.metadata["source_dimensions"] = [width, height]
            self.manifest.metadata["source_duration_seconds"] = duration
            self.manifest.metadata["preserve_source_resolution"] = (
                self.request.settings.preserve_source_resolution
            )
        except Exception as exc:
            self.manifest.metadata["source_probe_warning"] = str(exc)
        return []

    def _resource_scaled_timeout(self) -> float:
        """Scale stage timeout with media duration instead of a fixed minute limit."""

        assert self.manifest is not None
        try:
            duration = float(self.manifest.metadata["source_duration_seconds"])
        except (KeyError, TypeError, ValueError):
            duration = 0.0
        return max(
            self.request.settings.stage_timeout_seconds,
            duration * 2.0 + 600.0,
        )

    async def _extract_audio_stage(self) -> Sequence[ArtifactRecord]:
        _prepare_legacy_imports()
        from video_utils import extract_audio_from_video

        with tempfile.TemporaryDirectory(prefix="extract-", dir=self.work_directory) as work:
            output = Path(work) / "original.wav"
            ok = await asyncio.to_thread(extract_audio_from_video, str(self.video_path), str(output))
            if not ok or not output.is_file():
                raise RuntimeError("Could not extract source audio")
            return [self.artifact_store.put_file("audio/original.wav", output)]

    async def _demucs_stage(self) -> Sequence[ArtifactRecord]:
        original = self._artifact_path("audio/original.wav")
        with tempfile.TemporaryDirectory(prefix="demucs-", dir=self.work_directory) as work:
            if self.request.settings.enable_gpu_process_isolation:
                result = await asyncio.to_thread(
                    self.gpu_executor.run,
                    "demucs",
                    {
                        "input_audio": str(original),
                        "output_directory": str(work),
                        "segment_seconds": 6,
                        "timeout_seconds": self._resource_scaled_timeout(),
                    },
                    self._resource_scaled_timeout(),
                )
                vocals = Path(result["vocals_path"])
                background = Path(result["background_path"])
            else:
                _prepare_legacy_imports()
                from video_utils import separate_vocals_demucs

                vocals_value, background_value = await asyncio.to_thread(
                    separate_vocals_demucs,
                    str(original),
                    str(work),
                    6,
                    self._resource_scaled_timeout(),
                )
                vocals, background = Path(vocals_value), Path(background_value)
            validate_demucs_outputs(original, vocals, background)
            return [
                self.artifact_store.put_file("audio/vocals.wav", vocals),
                self.artifact_store.put_file("audio/background.wav", background),
            ]

    def _speech_audio(self) -> Path:
        return (
            self._artifact_path("audio/vocals.wav")
            if self.manifest.stage("demucs").status is StageStatus.COMPLETED
            else self._artifact_path("audio/original.wav")
        )

    def _background_audio(self) -> Path:
        return (
            self._artifact_path("audio/background.wav")
            if self.manifest.stage("demucs").status is StageStatus.COMPLETED
            else self._artifact_path("audio/original.wav")
        )

    async def _transcribe_stage(self) -> Sequence[ArtifactRecord]:
        speech = self._speech_audio()
        with tempfile.TemporaryDirectory(prefix="whisper-", dir=self.work_directory) as work:
            srt_path = Path(work) / "original.srt"
            if self.request.settings.enable_gpu_process_isolation:
                result = await asyncio.to_thread(
                    self.gpu_executor.run,
                    "whisper",
                    {
                        "input_audio": str(speech),
                        "output_srt": str(srt_path),
                        "num_workers": 1,
                    },
                    self._resource_scaled_timeout(),
                )
                segment_payload = result["segments"]
            else:
                _prepare_legacy_imports()
                from ai.transcription import extract_subtitles_whisper

                segments = await asyncio.to_thread(
                    extract_subtitles_whisper, str(speech), str(srt_path), 1
                )
                segment_payload = segments_to_dicts(segments)
            if not segment_payload:
                raise RuntimeError("Whisper returned no speech segments")
            from .gender_detector import enrich_segments_with_gender
            runtime_segs = segments_from_dicts(segment_payload)
            enriched_segs = await asyncio.to_thread(
                enrich_segments_with_gender, runtime_segs, speech
            )
            segment_payload = segments_to_dicts(enriched_segs)
            return [
                self.artifact_store.put_file("transcript/original.srt", srt_path),
                self.artifact_store.put_json(
                    "transcript/segments.json", {"segments": segment_payload}
                ),
            ]

    async def _ocr_stage(
        self, transcript: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        if self.request.settings.enable_gpu_process_isolation:
            result = await asyncio.to_thread(
                self.gpu_executor.run,
                "ocr",
                {
                    "video_path": str(self.video_path),
                    "target_lang": self.request.target_lang,
                    "segments": segments_to_dicts(transcript),
                    "batch_segments": self.request.settings.ocr_batch_segments,
                },
                self._resource_scaled_timeout(),
            )
        else:
            _prepare_legacy_imports()
            from ocr_utils import perform_video_ocr, release_ocr_reader

            segments = segments_from_dicts(segments_to_dicts(transcript))
            width, height = 1080, 1920
            main_positions = []
            try:
                for batch in chunked(
                    segments, self.request.settings.ocr_batch_segments
                ):
                    _blocks, width, height, main_y_pct = await asyncio.to_thread(
                        perform_video_ocr,
                        str(self.video_path),
                        self.request.target_lang,
                        1.0,
                        None,
                        batch,
                    )
                    main_positions.append(float(main_y_pct))
            finally:
                release_ocr_reader()
            main_y_pct = (
                sorted(main_positions)[len(main_positions) // 2]
                if main_positions
                else 0.85
            )
            result = {
                "segments": segments_to_dicts(segments),
                "width": width,
                "height": height,
                "main_y_pct": main_y_pct,
            }
        return [self.artifact_store.put_json("ocr/result.json", result)]

    async def _translate_stage(
        self, transcript: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        _prepare_legacy_imports()
        from ai.translation import translate_subtitles

        batches = bounded_batches(
            segments_from_dicts(segments_to_dicts(transcript)),
            self.request.settings.translation_batch_segments,
            self.request.settings.translation_batch_characters,
            lambda segment: len(str(segment.content)),
        )
        translated_all: List[RuntimeSegment] = []
        artifacts: List[ArtifactRecord] = []
        prior_context: List[Dict[str, str]] = []
        for batch_number, batch in enumerate(batches, 1):
            checkpoint_key = "translation/batches/{:05d}.json".format(batch_number)
            input_fingerprint = fingerprint_json(
                {
                    "pipeline_implementation_version": PIPELINE_IMPLEMENTATION_VERSION,
                    "segments": segments_to_dicts(batch),
                    "target_lang": self.request.target_lang,
                    "prior_context": prior_context[-3:],
                }
            )
            checkpoint_path = self.artifact_store.path_for(checkpoint_key)
            translated_batch = None
            if checkpoint_path.is_file():
                try:
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    if checkpoint.get("input_fingerprint") == input_fingerprint:
                        candidate_batch = segments_from_dicts(checkpoint["segments"])
                        validate_translated_batch(batch, candidate_batch)
                        translated_batch = candidate_batch
                        artifacts.append(
                            self.artifact_store.record_existing(checkpoint_key)
                        )
                except (
                    OSError,
                    KeyError,
                    TypeError,
                    ValueError,
                    RuntimeError,
                    json.JSONDecodeError,
                ):
                    translated_batch = None
            if translated_batch is None:
                translated_batch = await asyncio.to_thread(
                    translate_subtitles,
                    batch,
                    self.request.target_lang,
                    self.request.api_key,
                    str(self.video_path),
                    context_start_seconds=batch[0].start.total_seconds(),
                    context_end_seconds=batch[-1].end.total_seconds(),
                    prior_context=prior_context[-3:],
                    strict=True,
                    enable_g4f=False,
                )
                validate_translated_batch(batch, translated_batch)
                artifacts.append(
                    self.artifact_store.put_json(
                        checkpoint_key,
                        {
                            "input_fingerprint": input_fingerprint,
                            "segments": segments_to_dicts(translated_batch),
                        },
                    )
                )
            translated_all.extend(translated_batch)
            prior_context.extend(
                {
                    "source": str(segment.orig_content or ""),
                    "translated": str(segment.content),
                }
                for segment in translated_batch[-3:]
            )

        artifacts.extend([
            self.artifact_store.put_json(
                "translation/segments.json", {"segments": segments_to_dicts(translated_all)}
            ),
            self.artifact_store.put_text(
                "translation/translated.srt", compose_srt(translated_all)
            ),
        ])
        return artifacts

    def _merged_translated_segments(
        self, transcript: Sequence[RuntimeSegment]
    ) -> List[RuntimeSegment]:
        translated = self._load_segments("translation/segments.json")
        if self.manifest.stage("ocr").status is StageStatus.COMPLETED:
            ocr = self._load_json("ocr/result.json")
            return merge_ocr_geometry(translated, segments_from_dicts(ocr["segments"]))
        return merge_ocr_geometry(translated, transcript)

    async def _timing_stage(
        self, segments: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        policy = TimingPolicy(
            atempo_min=self.request.settings.atempo_min,
            atempo_max=self.request.settings.atempo_max,
        )
        rewriter = (
            GeminiTimingRewriter(self.request.api_key)
            if self.request.api_key
            else None
        )
        solved = await asyncio.to_thread(
            solve_segment_timing, segments, policy, rewriter
        )
        payload = {
            "segments": segments_to_dicts(solved.segments),
            "plans": [plan.__dict__ for plan in solved.plans],
            "unresolved_source_ids": solved.unresolved_source_ids,
            "rewrite_rounds": solved.rewrite_rounds,
        }
        return [
            self.artifact_store.put_json("translation/timed_segments.json", payload),
            self.artifact_store.put_text(
                "translation/timed.srt", compose_srt(solved.segments)
            ),
        ]

    def _rvc_enabled(self) -> bool:
        return bool(
            self.request.settings.enable_rvc
            and self.request.voice_source == "rvc"
            and self.request.rvc_model_path
            and is_real_rvc_model(self.request.rvc_model_path)
        )

    async def _tts_stage(
        self, segments: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        policy = TimingPolicy(
            atempo_min=self.request.settings.atempo_min,
            atempo_max=self.request.settings.atempo_max,
        )
        source = "rvc" if self._rvc_enabled() else self.request.voice_source
        if source not in {"edge", "fpt", "rvc"}:
            source = "edge"
        artifacts: List[ArtifactRecord] = []
        portable_infos: List[Dict[str, Any]] = []
        actual_rewriter = (
            GeminiTimingRewriter(self.request.api_key)
            if self.request.settings.enable_timing_solver and self.request.api_key
            else None
        )
        for batch_number, batch in enumerate(
            chunked(list(segments), self.request.settings.tts_batch_segments), 1
        ):
            checkpoint_key = "tts/batches/{:05d}.json".format(batch_number)
            input_fingerprint = fingerprint_json(
                {
                    "pipeline_implementation_version": PIPELINE_IMPLEMENTATION_VERSION,
                    "segments": segments_to_dicts(batch),
                    "voice_source": source,
                    "voice_param": self.request.voice_param,
                    "atempo_min": policy.atempo_min,
                    "atempo_max": policy.atempo_max,
                    "actual_timing_rewrite": actual_rewriter is not None,
                }
            )
            restored = False
            checkpoint_path = self.artifact_store.path_for(checkpoint_key)
            if checkpoint_path.is_file():
                try:
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    audio_records = [
                        ArtifactRecord.from_dict(item)
                        for item in checkpoint.get("audio_artifacts", [])
                    ]
                    if (
                        checkpoint.get("input_fingerprint") == input_fingerprint
                        and len(audio_records) == len(batch)
                        and all(
                            self.artifact_store.validate(record).valid
                            for record in audio_records
                        )
                    ):
                        artifacts.extend(audio_records)
                        artifacts.append(self.artifact_store.record_existing(checkpoint_key))
                        portable_infos.extend(checkpoint["segments"])
                        restored_segments = checkpoint.get("runtime_segments", [])
                        restored_by_index = {
                            int(item["index"]): item
                            for item in restored_segments
                            if isinstance(item, dict) and "index" in item
                        }
                        for segment in batch:
                            restored_segment = restored_by_index.get(int(segment.index))
                            if restored_segment is not None:
                                segment.content = str(restored_segment.get("content", ""))
                        restored = True
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    restored = False
            if restored:
                continue

            with tempfile.TemporaryDirectory(
                prefix="tts-batch-", dir=self.work_directory
            ) as work:
                async def generate_round(round_number: int):
                    return await generate_tts_audio_v2(
                        batch,
                        Path(work) / "round-{}".format(round_number),
                        voice_source=source,
                        voice_param=self.request.voice_param,
                        api_key=self.request.tts_api_key or self.request.api_key,
                        policy=policy,
                        strict_provider=True,
                    )

                infos = await generate_round(0)
                if actual_rewriter is not None:
                    for rewrite_round in range(1, policy.max_rewrite_rounds + 1):
                        requests = plan_actual_timing_rewrites(batch, infos)
                        if not requests:
                            break
                        replacements = await asyncio.to_thread(
                            actual_rewriter, requests
                        )
                        changed = False
                        for segment in batch:
                            replacement = replacements.get(int(segment.index))
                            if (
                                replacement
                                and replacement.strip() != str(segment.content).strip()
                            ):
                                segment.content = replacement.strip()
                                changed = True
                        if not changed:
                            break
                        infos = await generate_round(rewrite_round)
                batch_records = []
                batch_infos = []
                for info in infos:
                    key = "tts/{}.mp3".format(info["index"])
                    record = self.artifact_store.put_file(key, info["path"])
                    batch_records.append(record)
                    batch_infos.append({**info, "artifact_key": key, "path": None})
                checkpoint_record = self.artifact_store.put_json(
                    checkpoint_key,
                    {
                        "input_fingerprint": input_fingerprint,
                        "audio_artifacts": [record.to_dict() for record in batch_records],
                        "segments": batch_infos,
                        "runtime_segments": segments_to_dicts(batch),
                    },
                )
                artifacts.extend(batch_records)
                artifacts.append(checkpoint_record)
                portable_infos.extend(batch_infos)
        artifacts.append(
            self.artifact_store.put_json(
                "tts/segments.json",
                {
                    "segments": portable_infos,
                    "runtime_segments": segments_to_dicts(segments),
                    "unresolved_source_ids": sorted(
                        {
                            int(info.get("source_segment_id", info["index"]))
                            for info in portable_infos
                            if not bool(info.get("timing_fits", False))
                        }
                    ),
                },
            )
        )
        return artifacts

    def _segments_after_tts(
        self, fallback: Sequence[RuntimeSegment]
    ) -> List[RuntimeSegment]:
        payload = self._load_json("tts/segments.json")
        serialized = payload.get("runtime_segments")
        if isinstance(serialized, list) and serialized:
            return segments_from_dicts(serialized)
        return segments_from_dicts(segments_to_dicts(fallback))

    def _audio_infos(self) -> List[Dict[str, Any]]:
        key = (
            "rvc/segments.json"
            if self.manifest.stage("rvc").status is StageStatus.COMPLETED
            else "tts/segments.json"
        )
        infos = self._load_json(key)["segments"]
        return [
            {**item, "path": str(self._artifact_path(item["artifact_key"]))}
            for item in infos
        ]

    async def _rvc_stage(
        self, segments: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        tts_infos = self._load_json("tts/segments.json")["segments"]
        segment_map = {segment.index: segment for segment in segments}
        policy = TimingPolicy(
            atempo_min=self.request.settings.atempo_min,
            atempo_max=self.request.settings.atempo_max,
        )
        tts_by_index = {int(info["index"]): info for info in tts_infos}
        artifacts: List[ArtifactRecord] = []
        portable: List[Dict[str, Any]] = []
        for batch_number, batch in enumerate(
            chunked(tts_infos, self.request.settings.rvc_batch_segments), 1
        ):
            checkpoint_key = "rvc/batches/{:05d}.json".format(batch_number)
            input_fingerprint = fingerprint_json(
                {
                    "pipeline_implementation_version": PIPELINE_IMPLEMENTATION_VERSION,
                    "tts": batch,
                    "model": self.manifest.fingerprints.model_sha256.get("rvc", ""),
                    "atempo_min": policy.atempo_min,
                    "atempo_max": policy.atempo_max,
                }
            )
            checkpoint_path = self.artifact_store.path_for(checkpoint_key)
            restored = False
            if checkpoint_path.is_file():
                try:
                    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                    audio_records = [
                        ArtifactRecord.from_dict(item)
                        for item in checkpoint.get("audio_artifacts", [])
                    ]
                    if (
                        checkpoint.get("input_fingerprint") == input_fingerprint
                        and len(audio_records) == len(batch)
                        and all(
                            self.artifact_store.validate(record).valid
                            for record in audio_records
                        )
                    ):
                        artifacts.extend(audio_records)
                        artifacts.append(self.artifact_store.record_existing(checkpoint_key))
                        portable.extend(checkpoint["segments"])
                        restored = True
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                    restored = False
            if restored:
                continue

            with tempfile.TemporaryDirectory(
                prefix="rvc-batch-", dir=self.work_directory
            ) as work:
                work_path = Path(work)
                items_to_rvc = []
                male_items = []
                for info in batch:
                    gender = str(info.get("gender", "female") or "female").lower()
                    inp_path = str(self._artifact_path(info["artifact_key"]))
                    if gender == "male":
                        male_items.append({
                            "index": info["index"],
                            "output_path": inp_path,
                        })
                    else:
                        items_to_rvc.append({
                            "index": info["index"],
                            "input_path": inp_path,
                            "output_path": str(
                                work_path / "{}_rvc.wav".format(info["index"])
                            ),
                        })

                rvc_items_result = []
                if items_to_rvc:
                    payload = {
                        "model_path": str(self.request.rvc_model_path),
                        "items": items_to_rvc,
                    }
                    if self.request.settings.enable_gpu_process_isolation:
                        result = await asyncio.to_thread(
                            self.gpu_executor.run,
                            "rvc",
                            payload,
                            self._resource_scaled_timeout(),
                        )
                    else:
                        from .gpu_worker import run_request

                        result = await asyncio.to_thread(
                            run_request,
                            {"schema_version": 1, "stage": "rvc", "payload": payload},
                        )
                    rvc_items_result = result.get("items", [])

                all_items_result = male_items + rvc_items_result
                batch_records = []
                batch_infos = []
                for item in all_items_result:
                    index = int(item["index"])
                    segment = segment_map[index]
                    fitted = work_path / "{}_fitted.wav".format(index)
                    fit = await asyncio.to_thread(
                        fit_audio_to_window,
                        item["output_path"],
                        fitted,
                        max((segment.end - segment.start).total_seconds(), 0.1),
                        policy,
                    )
                    key = "rvc/{}.wav".format(index)
                    record = self.artifact_store.put_file(key, fitted)
                    batch_records.append(record)
                    original_info = tts_by_index[index]
                    batch_infos.append(
                        {
                            **original_info,
                            "artifact_key": key,
                            "path": None,
                            "rvc_source_audio_duration": fit.source_duration_seconds,
                            "target_audio_duration": fit.target_duration_seconds,
                            "actual_audio_duration": fit.output_duration_seconds,
                            "rvc_applied_atempo": fit.applied_atempo,
                            "timing_fits": fit.fits,
                        }
                    )
                checkpoint_record = self.artifact_store.put_json(
                    checkpoint_key,
                    {
                        "input_fingerprint": input_fingerprint,
                        "audio_artifacts": [record.to_dict() for record in batch_records],
                        "segments": batch_infos,
                    },
                )
                artifacts.extend(batch_records)
                artifacts.append(checkpoint_record)
                portable.extend(batch_infos)
        artifacts.append(
            self.artifact_store.put_json(
                "rvc/segments.json",
                {
                    "segments": portable,
                    "unresolved_source_ids": sorted(
                        {
                            int(info.get("source_segment_id", info["index"]))
                            for info in portable
                            if not bool(info.get("timing_fits", False))
                        }
                    ),
                },
            )
        )
        return artifacts

    async def _subtitles_stage(
        self, segments: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        _prepare_legacy_imports()
        from ass_utils import generate_ass_file

        width, height, main_y = 1080, 1920, 0.88
        if self.manifest.stage("ocr").status is StageStatus.COMPLETED:
            ocr = self._load_json("ocr/result.json")
            width = int(ocr.get("width", width))
            height = int(ocr.get("height", height))
            main_y = float(ocr.get("main_y_pct", main_y))
        with tempfile.TemporaryDirectory(prefix="subs-", dir=self.work_directory) as work:
            output = Path(work) / "final.ass"
            await asyncio.to_thread(
                generate_ass_file,
                segments,
                [],
                str(output),
                width,
                height,
                main_y,
            )
            return [self.artifact_store.put_file("subtitles/final.ass", output)]

    async def _mix_legacy_stage(self) -> Sequence[ArtifactRecord]:
        _prepare_legacy_imports()
        from video_utils import mix_audio_pydub

        with tempfile.TemporaryDirectory(prefix="mix-legacy-", dir=self.work_directory) as work:
            output = Path(work) / "mixed_legacy.wav"
            await asyncio.to_thread(
                mix_audio_pydub,
                str(self._background_audio()),
                self._audio_infos(),
                str(output),
                -2,
                1,
                True,
            )
            return [self.artifact_store.put_file("audio/mixed_legacy.wav", output)]

    async def _mix_v2_stage(self) -> Sequence[ArtifactRecord]:
        with tempfile.TemporaryDirectory(prefix="mix-v2-", dir=self.work_directory) as work:
            output = Path(work) / "mixed_v2.wav"
            await asyncio.to_thread(
                mix_audio_ffmpeg,
                self._background_audio(),
                self._audio_infos(),
                output,
                FFmpegMixSettings(
                    target_lufs=self.request.settings.target_lufs,
                    true_peak_dbtp=self.request.settings.true_peak_max_dbtp,
                    voice_chunk_seconds=self.request.settings.mixer_chunk_seconds,
                    max_inputs_per_pass=(
                        self.request.settings.mixer_max_inputs_per_pass
                    ),
                ),
                timeout_seconds=self.request.settings.stage_timeout_seconds,
            )
            return [self.artifact_store.put_file("audio/mixed_v2.wav", output)]

    def _selected_mix(self) -> Path:
        if self.manifest.stage("mix_v2").status is StageStatus.COMPLETED:
            return self._artifact_path("audio/mixed_v2.wav")
        return self._artifact_path("audio/mixed_legacy.wav")

    async def _render_stage(self) -> Sequence[ArtifactRecord]:
        _prepare_legacy_imports()
        from video_utils import process_video

        with tempfile.TemporaryDirectory(prefix="render-", dir=self.work_directory) as work:
            output = Path(work) / "final.mp4"
            ok = await asyncio.to_thread(
                process_video,
                str(self.video_path),
                str(self._artifact_path("subtitles/final.ass")),
                str(self._selected_mix()),
                str(output),
                "Arial",
                "&H00FFFFFF",
                1,
                0.88,
                self.request.delogo,
                self._resource_scaled_timeout(),
            )
            if not ok or not output.is_file():
                raise RuntimeError("Final video render failed")
            return [self.artifact_store.put_file("output/final.mp4", output)]

    async def _qc_stage(
        self, segments: Sequence[RuntimeSegment]
    ) -> Sequence[ArtifactRecord]:
        audio_by_index = {
            int(info["index"]): info for info in self._audio_infos()
        }
        qc_segments = []
        for segment in segments:
            payload = segment_to_dict(segment)
            audio_info = audio_by_index.get(int(segment.index), {})
            if audio_info:
                payload.update(
                    {
                        "audio_path": str(
                            self._artifact_path(audio_info["artifact_key"])
                        ),
                        "actual_audio_duration": audio_info.get(
                            "actual_audio_duration"
                        ),
                        "target_audio_duration": max(
                            (segment.end - segment.start).total_seconds(), 0.1
                        ),
                        "timing_fits": bool(
                            audio_info.get("timing_fits", False)
                        ),
                        "applied_atempo": audio_info.get(
                            "rvc_applied_atempo",
                            audio_info.get("applied_atempo"),
                        ),
                    }
                )
            qc_segments.append(payload)
        segment_artifact = self.artifact_store.put_json(
            "qc/segments.json", {"segments": qc_segments}
        )
        with tempfile.TemporaryDirectory(prefix="qc-", dir=self.work_directory) as work:
            work_path = Path(work)
            report_path = work_path / "qc_report.json"
            diagnostics = work_path / "diagnostics"
            await asyncio.to_thread(
                run_report_only_qc,
                self._artifact_path("output/final.mp4"),
                report_path,
                self._artifact_path("output/final.mp4"),
                self._artifact_path("qc/segments.json"),
                self._artifact_path("subtitles/final.ass"),
                diagnostics,
                QCSettings(
                    target_lufs_min=self.request.settings.target_lufs - 1.0,
                    target_lufs_max=self.request.settings.target_lufs + 1.0,
                    true_peak_max_dbtp=self.request.settings.true_peak_max_dbtp,
                ),
            )
            artifacts = [
                segment_artifact,
                self.artifact_store.put_file("qc/qc_report.json", report_path),
            ]
            for frame in sorted((diagnostics / "frames").glob("*.png")):
                artifacts.append(
                    self.artifact_store.put_file("qc/frames/{}".format(frame.name), frame)
                )
            return artifacts

    def _deliver_stage(self) -> Sequence[ArtifactRecord]:
        destinations = [("primary", Path(self.request.output_path))]
        if self.request.delivery_copy_path:
            copy_path = Path(self.request.delivery_copy_path)
            if copy_path.resolve() != Path(self.request.output_path).resolve():
                destinations.append(("automatic_copy", copy_path))

        published = []
        for label, destination in destinations:
            atomic_copy_file(self._artifact_path("output/final.mp4"), destination)
            sha256, size = hash_file(destination)
            published.append(
                {
                    "label": label,
                    "path": str(destination),
                    "sha256": sha256,
                    "size_bytes": size,
                }
            )
        assert self.manifest is not None
        self.manifest.stage("deliver").metadata["published_output"] = published[0]
        self.manifest.stage("deliver").metadata["published_outputs"] = published
        return []
