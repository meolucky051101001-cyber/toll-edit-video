from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Callable


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


def load_or_create_control_token(
    workspace_root: Path | None = None,
    *,
    on_write_error: Callable[[Path, Exception], None] | None = None,
) -> str:
    token = read_control_token(workspace_root)
    if token:
        return token

    token = secrets.token_urlsafe(32)
    for token_path in candidate_token_paths(workspace_root)[:2]:
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(token, encoding="utf-8")
            return token
        except Exception as exc:
            if on_write_error:
                on_write_error(token_path, exc)
    return token
