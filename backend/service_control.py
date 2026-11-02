"""Shared stop/start intent paths. No process, network or startup side effects."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

try:
    from .environment import read_environment
except ImportError:
    from environment import read_environment


def service_environment(project_root: Path, environment: Mapping[str, str] | None = None) -> dict:
    return read_environment(Path(project_root) / "backend", environment)


def service_workspace(project_root: Path, workspace: Path | None = None,
                      environment: Mapping[str, str] | None = None) -> Path:
    env = service_environment(project_root, environment)
    return Path(workspace or env.get("AUTODUB_WORKSPACE") or Path(project_root) / "workspace").resolve()


def control_directories(project_root: Path, workspace: Path | None = None,
                        environment: Mapping[str, str] | None = None) -> list[Path]:
    root = Path(project_root).resolve()
    env = service_environment(root, environment)
    ws = service_workspace(root, workspace, env)
    configured = env.get("TOOL_V1_CONTROL_DIR") or env.get("AUTODUB_CONTROL_DIR")
    candidates = [Path(configured)] if configured else []
    for location in (ws, root / "workspace"):
        candidates.extend([location / "bot_system" / "control", location / "control"])
    return list(dict.fromkeys(path.resolve() for path in candidates))


def intent_names(scope: str, include_downloader: bool = False) -> tuple[str, ...]:
    if scope == "telegram":
        return ("telegram.disabled",)
    if scope == "downloader":
        return ("downloader.disabled",)
    if scope in ("all", "cutover"):
        names = ("v1.pause", "ops.pause", "telegram.disabled", "scriptbot.disabled")
        return names + (("downloader.disabled",) if include_downloader or scope == "cutover" else ())
    return ()


def resume_services(project_root: Path, *, workspace: Path | None = None,
                    scope: str = "all", include_downloader: bool = False,
                    environment: Mapping[str, str] | None = None) -> list[str]:
    """Clear only intents covered by an explicit start request, without launching services."""
    removed = []
    for directory in control_directories(project_root, workspace, environment):
        for name in intent_names(scope, include_downloader):
            path = directory / name
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            removed.append(str(path))
    return removed


def service_paused(project_root: Path, *, workspace: Path | None = None,
                   service: str = "telegram") -> bool:
    names = ("v1.pause", f"{service}.disabled")
    return any((directory / name).exists()
               for directory in control_directories(project_root, workspace) for name in names)
