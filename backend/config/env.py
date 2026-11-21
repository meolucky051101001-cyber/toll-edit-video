"""Typed readers for environment variables.

One place for the parsing rules that were repeated across modules. The list of
variables the backend reads is generated into docs/ENV_VARS.md.
"""
from __future__ import annotations

import os

_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off")


def env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_flag(name: str, default: bool = False) -> bool:
    """A default-on flag is off only for 0/false/no/off; a default-off flag is on only for 1/true/yes/on."""
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    return normalized not in _OFF if default else normalized in _ON


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip())
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip())
    except ValueError:
        return default
