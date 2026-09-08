"""Small, environment-driven model policy for the legacy Tool V1 pipeline.

Tool V1 intentionally favors short startup time and low VRAM use.  The policy
is separate from Pipeline V2 so changing a V1 model can never change V2.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple


def _clean(value: Optional[str], default: str) -> str:
    candidate = str(value or "").strip()
    return candidate or default


def _choice(value: Optional[str], default: str, allowed: Tuple[str, ...]) -> str:
    candidate = _clean(value, default).lower()
    if candidate not in allowed:
        raise ValueError(
            "Unsupported V1 backend {!r}; expected one of {}".format(
                candidate, ", ".join(allowed)
            )
        )
    return candidate


def _ordered_unique(*values: str) -> Tuple[str, ...]:
    result = []
    for value in values:
        candidate = str(value or "").strip()
        if candidate and candidate not in result:
            result.append(candidate)
    return tuple(result)


@dataclass(frozen=True)
class V1ModelPolicy:
    whisper_model: str = "large-v3-turbo"
    whisper_fallback_model: str = "large-v3"
    demucs_model: str = "htdemucs"
    ocr_backend: str = "auto"
    paddle_detection_model: str = "PP-OCRv6_tiny_det"
    paddle_recognition_model: str = "PP-OCRv6_tiny_rec"
    paddle_engine: str = "onnxruntime"
    runtime_python: str = ""
    model_cache_directory: str = ""

    @classmethod
    def from_env(
        cls,
        environment: Optional[Mapping[str, str]] = None,
        project_root: Optional[Path] = None,
    ) -> "V1ModelPolicy":
        env = environment if environment is not None else os.environ
        root = Path(project_root or Path(__file__).resolve().parents[2])
        default_runtime = root / "backend" / "model_venv" / "Scripts" / "python.exe"
        configured_runtime = _clean(env.get("V1_MODEL_RUNTIME_PYTHON"), "")
        if not configured_runtime and default_runtime.is_file():
            configured_runtime = str(default_runtime)
        return cls(
            whisper_model=_clean(
                env.get("V1_WHISPER_MODEL"), "large-v3-turbo"
            ),
            whisper_fallback_model=_clean(
                env.get("V1_WHISPER_FALLBACK_MODEL"), "large-v3"
            ),
            demucs_model=_clean(env.get("V1_DEMUCS_MODEL"), "htdemucs"),
            ocr_backend=_choice(
                env.get("V1_OCR_BACKEND"),
                "auto",
                ("auto", "paddle", "easyocr"),
            ),
            paddle_detection_model=_clean(
                env.get("V1_PADDLE_DETECTION_MODEL"),
                "PP-OCRv6_tiny_det",
            ),
            paddle_recognition_model=_clean(
                env.get("V1_PADDLE_RECOGNITION_MODEL"),
                "PP-OCRv6_tiny_rec",
            ),
            paddle_engine=_clean(
                env.get("V1_PADDLE_ENGINE"), "onnxruntime"
            ),
            runtime_python=configured_runtime,
            model_cache_directory=_clean(
                env.get("V1_MODEL_CACHE"), str(root / "models" / "v1")
            ),
        )

    @property
    def whisper_candidates(self) -> Tuple[str, ...]:
        return _ordered_unique(
            self.whisper_model,
            self.whisper_fallback_model,
            "large-v3-turbo",
            "large-v3",
        )

    def runtime_python_path(self) -> Path:
        return Path(self.runtime_python or sys.executable)


def current_v1_model_policy(
    environment: Optional[Mapping[str, str]] = None,
) -> V1ModelPolicy:
    return V1ModelPolicy.from_env(environment)
