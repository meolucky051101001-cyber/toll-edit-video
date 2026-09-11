"""
render_history.py - Quản lý và lưu trữ vĩnh viễn thời gian render/edit của từng video thành phẩm.
Lưu trữ vào D:\banve\.render_history.json để dùng chung cho cả Tool V1, Tool V2 và Telegram Bot.
"""
import os
import json
from pathlib import Path
from typing import Dict, Optional

HISTORY_FILE = Path(r"D:\banve\.render_history.json")


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
    """Đọc toàn bộ lịch sử thời gian render từ file D:\banve\.render_history.json và cache runtime."""
    history_file = (output_dir / ".render_history.json") if output_dir else HISTORY_FILE
    meta: Dict[str, int] = {}
    if history_file.exists():
        try:
            raw = json.loads(history_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, (int, float)) and v > 0:
                        meta[k] = int(v)
                    elif isinstance(v, dict) and "duration_seconds" in v:
                        meta[k] = int(v["duration_seconds"])
        except Exception:
            pass

    # Đọc bổ sung từ job_status.json nếu chưa có
    workspace_candidates = [
        Path(r"C:\tool v1\workspace\job_status.json"),
        Path(r"C:\tool v2\workspace\job_status.json"),
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
                        if bname not in meta:
                            meta[bname] = int(dur)
                    vname = item.get("video_name", "")
                    if vname:
                        if vname not in meta:
                            meta[vname] = int(dur)
                        if f"Dubbed_{vname}" not in meta:
                            meta[f"Dubbed_{vname}"] = int(dur)
            except Exception:
                pass

    return meta


def record_render_duration(video_name_or_path: str, duration_seconds: float) -> None:
    """Ghi nhận thời gian render của video vào file lịch sử D:\banve\.render_history.json."""
    if not duration_seconds or duration_seconds <= 0:
        return
    try:
        clean_name = os.path.basename(video_name_or_path)
        history_file = HISTORY_FILE
        meta: Dict[str, int] = {}
        if history_file.exists():
            try:
                raw = json.loads(history_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    meta = {k: int(v if isinstance(v, (int, float)) else v.get("duration_seconds", 0)) 
                            for k, v in raw.items() if v}
            except Exception:
                meta = {}

        meta[clean_name] = int(round(duration_seconds))
        if clean_name.startswith("Dubbed_"):
            meta[clean_name[7:]] = int(round(duration_seconds))
        else:
            meta[f"Dubbed_{clean_name}"] = int(round(duration_seconds))

        history_file.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass