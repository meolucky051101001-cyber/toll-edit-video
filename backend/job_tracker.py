"""
job_tracker.py - Bộ quản lý & theo dõi tiến độ tập trung cho Tool V1.
Ghi nhận trạng thái thời gian thực (% tiến độ, bước xử lý, thời gian, hàng đợi)
vào bộ nhớ và file json để Dashboard Web & Bot cùng đọc được.
"""

import json
import logging
import os
import time
import threading
import uuid
import copy
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Dict, Any

from pipeline_v2.atomic_io import atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))
STATUS_FILE = WORKSPACE / "job_status.json"
BATCH_LOCK_FILE = WORKSPACE / "batch.lock"

_LOCK = threading.Lock()
logger = logging.getLogger(__name__)


class JobAlreadyRunningError(RuntimeError):
    pass

# Bảng quy đổi bước xử lý sang % tiến độ mặc định
STEP_PERCENT_MAP = {
    1.0: 10,   # Trích xuất âm thanh gốc
    2.0: 25,   # Demucs tách nhạc nền & giọng nói
    3.0: 40,   # Faster-Whisper nhận dạng giọng nói
    3.5: 55,   # PP-OCRv6 dò tìm phụ đề gốc
    4.0: 70,   # Gemini 3.8 Flash dịch phụ đề
    5.0: 85,   # RVC / Hoài My lồng tiếng AI
    6.0: 95,   # NVENC Hardware Render video
}

_DEFAULT_STATE: Dict[str, Any] = {
    "schema_version": 2,
    "job_id": None,
    "active": False,
    "status": "idle",       # idle | running | rendering | error | stopped
    "video_name": "",
    "video_status": "idle",
    "step": 0,
    "total_steps": 7,
    "step_name": "Hệ thống sẵn sàng",
    "percent": 0,
    "start_time": None,
    "elapsed_seconds": 0,
    "eta_seconds": None,
    "queue_total": 0,
    "queue_index": 0,
    "input_dir": "",
    "output_dir": "",
    "batch_started_at": None,
    "stop_requested": False,
    "last_error": None,
    "last_completed": None,
    "history": [],
    "updated_at": 0,
}

_CURRENT_STATE: Dict[str, Any] = copy.deepcopy(_DEFAULT_STATE)


