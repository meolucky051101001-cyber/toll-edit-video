from __future__ import annotations

import os
from pathlib import Path


def candidate_token_paths(workspace_root: Path | None = None) -> list[Path]:
    workspace = Path(
        workspace_root
        or os.getenv("AUTODUB_WORKSPACE")
        or Path(__file__).resolve().parent.parent / "workspace"
    )
    repository_root = Path(__file__).resolve().parent.parent
    return [
        workspace / "bot_system" / ".dashboard_control_token",
        workspace / ".dashboard_control_token",
        repository_root / "workspace_backup_safety" / ".dashboard_control_token",
        Path(__file__).resolve().parent / ".dashboard_control_token",
    ]


def read_control_token(
    workspace_root: Path | None = None, *, min_length: int = 16
) -> str:
    env_token = os.getenv("AUTODUB_LOCAL_TOKEN", "").strip()
    if env_token:
        return env_token

    for token_path in candidate_token_paths(workspace_root):
        try:
            if token_path.is_file():
                token = token_path.read_text(encoding="utf-8").strip()
                if len(token) >= min_length:
                    return token
        except OSError:
            continue
    return ""
