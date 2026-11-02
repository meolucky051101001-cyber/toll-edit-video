"""Compatibility re-export shim for canonical backend/whiteboard_animator/cli.py."""

from ..cli import *  # noqa: F401,F403

if __name__ == "__main__":
    raise SystemExit(main())
