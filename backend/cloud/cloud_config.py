"""
cloud_config.py - Cấu hình hệ thống Cloud GPU cho Tool V1.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR.parent

logger = logging.getLogger(__name__)

# Tự động nạp .env nếu chưa có trong biến môi trường.
# Khi fallback sang .env của checkout khác, không ghi đè workspace/token cục bộ của checkout hiện tại.
_LOCAL_ENV_PATH = BACKEND_DIR / ".env"
_EXTERNAL_ENV_PATH = Path(r"C:\tool v1\backend\.env")
_ISOLATED_ENV_KEYS = {
    "AUTODUB_WORKSPACE",
    "AUTODUB_LOCAL_TOKEN",
    "AUTODUB_OUTPUT_DIR",
    "AUTODUB_CLOUD_OUTPUT_DIR",
    "AUTODUB_INPUT_DIR",
}


def _env_candidates() -> List[Tuple[Path, bool]]:
    candidates = [(_LOCAL_ENV_PATH, False)]
    if _EXTERNAL_ENV_PATH.exists():
        if os.getenv("AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK") == "1":
            candidates.append((_EXTERNAL_ENV_PATH, True))
        else:
            logger.warning(
                "Bỏ qua file .env bên ngoài %s; đặt AUTODUB_ALLOW_EXTERNAL_ENV_FALLBACK=1 để cho phép.",
                _EXTERNAL_ENV_PATH,
            )
    return candidates


def _load_environment() -> None:
    for _env_candidate, _is_external_fallback in _env_candidates():
        if not _env_candidate.exists():
            continue
        try:
            with open(_env_candidate, "r", encoding="utf-8") as _ef:
                for _line in _ef:
                    if "=" in _line and not _line.strip().startswith("#"):
                        _k, _v = _line.strip().split("=", 1)
                        _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                        if _is_external_fallback and _k in _ISOLATED_ENV_KEYS:
                            continue
                        if _k and _k not in os.environ:
                            os.environ[_k] = _v
            break
        except Exception:
            continue


_load_environment()

_cloud_cache_backend = os.getenv("AUTODUB_CLOUD_CACHE_BACKEND", "none").strip().lower()
if _cloud_cache_backend not in {"none", "gdrive"}:
    _cloud_cache_backend = "none"

WORKSPACE_DIR = Path(os.getenv("AUTODUB_WORKSPACE", str(BACKEND_DIR.parent / "workspace")))


def _is_local_default_banve(raw_path: str) -> bool:
    cleaned = str(raw_path or "").strip().rstrip("\\/")
    return bool(cleaned) and os.path.normcase(cleaned) == os.path.normcase(r"D:\banve")


def get_cloud_output_dir() -> Path:
    """Keep Cloud deliverables in D:\\ToolV1_Cloud by default (never sharing Local's D:\\banve)."""
    cloud_explicit = os.getenv("AUTODUB_CLOUD_OUTPUT_DIR", "").strip()
    if cloud_explicit:
        return Path(cloud_explicit).expanduser()
    out_override = os.getenv("AUTODUB_OUTPUT_DIR", "").strip()
    if out_override and not _is_local_default_banve(out_override):
        return Path(out_override).expanduser()
    return Path(r"D:\ToolV1_Cloud")

DEFAULT_CONFIG: Dict[str, Any] = {
    "default_gpu": os.getenv("AUTODUB_CLOUD_DEFAULT_GPU", "T4"),  # T4 hoặc L4
    "cu_budget_daily": float(os.getenv("AUTODUB_CLOUD_CU_BUDGET", "10.0")),
    "session_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_SESSION_TIMEOUT", "7200")),  # 2 giờ
    "upload_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_UPLOAD_TIMEOUT", "1800")),     # 30 phút
    "exec_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_EXEC_TIMEOUT", "7200")),         # 2 giờ
    "download_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_DOWNLOAD_TIMEOUT", "1800")), # 30 phút
    "stop_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_STOP_TIMEOUT", "60")),
    "setup_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_SETUP_TIMEOUT", "600")),
    "min_remote_disk_mb": int(os.getenv("AUTODUB_CLOUD_MIN_DISK_MB", "4096")),
    "max_upload_size_bytes": int(os.getenv("AUTODUB_CLOUD_MAX_UPLOAD_BYTES", str(2 * 1024 * 1024 * 1024))),
    "budget_timezone": os.getenv("AUTODUB_CLOUD_BUDGET_TIMEZONE", "Asia/Ho_Chi_Minh"),
    "stop_latency_buffer_seconds": int(os.getenv("AUTODUB_CLOUD_STOP_LATENCY_BUFFER_SECONDS", "60")),
    "billing_rounding_quantum_cu": float(os.getenv("AUTODUB_CLOUD_BILLING_ROUNDING_QUANTUM_CU", "0.02")),
    "runtime_budget_check_interval_seconds": int(os.getenv("AUTODUB_CLOUD_RUNTIME_BUDGET_CHECK_INTERVAL_SECONDS", "15")),
    "provider_balance_stale_seconds": int(os.getenv("AUTODUB_CLOUD_PROVIDER_BALANCE_STALE_SECONDS", "300")),
    "min_provider_balance_cu": float(os.getenv("AUTODUB_CLOUD_MIN_PROVIDER_BALANCE_CU", "0.0")),
    "warm_session_handoff_seconds": int(os.getenv("AUTODUB_CLOUD_WARM_HANDOFF_SECONDS", "300")),
    "warm_session_keepalive_seconds": int(os.getenv("AUTODUB_CLOUD_WARM_KEEPALIVE_SECONDS", "300")),
    "warm_session_keepalive_max_seconds": int(os.getenv("AUTODUB_CLOUD_WARM_KEEPALIVE_MAX_SECONDS", "1800")),
    "warm_session_reuse_enabled": os.getenv("AUTODUB_CLOUD_WARM_REUSE", "1").strip().lower() not in ("0", "false", "no"),
    "cloud_gemini_keys_max": int(os.getenv("AUTODUB_CLOUD_GEMINI_KEYS_MAX", "2")),
    "cloud_asr_engine": os.getenv("AUTODUB_CLOUD_ASR_ENGINE", "openai-whisper").strip().lower() or "openai-whisper",
    "cloud_cache_backend": _cloud_cache_backend,
    "cloud_cache_drive_root": os.getenv(
        "AUTODUB_CLOUD_CACHE_DRIVE_ROOT", "/content/drive/MyDrive/AutoDubCloudCache"
    ).strip()
    or "/content/drive/MyDrive/AutoDubCloudCache",
    "cloud_cache_save_enabled": os.getenv("AUTODUB_CLOUD_CACHE_SAVE", "1").strip().lower()
    not in ("0", "false", "no"),
    "cloud_cache_save_timeout_seconds": int(
        os.getenv("AUTODUB_CLOUD_CACHE_SAVE_TIMEOUT_SECONDS", "900")
    ),
    "cloud_drive_mount_timeout_seconds": int(
        os.getenv("AUTODUB_CLOUD_DRIVE_MOUNT_TIMEOUT_SECONDS", "180")
    ),
    "cloud_poll_max_interval_seconds": float(os.getenv("AUTODUB_CLOUD_POLL_MAX_INTERVAL_SECONDS", "30")),
    "cloud_upload_retry_delays_seconds": [5, 15],
    "wsl_distro": os.getenv("AUTODUB_CLOUD_WSL_DISTRO", "Ubuntu"),
    "wsl_user": os.getenv("AUTODUB_CLOUD_WSL_USER", "root"),
    "results_dir": str(BASE_DIR / "results"),
    "db_path": str(WORKSPACE_DIR / "cloud_jobs.db"),
    "output_prefix": "cloud_",
}

