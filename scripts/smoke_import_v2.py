"""Dependency-light smoke check for Tool V2 configuration and core imports."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from workflow_api import router as workflow_router
from workflow_api import serve_workflow_page

from backend.config.paths import AppPaths
from backend.pipeline_v2.config import PipelineMode, PipelineSettings
from backend.pipeline_v2.download_validation import require_complete_response


def main() -> int:
    paths = AppPaths.from_environment(ROOT, {})
    settings = PipelineSettings.from_env({"PIPELINE_MODE": "v2"})
    require_complete_response(200, {})
    page = asyncio.run(serve_workflow_page())
    page_html = page.body.decode("utf-8")
    if (
        settings.mode is not PipelineMode.V2
        or paths.project_root != ROOT.resolve()
        or not workflow_router.routes
        or "AUTO_DUB_PATHS" not in page_html
        or "__CONFIGURED_" in page_html
    ):
        raise RuntimeError("Tool V2 smoke check failed")
    print("Tool V2 core imports and configuration: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
