"""Safely stop Tool V1 Telegram bot without touching Tool V2 or other python processes."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

try:
    from backend.v1_shutdown import EXIT_OK, ShutdownResult, execute_shutdown, main as _shutdown_main
except ImportError:
    from v1_shutdown import EXIT_OK, ShutdownResult, execute_shutdown, main as _shutdown_main


def stop_v1_bot(
    *,
    project_root: Path | None = None,
    workspace: Path | None = None,
    preview: bool = False,
    force: bool = False,
    mode: str = "stop",
    **kwargs,
) -> ShutdownResult:
    """Stop only the verified Tool V1 Telegram bot and its dedicated supervisor."""
    root = (Path(project_root) if project_root else Path(__file__).resolve().parents[1]).resolve()
    result = execute_shutdown(
        project_root=root,
        workspace=workspace,
        scope="telegram",
        mode=mode,
        preview=preview,
        force=force,
        include_downloader=False,
        **kwargs,
    )
    if result.preview:
        print(result.message)
    elif result.exit_code == EXIT_OK and result.terminated_pids:
        print("[Tool V1] Telegram bot has been stopped successfully.")
    elif result.exit_code == EXIT_OK:
        print("[Tool V1] No active Tool V1 Telegram bot process was found.")
    else:
        print(f"[Tool V1] {result.message}")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--preview", "--whatif", "-WhatIf", dest="preview", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--mode", choices=("stop", "restart"), default="stop")
    args = parser.parse_args(list(argv) if argv is not None else None)
    res = stop_v1_bot(
        project_root=args.project_root,
        workspace=args.workspace,
        preview=args.preview,
        force=args.force,
        mode=args.mode,
    )
    return res.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
