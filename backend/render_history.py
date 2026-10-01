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
LOCK_FILE = HISTORY_FILE.with_name(HISTORY_FILE.name + ".lock")


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
    """Đọc thời lượng từ lịch sử dùng chung và manifest của cả hai pipeline."""
    meta: Dict[str, int] = {}
    workspaces = [WORKSPACE_DIR]
    if PATHS.shared_workspace_dir:
        workspaces.append(PATHS.shared_workspace_dir)

    def register(key: str, duration: int) -> None:
        if not key or duration <= 0:
            return
        meta.setdefault(key, duration)
        clean = key.removeprefix("Dubbed_")
        meta.setdefault(clean, duration)
        meta.setdefault(f"Dubbed_{clean}", duration)
        if not clean.lower().endswith(".mp4"):
            meta.setdefault(f"{clean}.mp4", duration)
            meta.setdefault(f"Dubbed_{clean}.mp4", duration)

    history_files = [V2_HISTORY_FILE]
    for workspace in workspaces:
        history_files.extend(
            [
                workspace / "bot_system" / ".render_history_v2.json",
                workspace / "bot_system" / ".render_history.json",
                workspace / ".render_history_v2.json",
                workspace / ".render_history.json",
            ]
        )

    for hf in history_files:
        if hf.is_file():
            try:
                raw = json.loads(hf.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        duration = v if isinstance(v, (int, float)) else v.get("duration_seconds", 0)
                        register(k, int(duration or 0))
            except Exception:
                pass

    workspace_candidates = []
    for workspace in workspaces:
        workspace_candidates.extend(
            [workspace / "bot_system" / "job_status.json", workspace / "job_status.json"]
        )
    for ws in workspace_candidates:
        if ws.is_file():
            try:
                data = json.loads(ws.read_text(encoding="utf-8"))
                for item in data.get("history", []):
                    dur = item.get("duration_seconds")
                    if dur and dur > 0:
                        out_p = item.get("output_path", "")
                        if out_p:
                            register(os.path.basename(out_p), int(dur))
                        vname = item.get("video_name", "")
                        if vname:
                            register(vname, int(dur))
            except Exception:
                pass

    def manifest_duration(data: dict) -> int:
        created = data.get("created_at")
        updated = data.get("updated_at")
        try:
            if isinstance(created, (int, float)) and isinstance(updated, (int, float)):
                return max(0, int(round(updated - created)))
            if created and updated:
                start = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                return max(0, int(round((end - start).total_seconds())))
        except (TypeError, ValueError):
            pass
        stages = data.get("stages", {})
        if isinstance(stages, dict):
            return max(
                0,
                int(
                    round(
                        sum(
                            float(stage.get("duration_s", 0) or 0)
                            for stage in stages.values()
                            if isinstance(stage, dict)
                        )
                    )
                ),
            )
        return 0

    # V1 manifests are distinct from the dashboard's summarized job history.
    for workspace in workspaces:
        md = workspace / "bot_system" / "manifests"
        if md.is_dir():
            for mf in md.glob("*.manifest.json"):
                try:
                    data = json.loads(mf.read_text(encoding="utf-8"))
                    duration = manifest_duration(data)
                    if duration > 0:
                        register(data.get("video_name", ""), duration)
                        output_path = data.get("output_video_path")
                        if output_path:
                            register(Path(output_path).name, duration)
                except Exception:
                    continue

    # V2 manifests may live in either the local or shared workspace.
    for workspace in workspaces:
        if workspace.is_dir():
            for mf in workspace.glob("*/pipeline_v2/job_manifest.json"):
                try:
                    d = json.loads(mf.read_text(encoding="utf-8"))
                    duration = manifest_duration(d)
                    if duration > 0:
                        job_name = mf.parent.parent.name
                        register(job_name, duration)
                        source_path = d.get("metadata", {}).get("source_path")
                        if source_path:
                            register(Path(source_path).name, duration)
                except Exception:
                    continue

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
