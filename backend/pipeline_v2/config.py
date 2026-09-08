"""Environment-driven rollout configuration for pipeline v2."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class PipelineMode(str, Enum):
    LEGACY = "legacy"
    SHADOW = "shadow"
    V2 = "v2"


class QCGatePolicy(str, Enum):
    REPORT_ONLY = "report_only"
    WARN = "warn"
    BLOCK = "block"


def _env_bool(
    environment: Mapping[str, str], name: str, default: bool
) -> bool:
    value = environment.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("{} must be a boolean value".format(name))


def _env_float(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    value = environment.get(name)
    return default if value is None else float(value)


def _env_int(environment: Mapping[str, str], name: str, default: int) -> int:
    value = environment.get(name)
    return default if value is None else int(value)


@dataclass(frozen=True)
class PipelineSettings:
    mode: PipelineMode = PipelineMode.V2
    enable_stage_cache: bool = False
    enable_parallel_ocr_gemini: bool = False
    enable_adaptive_demucs: bool = False
    enable_adaptive_ocr: bool = False
    preserve_source_resolution: bool = True
    enable_gpu_process_isolation: bool = True
    enable_timing_solver: bool = True
    enable_ffmpeg_mix_v2: bool = False
    enable_legacy_mix_ab: bool = False
    enable_rvc: bool = True
    atempo_min: float = 0.92
    atempo_max: float = 1.40
    target_lufs: float = -15.0
    true_peak_max_dbtp: float = -1.0
    qc_gate_policy: QCGatePolicy = QCGatePolicy.REPORT_ONLY
    gpu_lock_timeout_seconds: float = 1800.0
    stage_timeout_seconds: float = 3600.0
    translation_batch_segments: int = 80
    translation_batch_characters: int = 12000
    ocr_batch_segments: int = 80
    tts_batch_segments: int = 64
    rvc_batch_segments: int = 64
    mixer_chunk_seconds: float = 300.0
    mixer_max_inputs_per_pass: int = 64

    def __post_init__(self) -> None:
        if not 0.5 <= self.atempo_min <= 1.0:
            raise ValueError("atempo_min must be between 0.5 and 1.0")
        if not 1.0 <= self.atempo_max <= 2.0:
            raise ValueError("atempo_max must be between 1.0 and 2.0")
        if self.atempo_min > self.atempo_max:
            raise ValueError("atempo_min must not exceed atempo_max")
        if self.gpu_lock_timeout_seconds <= 0 or self.stage_timeout_seconds <= 0:
            raise ValueError("Pipeline timeouts must be positive")
        integer_limits = (
            self.translation_batch_segments,
            self.translation_batch_characters,
            self.ocr_batch_segments,
            self.tts_batch_segments,
            self.rvc_batch_segments,
            self.mixer_max_inputs_per_pass,
        )
        if any(value <= 0 for value in integer_limits):
            raise ValueError("Pipeline batch and input limits must be positive")
        if self.mixer_max_inputs_per_pass < 2:
            raise ValueError("mixer_max_inputs_per_pass must be at least 2")
        if self.mixer_chunk_seconds <= 0:
            raise ValueError("mixer_chunk_seconds must be positive")

    @classmethod
    def from_env(
        cls, environment: Optional[Mapping[str, str]] = None
    ) -> "PipelineSettings":
        env = environment if environment is not None else os.environ
        mode_value = env.get("PIPELINE_MODE", PipelineMode.V2.value).strip().lower()
        qc_value = env.get(
            "QC_GATE_POLICY", QCGatePolicy.REPORT_ONLY.value
        ).strip().lower()
        try:
            mode = PipelineMode(mode_value)
        except ValueError as exc:
            raise ValueError("PIPELINE_MODE must be legacy, shadow or v2") from exc
        try:
            qc_policy = QCGatePolicy(qc_value)
        except ValueError as exc:
            raise ValueError(
                "QC_GATE_POLICY must be report_only, warn or block"
            ) from exc
        return cls(
            mode=mode,
            enable_stage_cache=_env_bool(env, "ENABLE_STAGE_CACHE", False),
            enable_parallel_ocr_gemini=_env_bool(
                env, "ENABLE_PARALLEL_OCR_GEMINI", False
            ),
            enable_adaptive_demucs=_env_bool(env, "ENABLE_ADAPTIVE_DEMUCS", False),
            enable_adaptive_ocr=_env_bool(env, "ENABLE_ADAPTIVE_OCR", False),
            preserve_source_resolution=_env_bool(
                env, "PRESERVE_SOURCE_RESOLUTION", True
            ),
            enable_gpu_process_isolation=_env_bool(
                env, "ENABLE_GPU_PROCESS_ISOLATION", True
            ),
            enable_timing_solver=_env_bool(env, "ENABLE_TIMING_SOLVER", True),
            enable_ffmpeg_mix_v2=_env_bool(env, "ENABLE_FFMPEG_MIX_V2", False),
            enable_legacy_mix_ab=_env_bool(env, "ENABLE_LEGACY_MIX_AB", False),
            enable_rvc=_env_bool(env, "ENABLE_RVC", True),
            atempo_min=_env_float(env, "ATEMPO_MIN", 0.92),
            atempo_max=_env_float(env, "ATEMPO_MAX", 1.40),
            target_lufs=_env_float(env, "TARGET_LUFS", -15.0),
            true_peak_max_dbtp=_env_float(env, "TRUE_PEAK_MAX_DBTP", -1.0),
            qc_gate_policy=qc_policy,
            gpu_lock_timeout_seconds=_env_float(
                env, "GPU_LOCK_TIMEOUT_SECONDS", 1800.0
            ),
            stage_timeout_seconds=_env_float(env, "STAGE_TIMEOUT_SECONDS", 3600.0),
            translation_batch_segments=_env_int(
                env, "TRANSLATION_BATCH_SEGMENTS", 80
            ),
            translation_batch_characters=_env_int(
                env, "TRANSLATION_BATCH_CHARACTERS", 12000
            ),
            ocr_batch_segments=_env_int(env, "OCR_BATCH_SEGMENTS", 80),
            tts_batch_segments=_env_int(env, "TTS_BATCH_SEGMENTS", 64),
            rvc_batch_segments=_env_int(env, "RVC_BATCH_SEGMENTS", 64),
            mixer_chunk_seconds=_env_float(env, "MIXER_CHUNK_SECONDS", 300.0),
            mixer_max_inputs_per_pass=_env_int(
                env, "MIXER_MAX_INPUTS_PER_PASS", 64
            ),
        )

    def cache_payload(self) -> Dict[str, Any]:
        """Return non-secret settings that affect generated artifacts."""

        payload = asdict(self)
        payload["mode"] = self.mode.value
        payload["qc_gate_policy"] = self.qc_gate_policy.value
        payload.pop("gpu_lock_timeout_seconds", None)
        payload.pop("stage_timeout_seconds", None)
        return payload
