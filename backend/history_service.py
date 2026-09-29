"""
history_service.py - Task history provider aggregating render histories and completed output files.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List


def format_duration_vn(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "--"
    total_sec = int(round(seconds))
    m = total_sec // 60
    s = total_sec % 60
    if m >= 60:
        h = m // 60
        m = m % 60
        return f"{h}h {m}p"
    if m > 0:
        return f"{m}p {s:02d}s"
    return f"{s}s"


def get_task_history(tool_name: str, output_dir: Path, workspace_dir: Path) -> Dict[str, Any]:
    from render_history import get_all_render_durations
    durations = get_all_render_durations(output_dir)

    items: List[Dict[str, Any]] = []
    seen_names = set()

    # 1. Scan current output folder
    if output_dir.is_dir():
        media_exts = {".mp4", ".mkv", ".mov", ".webm", ".avi"}
        for f in output_dir.iterdir():
            if f.is_file() and f.suffix.lower() in media_exts:
                st = f.stat()
                clean_name = f.name.replace("Dubbed_", "")
                dur = (
                    durations.get(f.name)
                    or durations.get(clean_name)
                    or durations.get(f"Dubbed_{f.name}")
                    or 0
                )
                mtime_dt = datetime.datetime.fromtimestamp(st.st_mtime)
                items.append({
                    "name": f.name,
                    "stem": f.stem,
                    "size_mb": round(st.st_size / (1024 * 1024), 2),
                    "completed_at": mtime_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    "timestamp": st.st_mtime,
                    "duration_seconds": int(dur),
                    "duration_formatted": format_duration_vn(dur),
                    "file_exists": True,
                    "status": "completed",
                    "path": str(f.resolve())
                })
                seen_names.add(f.name)
                seen_names.add(clean_name)

    # 2. Add historical entries from durations dict not currently in output directory
    for name, dur in durations.items():
        if not name or dur <= 0:
            continue
        clean_name = name.replace("Dubbed_", "")
        if name in seen_names or clean_name in seen_names:
            continue
        seen_names.add(name)
        seen_names.add(clean_name)

        display_name = name if name.endswith(".mp4") else f"{name}.mp4"
        items.append({
            "name": display_name,
            "stem": Path(display_name).stem,
            "size_mb": 0.0,
            "completed_at": "--",
            "timestamp": 0,
            "duration_seconds": int(dur),
            "duration_formatted": format_duration_vn(dur),
            "file_exists": False,
            "status": "archived",
            "path": ""
        })

    # Sort: existing files with timestamp first, then others by duration
    items.sort(key=lambda x: (x["file_exists"], x["timestamp"]), reverse=True)

    # Compute aggregate statistics
    valid_durations = [x["duration_seconds"] for x in items if x["duration_seconds"] > 0]
    total_seconds = sum(valid_durations)
    count = len(items)
    avg_sec = int(round(total_seconds / len(valid_durations))) if valid_durations else 0

    stats = {
        "total_tasks": count,
        "existing_files": sum(1 for x in items if x["file_exists"]),
        "archived_tasks": sum(1 for x in items if not x["file_exists"]),
        "total_render_seconds": total_seconds,
        "total_render_formatted": format_duration_vn(total_seconds),
        "avg_render_seconds": avg_sec,
        "avg_render_formatted": format_duration_vn(avg_sec),
        "fastest_seconds": min(valid_durations) if valid_durations else 0,
        "fastest_formatted": format_duration_vn(min(valid_durations)) if valid_durations else "--",
        "slowest_seconds": max(valid_durations) if valid_durations else 0,
        "slowest_formatted": format_duration_vn(max(valid_durations)) if valid_durations else "--",
    }

    return {
        "tool": tool_name,
        "stats": stats,
        "history": items
    }