# Đảm bảo các thư mục cần thiết tồn tại
Path(DEFAULT_CONFIG["results_dir"]).mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def to_wsl_path(win_path: str | Path) -> str:
    """Chuyển đường dẫn Windows (C:\\... hoặc D:\\...) sang đường dẫn WSL (/mnt/c/... hoặc /mnt/d/...)."""
    p_str = str(win_path).replace("\\", "/")
    if len(p_str) >= 2 and p_str[1] == ":":
        drive = p_str[0].lower()
        return f"/mnt/{drive}" + p_str[2:]
    return p_str


def to_win_path(wsl_path: str) -> str:
    """Chuyển đường dẫn WSL (/mnt/c/... hoặc /mnt/d/...) sang đường dẫn Windows (C:\\... hoặc D:\\...)."""
    norm = wsl_path.replace("\\", "/")
    if norm.startswith("/mnt/"):
        parts = norm.split("/", 3)
        if len(parts) >= 3:
            drive = parts[2].upper()
            rest = parts[3] if len(parts) > 3 else ""
            win_rest = rest.replace("/", "\\")
            return f"{drive}:\\{win_rest}"
    return wsl_path.replace("/", "\\")


def get_wsl_env_prefix() -> str:
    """Tiền tố lệnh shell trong WSL: nạp PATH cho colab/uv và thiết lập keyring plaintext."""
    return (
        'export PATH="/root/.local/bin:$PATH" && '
        'export PYTHON_KEYRING_BACKEND="keyrings.alt.file.PlaintextKeyring" && '
    )