@contextmanager
def _process_guard():
    """Serialize ownership changes; the OS releases the guard on process exit."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with open(BATCH_LOCK_FILE.with_suffix(".guard"), "a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _ensure_dir():
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is not a harmless existence probe on Windows.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _read_batch_lock_unlocked() -> Dict[str, Any]:
    try:
        with open(BATCH_LOCK_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _claim_batch_lock_unlocked(job_id: str) -> None:
    with _process_guard():
        _claim_batch_lock_guarded(job_id)


def _claim_batch_lock_guarded(job_id: str) -> None:
    _ensure_dir()
    payload = json.dumps(
        {"job_id": job_id, "pid": os.getpid(), "created_at": time.time()},
        ensure_ascii=False,
    ).encode("utf-8")
    for _attempt in range(2):
        try:
            descriptor = os.open(
                BATCH_LOCK_FILE,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError:
            owner = _read_batch_lock_unlocked()
            if not owner:
                raise JobAlreadyRunningError("Batch lock is being written or is unreadable.")
            owner_pid = int(owner.get("pid") or 0)
            if owner_pid and _pid_is_running(owner_pid):
                raise JobAlreadyRunningError(
                    "Một batch khác đang chạy (PID {}).".format(owner_pid)
                )
            try:
                BATCH_LOCK_FILE.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return
    raise JobAlreadyRunningError("Không thể giành quyền chạy batch.")


def _release_batch_lock_unlocked(job_id: Optional[str]) -> None:
    with _process_guard():
        _release_batch_lock_guarded(job_id)


def _release_batch_lock_guarded(job_id: Optional[str]) -> None:
    owner = _read_batch_lock_unlocked()
    if not job_id or not owner or owner.get("job_id") != job_id or owner.get("pid") != os.getpid():
        return
    try:
        BATCH_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _read_state_from_disk_unlocked() -> Optional[Dict[str, Any]]:
    """Read the newest complete state written by this or another process."""
    if not STATUS_FILE.exists():
        return None
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Không thể đọc trạng thái job: %s", exc)
        return None


def _sync_from_disk_unlocked() -> None:
    disk_state = _read_state_from_disk_unlocked()
    if not disk_state:
        return
    disk_updated = float(disk_state.get("updated_at") or 0)
    memory_updated = float(_CURRENT_STATE.get("updated_at") or 0)
    if disk_updated > memory_updated:
        _CURRENT_STATE.update(disk_state)


def _save_state_to_disk():
    _ensure_dir()
    try:
        atomic_write_json(STATUS_FILE, _CURRENT_STATE)
    except OSError as exc:
        # Trạng thái trong bộ nhớ vẫn dùng được; ghi log để lỗi đĩa không bị che giấu.
        logger.warning("Không thể ghi trạng thái job: %s", exc)


def get_status() -> Dict[str, Any]:
    """Lấy trạng thái hiện tại (kèm cập nhật elapsed_seconds nếu đang chạy)."""
    with _LOCK:
        # Batch CLI, Dashboard và Telegram có thể chạy ở các process khác nhau.
        # Luôn nhận bản mới hơn từ đĩa, kể cả khi state trong RAM đang active.
        _sync_from_disk_unlocked()

        state = copy.deepcopy(_CURRENT_STATE)
        owner = _read_batch_lock_unlocked()
        if state.get("active") and owner and not _pid_is_running(int(owner.get("pid") or 0)):
            state.update(active=False, status="interrupted",
                         step_name="Tiến trình đã thoát. Có thể chạy lại để tiếp tục.",
                         eta_seconds=None)
        if state.get("active") and state.get("start_time"):
            elapsed = int(time.time() - state["start_time"])
            state["elapsed_seconds"] = elapsed
            # Ước tính ETA đơn giản theo %
            pct = state.get("percent", 0)
            if pct > 10 and pct < 100:
                total_est = (elapsed / pct) * 100
                state["eta_seconds"] = max(0, int(total_est - elapsed))
            else:
                state["eta_seconds"] = None
        return state


def start_batch(
    total_videos: int,
    input_dir: str = "",
    output_dir: str = "",
    job_id: Optional[str] = None,
) -> str:
    """Đăng ký bắt đầu một hàng đợi batch."""
    with _LOCK:
        _sync_from_disk_unlocked()
        job_id = job_id or uuid.uuid4().hex
        owner = _read_batch_lock_unlocked()
        reserved = owner.get("job_id") == job_id and owner.get("pid") == os.getpid()
        if not reserved:
            _claim_batch_lock_unlocked(job_id)
        now = time.time()
        _CURRENT_STATE.update({
            "schema_version": 2,
            "job_id": job_id,
            "active": True,
            "status": "running",
            "video_name": "",
            "video_status": "idle",
            "step": 0,
            "step_name": "Đang chuẩn bị hàng đợi...",
            "percent": 0,
            "start_time": None,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "queue_total": total_videos,
            "queue_index": 0,
            "input_dir": input_dir,
            "output_dir": output_dir,
            "batch_started_at": now,
            "stop_requested": bool(_CURRENT_STATE.get("stop_requested")) if reserved else False,
            "last_error": None,
            "updated_at": now,
        })
        _save_state_to_disk()
        return job_id


def start_video(video_name: str, index: int = 1, total: int = 1):
    """Bắt đầu xử lý một video cụ thể."""
    with _LOCK:
        _sync_from_disk_unlocked()
        _CURRENT_STATE.update({
            "active": True,
            "status": "running",
            "video_name": video_name,
            "video_status": "running",
            "step": 0,
            "step_name": "Bắt đầu xử lý video...",
            "percent": 5,
            "start_time": time.time(),
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "queue_total": total,
            "queue_index": index,
            "last_error": None,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def update_step(step: float, step_name: str, percent: Optional[int] = None, details: str = ""):
    """Cập nhật bước xử lý và tiến độ %."""
    with _LOCK:
        _sync_from_disk_unlocked()
        if percent is None:
            percent = STEP_PERCENT_MAP.get(float(step), int(step * 15))
        percent = min(99, max(0, percent))

        status = "rendering" if step >= 6.0 else "running"
        if (_CURRENT_STATE.get("step"), _CURRENT_STATE.get("step_name")) != (step, step_name):
            logging.getLogger("pipeline.progress").info(
                "Video %s | Bước %s: %s",
                _CURRENT_STATE.get("video_name", ""), step, step_name)

        _CURRENT_STATE.update({
            "active": True,
            "status": status,
            "step": step,
            "step_name": step_name,
            "percent": percent,
            "details": details,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def finish_video(video_name: str, output_path: str = "", duration_seconds: float = 0):
    """Ghi nhận hoàn thành video."""
    with _LOCK:
        _sync_from_disk_unlocked()
        now = time.time()
        record = {
            "video_name": video_name,
            "output_path": output_path,
            "duration_seconds": int(duration_seconds),
            "completed_at": now,
        }
        history = _CURRENT_STATE.get("history", [])
        history.insert(0, record)
        # Giữ tối đa 20 video gần nhất
        _CURRENT_STATE["history"] = history[:20]

        _CURRENT_STATE.update({
            "percent": 100,
            "step": 6,
            "step_name": f"Hoàn thành xuất sắc: {video_name}",
            "last_completed": record,
            "video_status": "completed",
            "updated_at": now,
        })
        _save_state_to_disk()


def finish_batch():
    """Ghi nhận hoàn thành toàn bộ hàng đợi."""
    with _LOCK:
        _sync_from_disk_unlocked()
        _CURRENT_STATE.update({
            "active": False,
            "status": "idle",
            "queue_index": _CURRENT_STATE.get("queue_total", 0),
            "step_name": "Đã hoàn thành toàn bộ hàng đợi!",
            "percent": 100,
            "start_time": None,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "stop_requested": False,
            "updated_at": time.time(),
        })
        _save_state_to_disk()
        _release_batch_lock_unlocked(_CURRENT_STATE.get("job_id"))


def release_batch(job_id: str) -> None:
    with _LOCK:
        _release_batch_lock_unlocked(job_id)


def set_error(video_name: str, error_msg: str, fatal: bool = True):
    """Ghi nhận lỗi khi xử lý."""
    with _LOCK:
        _sync_from_disk_unlocked()
        _CURRENT_STATE.update({
            "active": not fatal,
            "status": "error" if fatal else "running",
            "video_name": video_name,
            "step_name": f"Lỗi: {error_msg}",
            "last_error": error_msg,
            "video_status": "error",
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def request_stop():
    """Gửi yêu cầu dừng batch."""
    with _LOCK:
        _sync_from_disk_unlocked()
        atomic_write_json(STATUS_FILE.with_name("stop_request.json"),
                          {"job_id": _CURRENT_STATE.get("job_id")})
        _CURRENT_STATE.update({
            "active": True,
            "status": "stopping",
            "stop_requested": True,
            "step_name": "Đang dừng tiến trình theo yêu cầu...",
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def is_stop_requested() -> bool:
    with _LOCK:
        _sync_from_disk_unlocked()
        try:
            command = json.loads(STATUS_FILE.with_name("stop_request.json").read_text(encoding="utf-8"))
            if command.get("job_id") and command["job_id"] == _CURRENT_STATE.get("job_id"):
                return True
        except (OSError, ValueError, TypeError):
            pass
        return bool(_CURRENT_STATE.get("stop_requested"))


def mark_stopped(message: str = "Đã dừng theo yêu cầu") -> None:
    with _LOCK:
        _sync_from_disk_unlocked()
        _CURRENT_STATE.update({
            "active": False,
            "status": "stopped",
            "step_name": message,
            "eta_seconds": None,
            "stop_requested": True,
            "updated_at": time.time(),
        })
        _save_state_to_disk()
        _release_batch_lock_unlocked(_CURRENT_STATE.get("job_id"))


def fail_batch(
    error_msg: str,
    video_name: str = "batch",
    job_id: Optional[str] = None,
) -> None:
    """Record an unrecoverable batch failure and release the cross-process lock."""
    with _LOCK:
        _sync_from_disk_unlocked()
        current_job_id = _CURRENT_STATE.get("job_id")
        if job_id and current_job_id not in (None, job_id):
            logger.warning(
                "Bỏ qua lỗi từ job %s vì job %s đang giữ trạng thái.",
                job_id,
                current_job_id,
            )
            return
        _CURRENT_STATE.update({
            "job_id": job_id or current_job_id,
            "active": False,
            "status": "error",
            "video_name": video_name,
            "step_name": f"Lỗi: {error_msg}",
            "last_error": error_msg,
            "eta_seconds": None,
            "updated_at": time.time(),
        })
        _save_state_to_disk()
        _release_batch_lock_unlocked(job_id or current_job_id)
