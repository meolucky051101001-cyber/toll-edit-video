"""Shared .env loading: process variables override local file defaults."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping, Optional


def _parse_dotenv_into(dotenv: Path, values: dict) -> None:
    if not dotenv.is_file():
        return
    for raw_line in dotenv.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if value[:1] in {"'", '"'} and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values.setdefault(key, value)


def read_environment(backend_directory: Path, environment: Optional[Mapping[str, str]] = None) -> dict:
    values = dict(os.environ if environment is None else environment)
    base = Path(backend_directory)
    _parse_dotenv_into(base / ".env", values)
    if base.name.lower() == "backend":
        _parse_dotenv_into(base.parent / ".env", values)
    return values


def load_environment(backend_directory: Path) -> None:
    for key, value in read_environment(backend_directory).items():
        os.environ.setdefault(key, value)
