"""
v1_separator_lock.py - Cross-process locking mechanism for heavy audio separation on Tool V1.
Ensures strictly 1 heavy separation model (BS-RoFormer or Demucs) runs on the RTX 4050 GPU at a time.
"""

import os
import sys
import time
import logging
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("v1_separator_lock")

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR.parent / "workspace")))
LOCK_FILE = WORKSPACE / "bot_system" / "separator.lock"


@contextmanager
def separator_gpu_lock(timeout_seconds: float = 300.0, poll_interval: float = 0.5):
    """
    Acquire cross-process exclusive lock for GPU audio separation.
    Releases automatically on context exit or process termination.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    handle = None
    acquired = False

    try:
        handle = open(LOCK_FILE, "a+b")
        while time.monotonic() < deadline:
            try:
                if os.name == "nt":
                    import msvcrt
                    # Non-blocking lock attempt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, IOError):
                # Lock held by another process; wait and retry
                time.sleep(poll_interval)

        if not acquired:
            raise TimeoutError(
                f"Không thể chiếm khóa GPU tách âm sau {timeout_seconds:.1f}s. "
                "Có tiến trình tách âm khác đang chạy chiếm dụng GPU."
            )

        logger.info("[GPU_LOCK] Đã chiếm khóa độc quyền tách âm trên GPU.")
        yield

    finally:
        if acquired and handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
                logger.info("[GPU_LOCK] Đã giải phóng khóa độc quyền tách âm trên GPU.")
            except Exception as e:
                logger.debug(f"[GPU_LOCK] Lỗi khi giải phóng khóa: {e}")
        if handle:
            try:
                handle.close()
            except Exception:
                pass
