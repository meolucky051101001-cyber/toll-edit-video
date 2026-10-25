# -*- coding: utf-8 -*-
"""F1 — Thống kê hiệu suất pipeline: thời lượng từng bước, throughput, tỷ lệ lỗi.

Dữ liệu ghi tại job_tracker.finish_video (mỗi video hoàn thành 1 dòng),
lưu ring-buffer 500 mục trong workspace/control/video_stats.json.
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("stats_service")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
CONTROL_DIR = WORKSPACE / "control"
STATS_FILE = CONTROL_DIR / "video_stats.json"
MAX_ENTRIES = 500


def _load() -> List[Dict[str, Any]]:
    try:
        if STATS_FILE.is_file():
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
    except Exception as exc:
        logger.warning("video_stats.json hỏng, tạo lại: %s", exc)
    return []


def append_video_stats(
    video_name: str,
    duration_s: float,
    total_s: float,
    step_durations: Dict[str, float],
    mode: str = "",
    ok: bool = True,
) -> None:
    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "ts": time.time(),
        "video_name": str(video_name or "")[:200],
        "duration_s": round(float(duration_s or 0.0), 1),
        "total_s": round(float(total_s or 0.0), 1),
        "steps": {
            str(k): round(float(v), 1)
            for k, v in (step_durations or {}).items()
            if isinstance(v, (int, float))
        },
        "mode": str(mode or ""),
        "ok": bool(ok),
    }
    items = _load()
    items.append(entry)
    items = items[-MAX_ENTRIES:]
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATS_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        tmp.replace(STATS_FILE)
    except Exception as exc:
        logger.error("Không thể ghi video_stats.json: %s", exc)


def _median(values: List[float]) -> float:
    vals = sorted(v for v in values if isinstance(v, (int, float)) and v >= 0)
    return round(statistics.median(vals), 1) if vals else 0.0


def get_summary(days: int = 14) -> Dict[str, Any]:
    """Tổng hợp: trung vị thời lượng từng bước, số video/ngày, tỷ lệ xử lý so với thời gian thực."""
    items = _load()
    cutoff = time.time() - max(1, int(days)) * 86400
    items = [x for x in items if float(x.get("ts") or 0) >= cutoff]

    per_step: Dict[str, List[float]] = {}
    per_day: Dict[str, Dict[str, float]] = {}
    total_source_s = 0.0
    total_proc_s = 0.0
    for x in items:
        for k, v in (x.get("steps") or {}).items():
            per_step.setdefault(str(k), []).append(float(v))
        day = str(x.get("date") or "?")
        d = per_day.setdefault(day, {"count": 0, "total_s": 0.0, "source_s": 0.0, "errors": 0})
        d["count"] += 1
        d["total_s"] += float(x.get("total_s") or 0.0)
        d["source_s"] += float(x.get("duration_s") or 0.0)
        if not x.get("ok", True):
            d["errors"] += 1
        total_source_s += float(x.get("duration_s") or 0.0)
        total_proc_s += float(x.get("total_s") or 0.0)

    done = [x for x in items if x.get("ok", True)]
    speed_ratio = (
        round(total_proc_s / total_source_s, 2)
        if total_source_s > 60 and total_proc_s > 0
        else None
    )

    return {
        "window_days": max(1, int(days)),
        "videos": len(done),
        "errors": sum(1 for x in items if not x.get("ok", True)),
        "source_minutes": round(total_source_s / 60, 1),
        "processing_minutes": round(total_proc_s / 60, 1),
        "speed_ratio": speed_ratio,
        "avg_total_s": round(total_proc_s / len(done), 1) if done else 0.0,
        "per_step_median_s": {k: _median(v) for k, v in sorted(per_step.items())},
        "per_day": [
            {
                "date": day,
                "count": d["count"],
                "total_s": round(d["total_s"], 1),
                "source_minutes": round(d["source_s"] / 60, 1),
                "errors": int(d["errors"]),
            }
            for day, d in sorted(per_day.items())
        ][-14:],
        "generated_at": time.time(),
    }
