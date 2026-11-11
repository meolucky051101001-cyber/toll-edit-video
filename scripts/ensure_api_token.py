"""Generate a local API token on first start/setup so the API is never open.

An empty API_TOKEN disables backend auth entirely, so a fresh install must not
stay empty: start.ps1 and setup.ps1 run this before launching any server.
Existing tokens are never overwritten or printed.
"""

import os
import secrets
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values, set_key

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]


def ensure_api_token(env_path: Path | None = None) -> str:
    target = env_path if env_path is not None else ROOT / ".env"
    if target.exists():
        current = (dotenv_values(target).get("API_TOKEN") or "").strip()
        if current:
            return current
    token = secrets.token_urlsafe(32)
    fd, name = tempfile.mkstemp(prefix=".env.", dir=target.parent)
    staging = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(target.read_text(encoding="utf-8-sig") if target.exists() else "")
        set_key(str(staging), "API_TOKEN", token)
        os.replace(staging, target)
    finally:
        staging.unlink(missing_ok=True)
    return token


def main() -> int:
    target = ROOT / ".env"
    had_token = bool((dotenv_values(target).get("API_TOKEN") or "").strip()) if target.exists() else False
    ensure_api_token()
    if had_token:
        print("API token already configured; left unchanged.")
    else:
        print("Generated a new local API token in .env (value not printed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())