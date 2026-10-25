# -*- coding: utf-8 -*-
"""F12 — Quản lý dung lượng workspace: liệt kê từng job dir (dung lượng, stems,
SRT, dubbing) và dọn an toàn (chỉ stems — giữ lại SRT/dubbing để còn sửa câu — hoặc cả job)."""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("workspace_usage_service")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))

STEMS_DIR_NAMES = ("bs_roformer", "demucs", "separated")
SKIP_DIR_NAMES = {"bot_system", "control", "downloads", "voice_previews_cache", "__pycache__"}


# Cache theo bộ root để tránh quét lại ổ đĩa nặng (17s+ trên workspace thật) trong 60s
_USAGE_CACHE: Dict[str, Any] = {}
_USAGE_TTL_SECONDS = 60.0


def _workspace_roots() -> List[Path]:
    try:
        from segment_editor_service import _workspace_roots as _seg_roots

        return _seg_roots()
    except Exception:
        return [WORKSPACE]


def _dir_size_mb(p: Path) -> float:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total / (1024 * 1024)


def list_job_usage(force: bool = False) -> Dict[str, Any]:
    roots = _workspace_roots()
    cache_key = "|".join(str(r) for r in roots)
    cached = _USAGE_CACHE.get(cache_key)
    if not force and cached and time.time() - float(cached.get("_cached_at", 0)) < _USAGE_TTL_SECONDS:
        return {k: v for k, v in cached.items() if k != "_cached_at"}

    jobs: List[Dict[str, Any]] = []
    total = 0.0
    for root in _workspace_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if not child.is_dir() or child.name in SKIP_DIR_NAMES:
                    continue
                size_mb = _dir_size_mb(child)
                if size_mb < 1:
                    continue
                total += size_mb
                jobs.append({
                    "name": child.name,
                    "root": str(root),
                    "size_mb": round(size_mb, 1),
                    "stems_mb": round(sum(_dir_size_mb(child / d) for d in STEMS_DIR_NAMES if (child / d).is_dir()), 1),
                    "has_dubbing": (child / "dubbing").is_dir(),
                    "has_srt": (child / "translated.srt").is_file(),
                    "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(child.stat().st_mtime)),
                })
            except OSError:
                continue
    jobs.sort(key=lambda j: -j["size_mb"])
    result = {
        "jobs": jobs[:200],
        "total_mb": round(total, 1),
        "roots": [str(r) for r in roots],
    }
    _USAGE_CACHE[cache_key] = {**result, "_cached_at": time.time()}
    return result


def invalidate_usage_cache() -> None:
    _USAGE_CACHE.clear()


def cleanup_jobs(names: List[str], mode: str = "stems", active_names=()) -> Dict[str, Any]:
    """Xóa stems (giữ SRT/dubbing) hoặc cả job dir. Chặn tên lạ và job đang xử lý."""
    invalidate_usage_cache()
    results: List[Dict[str, Any]] = []
    active_norm = set()
    for a in (active_names or ()):
        if not a:
            continue
        s = str(a).strip().lower()
        active_norm.add(s)
        active_norm.add(Path(s).stem)  # tên video có đuôi .mp4, tên job dir thì không
    roots = [r for r in _workspace_roots() if r.is_dir()]

    for raw in names or []:
        name = str(raw or "").strip()
        if not name or name in (".", "..") or name != Path(name).name or any(ch in '<>:"|?*' for ch in name):
            results.append({"name": name, "status": "rejected", "reason": "Tên không hợp lệ"})
            continue
        if name.lower() in active_norm:
            results.append({"name": name, "status": "rejected", "reason": "Video này đang được xử lý"})
            continue
        target = next((root / name for root in roots if (root / name).is_dir()), None)
        if not target:
            results.append({"name": name, "status": "not_found"})
            continue
        try:
            resolved = target.resolve()
            inside = any(resolved == r.resolve() or resolved.is_relative_to(r.resolve()) for r in roots)
            if not inside:
                results.append({"name": name, "status": "rejected", "reason": "Ngoài phạm vi workspace"})
                continue
            if mode == "stems":
                freed = 0.0
                for d in STEMS_DIR_NAMES:
                    p = resolved / d
                    if p.is_dir():
                        freed += _dir_size_mb(p)
                        shutil.rmtree(p, ignore_errors=True)
                results.append({"name": name, "status": "ok", "mode": "stems", "freed_mb": round(freed, 1)})
            else:
                freed = _dir_size_mb(resolved)
                shutil.rmtree(resolved, ignore_errors=True)
                results.append({"name": name, "status": "ok", "mode": "full", "freed_mb": round(freed, 1)})
        except Exception as exc:
            results.append({"name": name, "status": "error", "reason": str(exc)})

    return {
        "results": results,
        "total_freed_mb": round(sum(r.get("freed_mb", 0) for r in results if r.get("status") == "ok"), 1),
    }
