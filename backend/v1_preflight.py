"""Fast production-readiness checks for Tool V1 only."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


if isinstance(sys.stdout, io.TextIOWrapper):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


CORE_MODULES = (
    "torch", "torchaudio", "demucs", "easyocr", "edge_tts",
    "faster_whisper", "ffmpeg", "cv2", "pydub", "requests", "srt",
    "deep_translator", "yt_dlp",
)


def _load_env(backend: Path) -> dict:
    values = dict(os.environ)
    dotenv = backend / ".env"
    if dotenv.is_file():
        for raw_line in dotenv.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    return values


def _check(name: str, status: str, message: str) -> dict:
    return {"name": name, "status": status, "message": message}


def _directory_check(name: str, path: Path, read_only: bool = False) -> dict:
    try:
        if read_only:
            if not path.is_dir() or not os.access(path, os.R_OK | os.W_OK):
                return _check(name, "error", "directory missing or permissions unavailable")
            return _check(name, "pass", str(path.resolve()) + " (permission check only; no write probe)")
        path.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".v1-preflight-", dir=str(path))
        os.close(descriptor)
        Path(temporary).unlink()
        return _check(name, "pass", str(path.resolve()))
    except OSError as exc:
        return _check(name, "error", "{}: {}".format(type(exc).__name__, exc))


def _optional_ocr_check(root: Path, env: dict, timeout_seconds=None) -> dict:
    runtime = env.get("V1_MODEL_RUNTIME_PYTHON", "").strip()
    if not runtime:
        runtime = str(root / "backend" / "model_venv" / "Scripts" / "python.exe")
    python = Path(runtime)
    if not python.is_file():
        return _check("ocr:PP-OCRv6-tiny", "warning", "runtime chưa cài; V1 sẽ tự dùng EasyOCR")
    if timeout_seconds is not None and timeout_seconds <= 0:
        return _check("ocr:PP-OCRv6-tiny", "error", "deadline exhausted; probe incomplete")
    try:
        result = subprocess.run(
            [str(python), "-B", "-c", "import paddleocr; print(paddleocr.__version__)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30 if timeout_seconds is None else min(30, timeout_seconds),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
        )
        if result.returncode == 0:
            return _check("ocr:PP-OCRv6-tiny", "pass", result.stdout.strip() or "available")
    except subprocess.TimeoutExpired:
        if timeout_seconds is not None:
            return _check("ocr:PP-OCRv6-tiny", "error", "deadline exhausted; probe incomplete")
    except (OSError, subprocess.SubprocessError):
        pass
    return _check("ocr:PP-OCRv6-tiny", "warning", "runtime lỗi; V1 sẽ tự dùng EasyOCR")


def _cuda_check() -> dict:
    try:
        import torch

        available = torch.cuda.is_available()
        message = torch.cuda.get_device_name(0) if available else "CPU fallback"
        return _check("gpu:cuda", "pass" if available else "warning", message)
    except Exception as exc:
        return _check("gpu:cuda", "warning", "probe failed: {}".format(exc))


def _tts_check(root: Path) -> dict:
    capcut_ready = importlib.util.find_spec("capcut_tts_api") is not None
    vieneu_ready = importlib.util.find_spec("vieneu") is not None
    msg = f"CapCut TTS: {'sẵn sàng' if capcut_ready else 'chưa cài'}; Vie-TTS: {'sẵn sàng' if vieneu_ready else 'fallback CapCut/Edge'}"
    return _check("voice:tts", "pass", msg)


def run_preflight(project_root: Path, interface: str, read_only: bool = False, timeout_seconds=None) -> dict:
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
    root = project_root.resolve()
    env = _load_env(root / "backend")
    pipeline_mode = env.get("PIPELINE_MODE", "legacy").lower()
    checks = [
        _check("python:version", "pass" if sys.version_info >= (3, 10) else "error", sys.version.split()[0]),
        _check("pipeline:mode", "pass" if pipeline_mode == "legacy" else "error", pipeline_mode),
    ]
    modules = list(CORE_MODULES)
    if interface in {"telegram", "all"}:
        modules.append("telegram")
    for module in sorted(set(modules)):
        available = importlib.util.find_spec(module) is not None
        checks.append(_check("python_module:{}".format(module), "pass" if available else "error", "available" if available else "missing"))
    for binary in ("ffmpeg", "ffprobe"):
        path = shutil.which(binary)
        checks.append(_check("binary:{}".format(binary), "pass" if path else "error", path or "not found on PATH"))

    if interface in {"telegram", "all"}:
        token = env.get("BOT_TOKEN", "").strip()
        checks.append(_check("secret:BOT_TOKEN", "pass" if token else "error", "configured" if token else "missing"))
    gemini = env.get("GEMINI_API_KEY", "").strip()
    checks.append(_check("secret:GEMINI_API_KEY", "pass" if gemini else "warning", "configured" if gemini else "missing; free translation fallback remains"))
    if read_only:
        # Independent import probes dominate cold startup; overlap CUDA with
        # the isolated OCR process while retaining the original report order.
        with ThreadPoolExecutor(max_workers=2) as executor:
            cuda = executor.submit(_cuda_check)
            ocr = executor.submit(_optional_ocr_check, root, env,
                                  timeout_seconds=None if deadline is None else deadline - time.monotonic())
            checks.extend([cuda.result(), ocr.result()])
    else:
        checks.append(_cuda_check())
        checks.append(_optional_ocr_check(root, env, timeout_seconds=None if deadline is None else deadline - time.monotonic()))
    checks.append(_tts_check(root))
    checks.append(_directory_check("directory:workspace", Path(env.get("AUTODUB_WORKSPACE", str(root / "workspace"))), read_only=read_only))
    checks.append(_directory_check("directory:output", Path(env.get("AUTODUB_OUTPUT_DIR", r"D:\banve")), read_only=read_only))
    return {
        "ready": not any(item["status"] == "error" for item in checks),
        "tool": "v1",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--interface", choices=("batch", "telegram", "all"), default="all")
    parser.add_argument("--read-only", action="store_true", help="Check existing directory permissions without creating files")
    parser.add_argument("--timeout-seconds", type=float, help="Budget for import probes; a timeout is incomplete")
    args = parser.parse_args()
    if args.timeout_seconds is not None and (args.timeout_seconds < 0 or not args.timeout_seconds < float("inf")):
        parser.error("--timeout-seconds must be finite and nonnegative")
    result = run_preflight(Path(args.project_root), args.interface, read_only=args.read_only, timeout_seconds=args.timeout_seconds)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
