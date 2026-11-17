"""
cloud_config.py - Cấu hình hệ thống Cloud GPU cho Tool V1.
"""

import os
from pathlib import Path
from typing import Dict, Any

BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR.parent
WORKSPACE_DIR = Path(os.getenv("AUTODUB_WORKSPACE", str(BACKEND_DIR.parent / "workspace")))

# Application startup owns environment loading. Importing this module must not
# read another checkout's credentials or create production directories.

DEFAULT_CONFIG: Dict[str, Any] = {
    "default_gpu": os.getenv("AUTODUB_CLOUD_DEFAULT_GPU", "T4"),  # T4 hoặc L4
    "cu_budget_daily": float(os.getenv("AUTODUB_CLOUD_CU_BUDGET", "10.0")),
    "session_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_SESSION_TIMEOUT", "7200")),  # 2 giờ
    "upload_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_UPLOAD_TIMEOUT", "1800")),     # 30 phút
    "exec_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_EXEC_TIMEOUT", "7200")),         # 2 giờ
    "download_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_DOWNLOAD_TIMEOUT", "1800")), # 30 phút
    "stop_timeout_seconds": int(os.getenv("AUTODUB_CLOUD_STOP_TIMEOUT", "60")),
    "wsl_distro": os.getenv("AUTODUB_CLOUD_WSL_DISTRO", "Ubuntu"),
    "wsl_user": os.getenv("AUTODUB_CLOUD_WSL_USER", "root"),
    "results_dir": str(BASE_DIR / "results"),
    "db_path": str(WORKSPACE_DIR / "cloud_jobs.db"),
    "output_prefix": "cloud_",
}

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
