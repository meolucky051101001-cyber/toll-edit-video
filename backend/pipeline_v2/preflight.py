"""Production-readiness checks for AutoDub Pipeline v2."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from .config import PipelineSettings
from .stage_validation import is_real_rvc_model

try:
    from ai.model_policy import RuntimeModelPolicy
    from ai.model_runtime import runtime_module_available
except ImportError:
    from backend.ai.model_policy import RuntimeModelPolicy
    from backend.ai.model_runtime import runtime_module_available


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    status: str
    message: str


CORE_MODULES = (
    "torch",
    "torchaudio",
    "demucs",
    "easyocr",
    "edge_tts",
    "faster_whisper",
    "ffmpeg",
    "cv2",
    "pydub",
    "requests",
    "srt",
    "deep_translator",
    "yt_dlp",
)

INTERFACE_MODULES = {
    "api": ("fastapi", "uvicorn", "multipart"),
    "telegram": ("telegram",),
    "batch": (),
}


def _with_dotenv(environment: Mapping[str, str], backend_directory: Path) -> Dict[str, str]:
    try:
        from environment import read_environment
    except ImportError:
        from backend.environment import read_environment
    return read_environment(backend_directory, environment)


def _configured_secret(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "").strip()
    if value.upper().startswith(("YOUR_", "PASTE_")):
        return ""
    return value


def _writable_directory(path: Path) -> Optional[str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".autodub-preflight-", dir=str(path))
        os.close(descriptor)
        Path(temporary_name).unlink()
        return None
    except OSError as exc:
        return "{}: {}".format(type(exc).__name__, exc)


def _module_checks(names: Iterable[str]) -> List[PreflightCheck]:
    checks = []
    for name in sorted(set(names)):
        available = importlib.util.find_spec(name) is not None
        checks.append(
            PreflightCheck(
                "python_module:{}".format(name),
                "pass" if available else "error",
                "available" if available else "missing",
            )
        )
    return checks


def _dependency_check() -> PreflightCheck:
    """Fail readiness when the active Python environment is inconsistent."""

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return PreflightCheck("python:dependencies", "error", str(exc))

    detail = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part.strip()
    )
    invalid_distribution = "invalid distribution" in detail.lower()
    healthy = result.returncode == 0 and not invalid_distribution
    return PreflightCheck(
        "python:dependencies",
        "pass" if healthy else "error",
        "No broken requirements found" if healthy else detail[-3000:],
    )


def _command_check(name: str) -> PreflightCheck:
    path = shutil.which(name)
    return PreflightCheck(
        "binary:{}".format(name),
        "pass" if path else "error",
        path or "not found on PATH",
    )


def _nvenc_check() -> PreflightCheck:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return PreflightCheck("encoder:h264_nvenc", "skipped", "FFmpeg is unavailable")
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            ),
        )
        available = result.returncode == 0 and "h264_nvenc" in result.stdout
    except (OSError, subprocess.SubprocessError):
        available = False
    return PreflightCheck(
        "encoder:h264_nvenc",
        "pass" if available else "warning",
        "available" if available else "unavailable; renderer will use a slower fallback",
    )


def _cuda_check() -> PreflightCheck:
    if importlib.util.find_spec("torch") is None:
        return PreflightCheck("gpu:cuda", "skipped", "PyTorch is unavailable")
    try:
        import torch

        if not torch.cuda.is_available():
            return PreflightCheck(
                "gpu:cuda", "warning", "CUDA is unavailable; large models will run on CPU"
            )
        properties = torch.cuda.get_device_properties(0)
        memory_gib = properties.total_memory / (1024 ** 3)
        return PreflightCheck(
            "gpu:cuda",
            "pass" if memory_gib >= 5.5 else "warning",
            "{} ({:.1f} GiB VRAM)".format(properties.name, memory_gib),
        )
    except Exception as exc:
        return PreflightCheck("gpu:cuda", "warning", "probe failed: {}".format(exc))


def _rvc_check(project_root: Path, settings: PipelineSettings) -> PreflightCheck:
    candidates = []
    for directory in (
        project_root / "MyVoiceModel_v2",
        project_root / "models" / "rvc",
    ):
        if directory.is_dir():
            candidates.extend(sorted(directory.glob("*.pth")))
    real_models = [candidate for candidate in candidates if is_real_rvc_model(candidate)]
    if real_models:
        if importlib.util.find_spec("rvc_python") is None:
            return PreflightCheck(
                "rvc:model",
                "warning",
                "real model found but optional rvc-python package is missing",
            )
        return PreflightCheck("rvc:model", "pass", str(real_models[0]))
    status = "warning" if settings.enable_rvc else "skipped"
    return PreflightCheck(
        "rvc:model",
        status,
        "no real .pth model found; Edge TTS remains available",
    )


def _strong_model_checks(
    project_root: Path, environment: Mapping[str, str]
) -> List[PreflightCheck]:
    try:
        policy = RuntimeModelPolicy.from_env(environment, project_root=project_root)
    except ValueError as exc:
        return [PreflightCheck("models:policy", "error", str(exc))]
    checks = [
        PreflightCheck(
            "models:policy",
            "pass",
            "ASR={} separator={} OCR={}".format(
                policy.asr_backend, policy.separator_backend, policy.ocr_backend
            ),
        )
    ]
    runtime_python = policy.runtime_python_path()
    checks.append(
        PreflightCheck(
            "models:runtime",
            "pass" if runtime_python.is_file() else "warning",
            str(runtime_python)
            if runtime_python.is_file()
            else "missing; proven fallback models remain active",
        )
    )
    modules = (
        (
            "models:qwen3_asr",
            "qwen_asr",
            "{} + {}".format(policy.qwen_asr_model, policy.qwen_aligner_model),
            "Faster-Whisper {}".format(policy.whisper_model),
        ),
        (
            "models:bs_roformer",
            "audio_separator",
            policy.separator_model,
            "Demucs {}".format(policy.demucs_primary_model),
        ),
        (
            "models:pp_ocr_v6",
            "paddleocr",
            "{} via {}".format(
                policy.paddle_ocr_version, policy.paddle_ocr_engine
            ),
            "EasyOCR ch_sim",
        ),
    )
    for name, module, primary, fallback in modules:
        available = runtime_module_available(module, policy) if runtime_python.is_file() else False
        checks.append(
            PreflightCheck(
                name,
                "pass" if available else "warning",
                primary if available else "unavailable; fallback={}".format(fallback),
            )
        )
    checks.append(
        PreflightCheck(
            "models:translation",
            "pass",
            "Gemini={} OpenAI={} DeepSeek={}".format(
                policy.gemini_model, policy.openai_model, policy.deepseek_model
            ),
        )
    )
    return checks


def run_preflight(
    project_root: Path,
    interface: str = "all",
    environment: Optional[Mapping[str, str]] = None,
) -> Dict[str, object]:
    root = Path(project_root).resolve()
    backend = root / "backend"
    env = _with_dotenv(os.environ if environment is None else environment, backend)
    checks: List[PreflightCheck] = []

    checks.append(
        PreflightCheck(
            "python:version",
            "pass" if sys.version_info >= (3, 10) else "error",
            sys.version.split()[0],
        )
    )
    try:
        settings = PipelineSettings.from_env(env)
        checks.append(
            PreflightCheck(
                "pipeline:config",
                "pass" if settings.mode.value == "v2" else "warning",
                settings.mode.value
                if settings.mode.value == "v2"
                else "{}; set PIPELINE_MODE=v2 to activate Pipeline v2".format(
                    settings.mode.value
                ),
            )
        )
    except (TypeError, ValueError) as exc:
        checks.append(PreflightCheck("pipeline:config", "error", str(exc)))
        settings = PipelineSettings()

    interfaces = tuple(INTERFACE_MODULES) if interface == "all" else (interface,)
    modules = list(CORE_MODULES)
    for selected in interfaces:
        modules.extend(INTERFACE_MODULES[selected])
    checks.extend(_module_checks(modules))
    checks.append(_dependency_check())
    checks.extend((_command_check("ffmpeg"), _command_check("ffprobe")))
    checks.append(_nvenc_check())
    checks.append(_cuda_check())
    checks.append(_rvc_check(root, settings))
    checks.extend(_strong_model_checks(root, env))

    workspace = Path(env.get("AUTODUB_WORKSPACE", str(root / "workspace")))
    output = Path(env.get("AUTODUB_OUTPUT_DIR", r"D:\banve"))
    for name, directory in (("workspace", workspace), ("output", output)):
        error = _writable_directory(directory)
        checks.append(
            PreflightCheck(
                "directory:{}".format(name),
                "error" if error else "pass",
                error or str(directory.resolve()),
            )
        )

    if "api" in interfaces:
        cors_origins = [
            item.strip()
            for item in env.get(
                "AUTODUB_CORS_ORIGINS",
                "http://127.0.0.1:5173,http://localhost:5173,null",
            ).split(",")
            if item.strip()
        ]
        wildcard = "*" in cors_origins
        checks.append(
            PreflightCheck(
                "api:cors",
                "error" if wildcard else "pass",
                "wildcard origin exposes the local file-processing API"
                if wildcard
                else ",".join(cors_origins),
            )
        )

    if "telegram" in interfaces:
        token = _configured_secret(env, "BOT_TOKEN")
        checks.append(
            PreflightCheck(
                "secret:BOT_TOKEN",
                "pass" if token else "error",
                "configured" if token else "missing or placeholder",
            )
        )
    gemini = _configured_secret(env, "GEMINI_API_KEY")
    checks.append(
        PreflightCheck(
            "secret:GEMINI_API_KEY",
            "pass" if gemini else "warning",
            "configured" if gemini else "missing; Google fallback works but Gemini/timing rewrite does not",
        )
    )

    counts = {status: 0 for status in ("pass", "warning", "error", "skipped")}
    for check in checks:
        counts[check.status] += 1
    return {
        "schema_version": 1,
        "ready": counts["error"] == 0,
        "interface": interface,
        "summary": counts,
        "checks": [asdict(check) for check in checks],
    }


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check AutoDub v2 production readiness")
    parser.add_argument(
        "--project-root",
        default=str(Path(__file__).resolve().parents[2]),
    )
    parser.add_argument(
        "--interface",
        choices=("all", "api", "telegram", "batch"),
        default="all",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_argument_parser().parse_args(argv)
    report = run_preflight(Path(args.project_root), args.interface)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for check in report["checks"]:
            print("[{:<7}] {:<32} {}".format(check["status"], check["name"], check["message"]))
        summary = report["summary"]
        print(
            "ready={} pass={} warning={} error={} skipped={}".format(
                report["ready"],
                summary["pass"],
                summary["warning"],
                summary["error"],
                summary["skipped"],
            )
        )
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
