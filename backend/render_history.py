"""
render_history.py - Quản lý và lưu trữ vĩnh viễn thời gian render/edit của từng video thành phẩm.
Có thể đọc lịch sử workspace dùng chung nếu được cấu hình qua environment.
"""
import os
import json
import time
import threading
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
_RECORD_LOCK = threading.Lock()


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


_DURATION_CACHE = {}
_DURATION_CACHE_LOCK = threading.RLock()
_DURATION_CACHE_TTL = 2.0
_JSON_CACHE = {}


def _workspace_roots():
    # Only explicit configured roots. Polling one tool must not scan unrelated
    # installations, nor accidentally discover production files in tests.
    candidates = [WORKSPACE_DIR]
    if PATHS.shared_workspace_dir:
        candidates.append(Path(PATHS.shared_workspace_dir))
    return list({os.path.normcase(str(Path(p).resolve())): Path(p) for p in candidates}.values())


def _read_cached_json(path):
    path = Path(path)
    stat = path.stat()
    key = os.path.normcase(str(path.resolve()))
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = _JSON_CACHE.get(key)
    if cached and cached[0] == signature:
        return cached[1]
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    _JSON_CACHE[key] = (signature, data)
    if len(_JSON_CACHE) > 4096:
        _JSON_CACHE.pop(next(iter(_JSON_CACHE)))
    return data


def invalidate_render_duration_cache():
    with _DURATION_CACHE_LOCK:
        _DURATION_CACHE.clear()
        _JSON_CACHE.clear()


def get_all_render_durations(output_dir: Optional[Path] = None) -> Dict[str, int]:
    """Bound repeated scans; stat-based JSON reuse notices external updates.

    Cache lifetime is two seconds. A completed local job invalidates immediately;
    external writers are observed on the next bounded refresh. Returned mappings
    are copies so one response cannot poison later polling results.
    """
    roots = _workspace_roots()
    key = (tuple(os.path.normcase(str(p.resolve())) for p in roots),
           str(HISTORY_FILE), str(output_dir or ""))
    with _DURATION_CACHE_LOCK:
        entry = _DURATION_CACHE.get(key)
        now = time.monotonic()
        if entry and now - entry[0] < _DURATION_CACHE_TTL:
            return dict(entry[1])
        result = _scan_render_durations(roots, output_dir)
        _DURATION_CACHE[key] = (time.monotonic(), result)
        if len(_DURATION_CACHE) > 8:
            _DURATION_CACHE.pop(next(iter(_DURATION_CACHE)))
        return dict(result)


def _scan_render_durations(roots, output_dir):
    meta = {}
    def register(name, value):
        try:
            duration = int(round(float(value)))
        except (ValueError, TypeError, OverflowError):
            return
        if not name or duration <= 0:
            return
        name = Path(str(name)).name
        clean = name[7:] if name.startswith("Dubbed_") else name
        for alias in (name, clean, "Dubbed_" + clean):
            meta.setdefault(alias, duration)
        stem = Path(clean).stem
        meta.setdefault("Dubbed_" + stem + ".mp4", duration)
        meta.setdefault(stem + ".mp4", duration)

    history_files = [HISTORY_FILE]
    if SHARED_HISTORY_FILE:
        history_files.append(SHARED_HISTORY_FILE)
    for root in roots:
        history_files.extend(root / rel for rel in (
            "bot_system/.render_history.json", ".render_history.json",
            "bot_system/.render_history_v2.json", ".render_history_v2.json"))
    for path in dict.fromkeys(history_files):
        try:
            data = _read_cached_json(path)
            if isinstance(data, dict):
                for name, record in data.items():
                    register(name, record.get("duration_seconds", 0) if isinstance(record, dict) else record)
        except (OSError, ValueError, TypeError):
            continue

    def duration(data):
        created, updated = data.get("created_at"), data.get("updated_at")
        try:
            if isinstance(created, (int, float)) and isinstance(updated, (int, float)):
                return max(0, updated - created)
            if created and updated:
                start = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
                return max(0, (end - start).total_seconds())
        except (ValueError, TypeError):
            pass
        stages = data.get("stages", {})
        return sum(float(stage.get("duration_s", 0) or 0) for stage in stages.values() if isinstance(stage, dict)) if isinstance(stages, dict) else 0

    for root in roots:
        for path in (root / "bot_system/job_status.json", root / "job_status.json"):
            try:
                for row in _read_cached_json(path).get("history", []):
                    register(row.get("output_path") or row.get("video_name"), row.get("duration_seconds"))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        manifests = list((root / "bot_system/manifests").glob("*.manifest.json"))
        manifests.extend(root.glob("*/pipeline_v2/job_manifest.json"))
        for path in manifests:
            try:
                data = _read_cached_json(path)
                output = data.get("output_video_path")
                # Explicit output identities prevent same-name files in another
                # destination from borrowing this render duration.
                if output_dir is not None and output and Path(output).is_absolute():
                    if Path(output).parent.resolve() != Path(output_dir).resolve():
                        continue
                name = output or data.get("video_name") or data.get("metadata", {}).get("source_path") or path.parent.parent.name
                register(name, duration(data))
            except (OSError, ValueError, TypeError, AttributeError, OverflowError):
                continue
    return meta


def record_render_duration(video_name_or_path: str, duration_seconds: float) -> None:
    """Ghi nhận thời gian render đồng thời vào cả .render_history.json và .render_history_v2.json một cách an toàn."""
    if not duration_seconds or duration_seconds <= 0:
        return
    invalidate_render_duration_cache()
    clean_name = os.path.basename(video_name_or_path)
    dur_int = int(round(duration_seconds))

    target_files = [HISTORY_FILE]
    # msvcrt chỉ khóa giữa tiến trình; khóa thread để chống lost-update trong cùng process.
    with _RECORD_LOCK:
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
                        try:
                            import shutil
                            corrupt_backup = history_file.parent / f".render_history.corrupt.{int(time.time())}.json"
                            shutil.copy2(history_file, corrupt_backup)
                        except Exception:
                            pass
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
