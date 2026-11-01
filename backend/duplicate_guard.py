# -*- coding: utf-8 -*-
"""F10 — Chống xử lý trùng lặp: fingerprint (size + SHA-256 của 64KB đầu file)
đăng ký mỗi video hoàn thành / tải lên; pre-run summary và upload dùng để cảnh báo trùng.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("duplicate_guard")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
CONTROL_DIR = WORKSPACE / "control"
REGISTRY_FILE = CONTROL_DIR / "seen_videos.json"
MAX_ENTRIES = 1000
_HEAD_BYTES = 65536


def fingerprint_path(path: Path) -> Optional[str]:
    try:
        st = path.stat()
        h = hashlib.sha256()
        h.update(str(st.st_size).encode("utf-8"))
        with open(path, "rb") as f:
            head = f.read(_HEAD_BYTES)
        h.update(head)
        return h.hexdigest()
    except Exception:
        return None


def _load() -> Dict[str, Any]:
    try:
        if REGISTRY_FILE.is_file():
            data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                return data
    except Exception as exc:
        logger.warning("seen_videos.json hỏng, tạo lại: %s", exc)
    return {"entries": {}}


def _save(data: Dict[str, Any]) -> None:
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = REGISTRY_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(REGISTRY_FILE)
    except Exception as exc:
        logger.error("Không thể ghi seen_videos.json: %s", exc)


def register(kind: str, name: str, path: Path, extra: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Đăng ký fingerprint của một file (kind: 'output' | 'upload')."""
    fp = fingerprint_path(path)
    if not fp:
        return None
    data = _load()
    entries: Dict[str, Any] = data["entries"]
    entries[fp] = {
        "kind": kind,
        "name": str(name or path.name)[:200],
        "path": str(path),
        "registered_at": time.time(),
        **(extra or {}),
    }
    if len(entries) > MAX_ENTRIES:
        for stale in sorted(entries, key=lambda k: entries[k].get("registered_at", 0))[: len(entries) - MAX_ENTRIES]:
            entries.pop(stale, None)
    _save(data)
    return fp


def check_path(path: Path) -> Optional[Dict[str, Any]]:
    """Trả về thông tin bản trùng (nội dung giống hệt) đã đăng ký trước đây, hoặc None."""
    fp = fingerprint_path(path)
    if not fp:
        return None
    hit = _load()["entries"].get(fp)
    if not hit:
        return None
    # Bỏ qua trường hợp file tự nó vừa được đăng ký với đúng tên/path này
    if hit.get("kind") == "upload" and Path(str(hit.get("path", ""))).name == path.name:
        return None
    return hit
