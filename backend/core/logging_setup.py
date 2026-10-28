"""Cấu hình logging dùng chung, tự động che token/mật khẩu trước khi ghi log."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable

DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3")

_SECRET_PATTERNS = (
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(token|api[_-]?key|secret|password|passwd|x-local-control-token)"
        r"([\"']?\s*[=:]\s*[\"']?)([^\s&,;\"']+)"
    ),
)

REDACTED = "<redacted>"


def redact_secrets(text: str) -> str:
    if not text:
        return text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: m.group(1) + m.group(2) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def _normalize_paths(log_files: str | Path | Iterable[str | Path] | None) -> list[Path]:
    if log_files is None:
        return []
    if isinstance(log_files, (str, Path)):
        return [Path(log_files)]
    return [Path(p) for p in log_files]


def setup_logging(
    service: str,
    log_files: str | Path | Iterable[str | Path] | None = None,
    *,
    level: int = logging.INFO,
    console: bool = True,
    force: bool = False,
    fmt: str = DEFAULT_FORMAT,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> logging.Logger:
    root = logging.getLogger()
    if not force and any(getattr(handler, "_autodub_service", None) == service for handler in root.handlers):
        return logging.getLogger(service)

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

    formatter = RedactingFormatter(fmt)
    handlers: list[logging.Handler] = []

    for path in _normalize_paths(log_files):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler: logging.Handler = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
        except OSError:
            continue
        handler.setFormatter(formatter)
        handler._autodub_service = service
        handlers.append(handler)

    if console and sys.stdout is not None:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler._autodub_service = service
        handlers.append(stream_handler)

    for handler in handlers:
        root.addHandler(handler)

    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)

    for name in _NOISY_LOGGERS:
        noisy = logging.getLogger(name)
        if noisy.level == logging.NOTSET or noisy.level < logging.WARNING:
            noisy.setLevel(logging.WARNING)

    return logging.getLogger(service)
