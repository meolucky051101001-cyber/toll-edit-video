"""
qc_service.py - Service to query, list, and format QC Gate reports for Tool V2.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def list_qc_reports(workspace_dir: Path) -> List[Dict[str, Any]]:
    reports = []
    if not workspace_dir.is_dir():
        return reports

    for qf in workspace_dir.rglob("qc_report.json"):
        try:
            st = qf.stat()
            data = json.loads(qf.read_text(encoding="utf-8"))
            vpath = data.get("video_path") or ""
            vname = Path(vpath).name if vpath else qf.parent.name
            
            # Determine job dir name
            job_name = qf.parents[2].name if len(qf.parents) >= 3 else qf.parent.name
            if "artifacts" in job_name or "qc" in job_name:
                job_name = qf.parents[3].name if len(qf.parents) >= 4 else job_name

            metrics = data.get("metrics") or {}
            loudness = metrics.get("integrated_loudness_lufs")
            peak = metrics.get("true_peak_dbtp")
            dur_delta = metrics.get("duration_delta_seconds")
            overall = data.get("overall", "unknown")
            summary = data.get("summary") or {}

            reports.append({
                "job_name": job_name,
                "video_name": vname,
                "video_stem": Path(vname).stem,
                "report_path": str(qf.resolve()),
                "timestamp": st.st_mtime,
                "date": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "overall": overall,
                "is_pass": overall in {"ok", "pass"},
                "loudness_lufs": round(float(loudness), 1) if loudness is not None else None,
                "true_peak_dbtp": round(float(peak), 1) if peak is not None else None,
                "duration_delta": round(float(dur_delta), 3) if dur_delta is not None else None,
                "summary": summary
            })
        except Exception:
            continue

    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    return reports


def get_qc_report(workspace_dir: Path, target: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not workspace_dir.is_dir():
        return None

    all_reports = list_qc_reports(workspace_dir)
    if not all_reports:
        return None

    selected_report_meta = None
    if target:
        target_lower = target.lower().strip()
        target_stem = Path(target_lower).stem
        if target_stem.startswith("dubbed_"):
            target_stem = target_stem[7:]

        for r in all_reports:
            r_stem = r["video_stem"].lower()
            if r_stem.startswith("dubbed_"):
                r_stem = r_stem[7:]
            if (target_lower in r["video_name"].lower() or 
                target_stem in r_stem or 
                r_stem in target_stem or 
                target_lower in r["job_name"].lower()):
                selected_report_meta = r
                break

    if not selected_report_meta:
        selected_report_meta = all_reports[0]

    report_path = Path(selected_report_meta["report_path"])
    if not report_path.is_file():
        return None

    try:
        raw_data = json.loads(report_path.read_text(encoding="utf-8"))
        raw_data["meta"] = selected_report_meta
        raw_data["all_reports_count"] = len(all_reports)
        return raw_data
    except Exception:
        return None
