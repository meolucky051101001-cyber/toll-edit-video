"""Portable, environment-driven filesystem locations for Tool V2."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ENV_REFERENCE = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
    r"|\$([A-Za-z_][A-Za-z0-9_]*)"
    r"|%([A-Za-z_][A-Za-z0-9_]*)%"
)


def _expand_path(value: str, environment: Mapping[str, str]) -> str:
    """Expand common dotenv variable forms using the supplied environment."""

    def replace(match: re.Match[str]) -> str:
        name = next(group for group in match.groups() if group is not None)
        return str(environment.get(name, match.group(0)))

    return os.path.expanduser(_ENV_REFERENCE.sub(replace, value.strip()))


def repo_root() -> Path:
    """Thư mục gốc repo, ổn định dù module được import theo tên nào."""
    return Path(__file__).resolve().parents[2]


_DEFAULT_DOWNLOADER_SAVE_DIR = Path(r"D:\vid chưa edit")


def downloader_save_dirs(
    environment: Mapping[str, str] | None = None,
) -> set[Path]:
    """Các thư mục bot Douyin/XHS ghi video tải về (chỉ tải, không edit)."""
    env = os.environ if environment is None else environment
    dirs = {_DEFAULT_DOWNLOADER_SAVE_DIR.resolve(strict=False)}
    raw = str(env.get("SAVE_DIRECTORY", "") or "").strip()
    if raw:
        dirs.add(Path(_expand_path(raw, env)).resolve(strict=False))
    return dirs


def is_downloader_save_dir(
    path: Path | str, environment: Mapping[str, str] | None = None
) -> bool:
    try:
        resolved = Path(path).resolve(strict=False)
    except (OSError, ValueError):
        return False
    return resolved in downloader_save_dirs(environment)


def _user_data_root(environment: Mapping[str, str]) -> Path:
    if os.name == "nt":
        base = environment.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        return Path(base) / "AutoDub"
    base = environment.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "autodub"


def _configured_path(
    name: str,
    environment: Mapping[str, str],
    default: Path,
    project_root: Path,
) -> Path:
    raw = str(environment.get(name, "") or "").strip()
    candidate = Path(_expand_path(raw, environment)) if raw else default
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve(strict=False)


@dataclass(frozen=True)
class AppPaths:
    """Resolved paths shared by the API, bot, batch runner and preflight."""

    project_root: Path
    data_root: Path
    workspace: Path
    input_dir: Path
    output_dir: Path
    model_cache: Path
    shared_assets_dir: Path
    shared_workspace_dir: Path | None

    @classmethod
    def from_environment(
        cls,
        project_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> AppPaths:
        env = os.environ if environment is None else environment
        root = Path(project_root or repo_root()).resolve()
        default_data_root = _user_data_root(env)
        data_root = _configured_path(
            "AUTODUB_DATA_DIR", env, default_data_root, root
        )
        video_root = Path.home() / "Videos" / "AutoDub"
        legacy_workspace = root / "workspace"
        workspace_default = (
            legacy_workspace
            if legacy_workspace.is_dir() and not env.get("AUTODUB_DATA_DIR")
            else data_root / "workspace"
        )
        legacy_model_cache = root / "models"
        model_cache_default = (
            legacy_model_cache
            if legacy_model_cache.is_dir() and not env.get("AUTODUB_DATA_DIR")
            else data_root / "models"
        )
        shared_workspace_value = str(
            env.get("AUTODUB_SHARED_WORKSPACE_DIR", "") or ""
        ).strip()

        default_input = (
            Path("D:/workspace/downloads")
            if Path("D:/workspace/downloads").is_dir()
            else (workspace_default / "downloads" if (workspace_default / "downloads").is_dir() else video_root / "input")
        )

        return cls(
            project_root=root,
            data_root=data_root,
            workspace=_configured_path(
                "AUTODUB_WORKSPACE", env, workspace_default, root
            ),
            input_dir=_configured_path(
                "AUTODUB_INPUT_DIR", env, default_input, root
            ),
            output_dir=_configured_path(
                "AUTODUB_OUTPUT_DIR", env, video_root / "output", root
            ),
            model_cache=_configured_path(
                "AUTODUB_MODEL_CACHE",
                env,
                model_cache_default,
                root,
            ),
            shared_assets_dir=_configured_path(
                "AUTODUB_SHARED_ASSETS_DIR", env, data_root / "assets", root
            ),
            shared_workspace_dir=(
                _configured_path(
                    "AUTODUB_SHARED_WORKSPACE_DIR", env, data_root / "shared-workspace", root
                )
                if shared_workspace_value
                else None
            ),
        )
