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
from pathlib import Path
from typing import Optional, Dict, Any

from pipeline_v2.atomic_io import atomic_write_json

BASE_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(BASE_DIR.parent / "workspace")))
STATUS_FILE = WORKSPACE / "job_status.json"

_LOCK = threading.Lock()
logger = logging.getLogger(__name__)

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
    "step": 0,
    "total_steps": 6,
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
    "updated_at": time.time(),
}

_CURRENT_STATE: Dict[str, Any] = dict(_DEFAULT_STATE)


def _ensure_dir():
    try:
        WORKSPACE.mkdir(parents=True, exist_ok=True)
    except Exception:
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

        state = dict(_CURRENT_STATE)
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
        now = time.time()
        _CURRENT_STATE.update({
            "schema_version": 2,
            "job_id": job_id,
            "active": True,
            "status": "running",
            "queue_total": total_videos,
            "queue_index": 0,
            "input_dir": input_dir,
            "output_dir": output_dir,
            "batch_started_at": now,
            "stop_requested": False,
            "last_error": None,
            "updated_at": now,
        })
        _save_state_to_disk()
        return job_id


def start_video(video_name: str, index: int = 1, total: int = 1):
    """Bắt đầu xử lý một video cụ thể."""
    with _LOCK:
        _CURRENT_STATE.update({
            "active": True,
            "status": "running",
            "video_name": video_name,
            "step": 1,
            "step_name": "Bắt đầu xử lý video...",
            "percent": 5,
            "start_time": time.time(),
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "queue_total": total,
            "queue_index": index,
            "last_error": None,
            "stop_requested": False,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def update_step(step: float, step_name: str, percent: Optional[int] = None, details: str = ""):
    """Cập nhật bước xử lý và tiến độ %."""
    with _LOCK:
        if percent is None:
            percent = STEP_PERCENT_MAP.get(float(step), int(step * 15))
        percent = min(99, max(0, percent))

        status = "rendering" if step >= 6.0 else "running"

        _CURRENT_STATE.update({
            "active": True,
            "status": status,
            "step": step,
            "step_name": step_name,
            "percent": percent,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def finish_video(video_name: str, output_path: str = "", duration_seconds: float = 0):
    """Ghi nhận hoàn thành video."""
    with _LOCK:
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
            "updated_at": now,
        })
        _save_state_to_disk()


def finish_batch():
    """Ghi nhận hoàn thành toàn bộ hàng đợi."""
    with _LOCK:
        _CURRENT_STATE.update({
            "active": False,
            "status": "idle",
            "video_name": "",
            "step": 0,
            "step_name": "Đã hoàn thành toàn bộ hàng đợi!",
            "percent": 100,
            "start_time": None,
            "elapsed_seconds": 0,
            "eta_seconds": None,
            "stop_requested": False,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def set_error(video_name: str, error_msg: str):
    """Ghi nhận lỗi khi xử lý."""
    with _LOCK:
        _CURRENT_STATE.update({
            "active": False,
            "status": "error",
            "video_name": video_name,
            "step_name": f"Lỗi: {error_msg}",
            "last_error": error_msg,
            "stop_requested": False,
            "updated_at": time.time(),
        })
        _save_state_to_disk()


def request_stop():
    """Gửi yêu cầu dừng batch."""
    with _LOCK:
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
        return bool(_CURRENT_STATE.get("stop_requested"))


def mark_stopped(message: str = "Đã dừng theo yêu cầu") -> None:
    with _LOCK:
        _CURRENT_STATE.update({
            "active": False,
            "status": "stopped",
            "step_name": message,
            "eta_seconds": None,
            "stop_requested": True,
            "updated_at": time.time(),
        })
        _save_state_to_disk()
