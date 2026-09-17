"""
render_history.py - Quản lý và lưu trữ vĩnh viễn thời gian render/edit của từng video thành phẩm.
Hỗ trợ đọc và ghi đồng bộ giữa Tool V1, Tool V2 và Telegram Bot qua D:\banve\.render_history.json.
"""
import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

SHARED_HISTORY_FILE = Path(r"D:\banve\.render_history.json")
V2_HISTORY_FILE = Path(os.getenv("TOOL_V2_RENDER_HISTORY", r"D:\banve\.render_history_v2.json"))
HISTORY_FILE = V2_HISTORY_FILE


def format_duration(seconds: float) -> str:
    """Định dạng giây sang dạng Xp Ys hoặc Xs."""
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


def get_all_render_durations(output_dir: Optional[Path] = None) -> Dict[str, int]:
    """Đọc toàn bộ lịch sử thời gian render từ các file lịch sử và job manifest V2."""
    meta: Dict[str, int] = {}

    # 1. Đọc từ các file history JSON trong D:\banve
    target_dir = Path(output_dir) if output_dir else Path(r"D:\banve")
    history_files = [
        target_dir / ".render_history.json",
        target_dir / ".render_history_v2.json",
        SHARED_HISTORY_FILE,
        V2_HISTORY_FILE,
    ]
    for hf in history_files:
        if hf.exists():
            try:
                raw = json.loads(hf.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        if isinstance(v, (int, float)) and v > 0:
                            meta[k] = int(v)
                        elif isinstance(v, dict) and "duration_seconds" in v:
                            meta[k] = int(v["duration_seconds"])
            except Exception:
                pass

    # 2. Đọc bổ sung từ job_status.json (nếu có)
    workspace_candidates = [
        Path(r"C:\tool v1\workspace\job_status.json"),
        Path(r"C:\tool v2\workspace\job_status.json"),
        Path(__file__).resolve().parents[1] / "workspace" / "job_status.json",
    ]
    for ws in workspace_candidates:
        if ws.exists():
            try:
                data = json.loads(ws.read_text(encoding="utf-8"))
                for item in data.get("history", []):
                    dur = item.get("duration_seconds")
                    if not dur or dur <= 0:
                        continue
                    out_p = item.get("output_path", "")
                    if out_p:
                        bname = os.path.basename(out_p)
                        meta.setdefault(bname, int(dur))
                    vname = item.get("video_name", "")
                    if vname:
                        meta.setdefault(vname, int(dur))
                        meta.setdefault(f"Dubbed_{vname}", int(dur))
            except Exception:
                pass

    # 3. Đọc bổ sung từ các job manifest V2 trong workspace
    v2_workspaces = [
        Path(r"C:\tool v2\workspace"),
        Path(r"C:\tool v1\workspace"),
        Path(__file__).resolve().parents[1] / "workspace",
    ]
    for root_ws in v2_workspaces:
        if root_ws.is_dir():
            for mf in root_ws.glob("*/pipeline_v2/job_manifest.json"):
                try:
                    d = json.loads(mf.read_text(encoding="utf-8"))
                    c_at = d.get("created_at")
                    u_at = d.get("updated_at")
                    if c_at and u_at:
                        t0 = datetime.fromisoformat(c_at.replace("Z", "+00:00"))
                        t1 = datetime.fromisoformat(u_at.replace("Z", "+00:00"))
                        dur = int(round(max(0, (t1 - t0).total_seconds())))
                        if dur > 0:
                            jname = mf.parent.parent.name
                            meta.setdefault(jname, dur)
                            meta.setdefault(f"{jname}.mp4", dur)
                            meta.setdefault(f"Dubbed_{jname}", dur)
                            meta.setdefault(f"Dubbed_{jname}.mp4", dur)
                            meta.setdefault(f"final_{jname}.mp4", dur)
                            sp = d.get("metadata", {}).get("source_path")
                            if sp:
                                sname = Path(sp).name
                                meta.setdefault(sname, dur)
                                meta.setdefault(f"Dubbed_{sname}", dur)
                                if not sname.lower().endswith(".mp4"):
                                    meta.setdefault(f"{sname}.mp4", dur)
                                    meta.setdefault(f"Dubbed_{sname}.mp4", dur)
                except Exception:
                    pass

    return meta


def record_render_duration(video_name_or_path: str, duration_seconds: float) -> None:
    """Ghi nhận thời gian render đồng thời vào cả .render_history.json và .render_history_v2.json."""
    if not duration_seconds or duration_seconds <= 0:
        return
    clean_name = os.path.basename(video_name_or_path)
    dur_int = int(round(duration_seconds))

    target_files = [HISTORY_FILE]
    for history_file in target_files:
        try:
            meta: Dict[str, int] = {}
            if history_file.exists():
                try:
                    raw = json.loads(history_file.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        meta = {k: int(v if isinstance(v, (int, float)) else v.get("duration_seconds", 0)) 
                                for k, v in raw.items() if v}
                except Exception:
                    meta = {}

            meta[clean_name] = dur_int
            if clean_name.startswith("Dubbed_"):
                meta[clean_name[7:]] = dur_int
            else:
                meta[f"Dubbed_{clean_name}"] = dur_int

            history_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = history_file.with_suffix(history_file.suffix + f".tmp.{os.getpid()}")
            temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(history_file)
        except Exception:
            pass
