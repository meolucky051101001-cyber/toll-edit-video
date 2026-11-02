"""Compatibility entry point for the canonical root release packager."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from package_v1_release import *  # noqa: F401,F403,E402
from package_v1_release import main as _main


if __name__ == "__main__":
    raise SystemExit(_main())
