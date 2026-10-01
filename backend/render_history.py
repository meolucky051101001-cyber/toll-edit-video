"""
render_history.py - Quản lý và lưu trữ vĩnh viễn thời gian render/edit của từng video thành phẩm.
Có thể đọc lịch sử workspace dùng chung nếu được cấu hình qua environment.
"""
import os
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

try:
    from pipeline_v2.atomic_io import atomic_write_json
except ImportError:
    try:
        from backend.pipeline_v2.atomic_io import atomic_write_json
    except ImportError:
        atomic_write_json = None

try:
    from backend.config.paths import AppPaths
    from backend.environment import read_environment
except ImportError:
    from config.paths import AppPaths
    from environment import read_environment

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
PATHS = AppPaths.from_environment(PROJECT_ROOT, read_environment(BACKEND_DIR))
WORKSPACE_DIR = PATHS.workspace
def _resolve_v2_history():
    bs = WORKSPACE_DIR / "bot_system"
    target = bs / ".render_history_v2.json"
    if target.exists() or bs.is_dir():
        return target
    return WORKSPACE_DIR / ".render_history_v2.json"

SHARED_HISTORY_FILE = (
    PATHS.shared_workspace_dir / "bot_system" / ".render_history.json"
    if PATHS.shared_workspace_dir
    else None
)
V2_HISTORY_FILE = Path(os.getenv("TOOL_V2_RENDER_HISTORY", str(_resolve_v2_history())))
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
    """Đọc toàn bộ lịch sử thời gian render từ các file lịch sử trong workspace, V1 manifests và V2 manifests."""
    meta: Dict[str, int] = {}

    def _register(key: str, dur: int):
        if not key:
            return
        try:
            dur = int(dur)
        except (TypeError, ValueError):
            return
        if dur <= 0:
            return
        key = Path(str(key)).name
        meta.setdefault(key, dur)
        clean = key[7:] if key.startswith("Dubbed_") else key
        meta.setdefault(clean, dur)
        meta.setdefault(f"Dubbed_{clean}", dur)
        if not clean.lower().endswith(".mp4"):
            meta.setdefault(f"{clean}.mp4", dur)
            meta.setdefault(f"Dubbed_{clean}.mp4", dur)

    workspace_roots = [WORKSPACE_DIR]
    if PATHS.shared_workspace_dir:
        workspace_roots.append(PATHS.shared_workspace_dir)

    # 1. Đọc lịch sử JSON trong workspace hiện hành và workspace chia sẻ.
    history_files = [
        V2_HISTORY_FILE,
        WORKSPACE_DIR / "bot_system" / ".render_history_v2.json",
        WORKSPACE_DIR / "bot_system" / ".render_history.json",
        WORKSPACE_DIR / ".render_history.json",
    ]
    if PATHS.shared_workspace_dir:
        history_files.extend(
            [
                SHARED_HISTORY_FILE,
                PATHS.shared_workspace_dir / "bot_system" / ".render_history_v2.json",
                PATHS.shared_workspace_dir / ".render_history_v2.json",
                PATHS.shared_workspace_dir / "bot_system" / ".render_history.json",
                PATHS.shared_workspace_dir / ".render_history.json",
            ]
        )
    for hf in history_files:
        if hf.exists():
            try:
                raw = json.loads(hf.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        dur_val = v if isinstance(v, (int, float)) else v.get("duration_seconds", 0)
                        _register(k, dur_val)
            except Exception:
                pass

    # 2. Đọc bổ sung từ job_status.json (nếu có)
    workspace_candidates = [
        WORKSPACE_DIR / "bot_system" / "job_status.json",
        WORKSPACE_DIR / "job_status.json",
    ]
    if PATHS.shared_workspace_dir:
        workspace_candidates.extend(
            [
                PATHS.shared_workspace_dir / "bot_system" / "job_status.json",
                PATHS.shared_workspace_dir / "job_status.json",
            ]
        )
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
                        _register(os.path.basename(out_p), int(dur))
                    vname = item.get("video_name", "")
                    if vname:
                        _register(vname, int(dur))
            except Exception:
                pass

    # 3. Đọc V1 manifests theo workspace đã cấu hình, không hard-code máy cụ thể.
    manifest_dirs = [root / "bot_system" / "manifests" for root in workspace_roots]
    for md in manifest_dirs:
        if md.is_dir():
            try:
                for mf in md.glob("*.manifest.json"):
                    try:
                        d = json.loads(mf.read_text(encoding="utf-8"))
                        vname = d.get("video_name")
                        out_p = d.get("output_video_path")
                        c_at = d.get("created_at")
                        u_at = d.get("updated_at")
                        dur = 0
                        if isinstance(c_at, (int, float)) and isinstance(u_at, (int, float)):
                            dur = int(round(u_at - c_at))
                        if not dur:
                            stages = d.get("stages", {})
                            dur = int(round(sum(
                                (st.get("duration_s", 0) or 0)
                                for st in stages.values() if isinstance(st, dict)
                            )))
                        if dur > 0:
                            if vname:
                                _register(vname, dur)
                            if out_p:
                                _register(os.path.basename(out_p), dur)
                    except Exception:
                        pass
            except Exception:
                pass

    # 4. Đọc V2 manifests trong các workspace đã cấu hình.
    for root_ws in workspace_roots:
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
                            _register(jname, dur)
                            _register(f"Dubbed_{jname}.mp4", dur)
                            sp = d.get("metadata", {}).get("source_path")
                            if sp:
                                _register(Path(sp).name, dur)
                except Exception:
                    pass

    return meta


def record_render_duration(video_name_or_path: str, duration_seconds: float) -> None:
    """Ghi nhận thời gian render đồng thời vào cả .render_history.json và .render_history_v2.json một cách an toàn."""
    if not duration_seconds or duration_seconds <= 0:
        return
    clean_name = os.path.basename(video_name_or_path)
    dur_int = int(round(duration_seconds))

    target_files = [HISTORY_FILE]
    for history_file in target_files:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = history_file.with_name(history_file.name + ".lock")
        lock_fd = None

        # Khóa file để đồng bộ đa tiến trình trên Windows
        try:
            import msvcrt
            lock_fd = open(lock_file, "a+", encoding="utf-8")
            for _ in range(30):
                try:
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (IOError, OSError):
                    time.sleep(0.05)
        except Exception:
            pass

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

            if atomic_write_json is not None:
                atomic_write_json(history_file, meta)
            else:
                temporary = history_file.with_suffix(history_file.suffix + f".tmp.{os.getpid()}")
                temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                # Thử lại khi có tranh chấp trên Windows
                for attempt in range(8):
                    try:
                        temporary.replace(history_file)
                        break
                    except OSError:
                        time.sleep(min(0.05 * (2 ** attempt), 0.5))
        except Exception as e:
            import logging
            logging.getLogger("render_history").warning(f"Lỗi ghi nhận lịch sử render vào {history_file}: {e}")
        finally:
            if lock_fd:
                try:
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    lock_fd.close()
                except Exception:
                    pass
