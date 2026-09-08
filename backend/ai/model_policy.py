"""Central, environment-driven model policy for Pipeline v2.

The quality profile keeps the existing strong-model defaults. The fast profile
selects the proven V1 ASR/separator with V2 processing and validation intact.
Explicit backend/model settings override either profile.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple


def _clean(value: Optional[str], default: str) -> str:
    candidate = str(value or "").strip()
    return candidate or default


def _choice(value: Optional[str], default: str, allowed: Tuple[str, ...]) -> str:
    candidate = _clean(value, default).lower()
    if candidate not in allowed:
        raise ValueError(
            "Unsupported model backend {!r}; expected one of {}".format(
                candidate, ", ".join(allowed)
            )
        )
    return candidate


def ordered_unique(*values: str) -> Tuple[str, ...]:
    result = []
    for value in values:
        candidate = str(value or "").strip()
        if candidate and candidate not in result:
            result.append(candidate)
    return tuple(result)


@dataclass(frozen=True)
class RuntimeModelPolicy:
    speed_profile: str = "quality"
    separator_backend: str = "auto"
    separator_model: str = "model_bs_roformer_ep_317_sdr_12.9755.ckpt"
    demucs_primary_model: str = "htdemucs_ft"
    demucs_fallback_model: str = "htdemucs"
    asr_backend: str = "auto"
    qwen_asr_model: str = "Qwen/Qwen3-ASR-0.6B"
    qwen_aligner_model: str = "Qwen/Qwen3-ForcedAligner-0.6B"
    qwen_language: str = "Chinese"
    whisper_model: str = "large-v3"
    ocr_backend: str = "auto"
    paddle_ocr_version: str = "PP-OCRv6"
    paddle_ocr_engine: str = "onnxruntime"
    gemini_model: str = "gemini-3.8-flash"
    openai_model: str = "gpt-5.6-sol"
    deepseek_model: str = "deepseek-v4-pro"
    model_runtime_python: str = ""
    model_cache_directory: str = ""

    @classmethod
    def from_env(
        cls,
        environment: Optional[Mapping[str, str]] = None,
        project_root: Optional[Path] = None,
    ) -> "RuntimeModelPolicy":
        env = environment if environment is not None else os.environ
        speed_profile = _choice(env.get("MODEL_SPEED_PROFILE"), "quality", ("quality", "fast"))
        fast = speed_profile == "fast"
        root = Path(project_root or Path(__file__).resolve().parents[2])
        default_runtime = root / "backend" / "model_venv" / "Scripts" / "python.exe"
        default_cache = root / "models"
        runtime_python = _clean(env.get("MODEL_RUNTIME_PYTHON"), "")
        if not runtime_python and default_runtime.is_file():
            runtime_python = str(default_runtime)
        return cls(
            speed_profile=speed_profile,
            separator_backend=_choice(
                env.get("SOURCE_SEPARATOR_BACKEND"),
                "demucs" if fast else "auto",
                ("auto", "roformer", "demucs"),
            ),
            separator_model=_clean(
                env.get("SOURCE_SEPARATOR_MODEL"),
                "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
            ),
            demucs_primary_model=_clean(
                env.get("DEMUCS_MODEL"), "htdemucs" if fast else "htdemucs_ft"
            ),
            demucs_fallback_model=_clean(
                env.get("DEMUCS_FALLBACK_MODEL"), "htdemucs_ft" if fast else "htdemucs"
            ),
            asr_backend=_choice(
                env.get("ASR_BACKEND"),
                "whisper" if fast else "auto",
                ("auto", "qwen3", "whisper"),
            ),
            qwen_asr_model=_clean(
                env.get("QWEN_ASR_MODEL"), "Qwen/Qwen3-ASR-0.6B"
            ),
            qwen_aligner_model=_clean(
                env.get("QWEN_ALIGNER_MODEL"),
                "Qwen/Qwen3-ForcedAligner-0.6B",
            ),
            qwen_language=_clean(env.get("QWEN_ASR_LANGUAGE"), "Chinese"),
            whisper_model=_clean(env.get("WHISPER_MODEL"), "large-v3"),
            ocr_backend=_choice(
                env.get("OCR_BACKEND"),
                "auto",
                ("auto", "paddle", "easyocr"),
            ),
            paddle_ocr_version=_clean(
                env.get("PADDLE_OCR_VERSION"), "PP-OCRv6"
            ),
            paddle_ocr_engine=_clean(
                env.get("PADDLE_OCR_ENGINE"), "onnxruntime"
            ),
            gemini_model=_clean(env.get("GEMINI_MODEL"), "gemini-3.8-flash"),
            openai_model=_clean(env.get("OPENAI_MODEL"), "gpt-5.6-sol"),
            deepseek_model=_clean(
                env.get("DEEPSEEK_MODEL"), "deepseek-v4-pro"
            ),
            model_runtime_python=runtime_python,
            model_cache_directory=_clean(
                env.get("AUTODUB_MODEL_CACHE"), str(default_cache)
            ),
        )

    def runtime_python_path(self) -> Path:
        return Path(self.model_runtime_python or sys.executable)

    def fingerprint_payload(self) -> dict:
        payload = asdict(self)
        # Absolute interpreter/cache paths do not change model output and would
        # make otherwise-portable stage caches machine-specific.
        payload.pop("model_runtime_python", None)
        payload.pop("model_cache_directory", None)
        return payload

    @property
    def gemini_candidates(self) -> Tuple[str, ...]:
        return ordered_unique(
            self.gemini_model,
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash",
            "gemini-flash-latest",
            "gemini-3.5-flash",
            "gemini-flash-lite-latest",
            "gemini-3.5-flash-lite",
        )

    @property
    def openai_candidates(self) -> Tuple[str, ...]:
        return ordered_unique(
            self.openai_model,
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-4o",
            "gpt-4o-mini",
        )

    @property
    def deepseek_candidates(self) -> Tuple[str, ...]:
        return ordered_unique(
            self.deepseek_model,
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-chat",
            "deepseek-reasoner",
        )


def current_model_policy(
    environment: Optional[Mapping[str, str]] = None,
) -> RuntimeModelPolicy:
    return RuntimeModelPolicy.from_env(environment)
