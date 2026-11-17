"""
v1_gpu_gatekeeper.py - Cổng kiểm soát độc quyền GPU & Điều tiết VRAM cho Tool V1.
Đảm bảo trên card đồ họa RTX 4050 (6GB VRAM):
1. Tại 1 thời điểm CHỈ DUY NHẤT 1 tác vụ AI nặng (RoFormer/Demucs, Whisper, OCR, NVENC) được chiếm dụng GPU.
2. Trước khi cấp quyền GPU: Tự động kiểm tra VRAM khả dụng (VRAM Safety Guard). Nếu VRAM đang bị nghẽn (>85%),
   tiến trình sẽ kiên nhẫn xếp hàng chờ VRAM hạ nhiệt thay vì nhảy vào làm tràn bộ nhớ (CUDA OOM).
3. Sau khi hoàn thành: Tự động giải phóng khóa ngay lập tức và dọn dẹp bộ nhớ đệm (gc.collect).
"""

from __future__ import annotations

import contextvars
import gc
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger("v1_gpu_gatekeeper")


def _check_stop():
    from batch_control import BatchStopRequested, stop_check
    predicate = stop_check.get()
    if predicate and predicate():
        raise BatchStopRequested("Batch stop requested while waiting for GPU")
    # A waiter may be cancelled through the dashboard without cancelling its
    # asyncio task. Only inspect the current execution's cancellation state.
    import job_tracker
    if job_tracker.current_execution.get() and job_tracker.is_stop_requested():
        raise BatchStopRequested("Batch stop requested while waiting for GPU")

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR.parent / "workspace")))
GPU_LOCK_FILE = WORKSPACE / "bot_system" / "v1_gpu_execution.lock"

# ContextVar để nhận diện task/worker hiện tại (tự động kế thừa sang thread khi dùng asyncio.to_thread)
_GPU_CONTEXT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_v1_gpu_context_id", default=None)
_GPU_SCOPE = contextvars.ContextVar("_v1_gpu_scope", default=None)

# Biến theo dõi tái nhập (Re-entrancy) ở cấp tiến trình
_PROCESS_LOCK = threading.RLock()
_CURRENT_HOLDER: Optional[str] = None
_RECURSION_DEPTH: int = 0
_HELD_HANDLE = None


def set_gpu_context_id(cid: str) -> None:
    """Gán định danh ngữ cảnh cho tác vụ hiện tại (Job ID / Worker ID)."""
    _GPU_CONTEXT_ID.set(cid)


def get_gpu_context_id() -> str:
    """Lấy định danh ngữ cảnh hiện tại."""
    import job_tracker
    execution = job_tracker.current_execution.get()
    if execution:
        return "execution_" + str(execution)
    cid = _GPU_CONTEXT_ID.get()
    if not cid:
        try:
            import asyncio
            t = asyncio.current_task()
            if t:
                cid = f"task_{id(t)}"
                _GPU_CONTEXT_ID.set(cid)
                return cid
        except RuntimeError:
            pass
        cid = f"thread_{threading.get_ident()}"
        _GPU_CONTEXT_ID.set(cid)
    return cid


def _task_identity():
    import asyncio
    try:
        task = asyncio.current_task()
        return id(task) if task else None
    except RuntimeError:
        return None


def _enter_scope():
    """Nested calls share a token; sibling asyncio tasks get different tokens."""
    cid, task_id = get_gpu_context_id(), _task_identity()
    scope = _GPU_SCOPE.get()
    if scope is None or scope[0] != cid or (task_id is not None and scope[2] != task_id):
        scope = (cid, uuid.uuid4().hex, task_id)
        return scope, _GPU_SCOPE.set(scope)
    return scope, None


@contextmanager
def gpu_gatekeeper(
    stage_name: str = "gpu_task",
    timeout_seconds: float = 900.0,
    poll_interval: float = 0.5,
    min_free_mb: float = 1800.0,
):
    """
    Khóa độc quyền GPU đa tiến trình & hỗ trợ tái nhập (Re-entrant Mutex).
    - Cùng 1 worker/job lồng nhau: tăng recursion depth, không tự khóa chính mình.
    - Khác worker/job: bắt buộc chờ đối phương giải phóng hoàn toàn GPU.
    """
    scope, scope_token = _enter_scope()
    try:
        with _scoped_gpu_gatekeeper(scope, stage_name, timeout_seconds, poll_interval, min_free_mb):
            yield
    finally:
        if scope_token is not None:
            _GPU_SCOPE.reset(scope_token)


@contextmanager
def _scoped_gpu_gatekeeper(scope, stage_name, timeout_seconds, poll_interval, min_free_mb):
    global _CURRENT_HOLDER, _RECURSION_DEPTH, _HELD_HANDLE

    cid, holder_id, _ = scope
    _check_stop()

    # Kiểm tra xem chính context này đã đang giữ khóa chưa (Re-entrancy)
    with _PROCESS_LOCK:
        if _CURRENT_HOLDER == holder_id and _RECURSION_DEPTH > 0:
            _RECURSION_DEPTH += 1
            logger.debug("[GPU_GATEKEEPER] Tái nhập khóa GPU cho '%s' (depth=%d, cid=%s)", stage_name, _RECURSION_DEPTH, cid)
            reentrant = True
        else:
            reentrant = False

    if reentrant:
        try:
            yield
        finally:
            with _PROCESS_LOCK:
                _RECURSION_DEPTH -= 1
                logger.debug("[GPU_GATEKEEPER] Thoát tái nhập khóa GPU '%s' (depth=%d, cid=%s)", stage_name, _RECURSION_DEPTH, cid)
        return

    # Chưa giữ khóa: Phải chiếm khóa file OS
    GPU_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    handle = None
    acquired = False

    from v1_vram_monitor import get_vram_stats, is_vram_safe_for_gpu_step
    from worker_settings import get_worker_settings

    worker_cfg = get_worker_settings()
    auto_guard = worker_cfg.get("auto_vram_guard", True)

    try:
        handle = open(GPU_LOCK_FILE, "a+b")
        while time.monotonic() < deadline:
            _check_stop()
            # 1. Thử chiếm khóa file OS
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, IOError):
                # Đang có tiến trình/luồng khác giữ GPU, ngủ chờ lượt
                time.sleep(poll_interval)
                continue

            # 2. Đã chiếm khóa file, kiểm tra an toàn VRAM
            if acquired:
                if auto_guard:
                    safe, reason = is_vram_safe_for_gpu_step(min_free_mb=min_free_mb)
                    if not safe:
                        logger.warning(
                            "[GPU_GATEKEEPER] Đang chờ VRAM hạ nhiệt cho stage '%s': %s",
                            stage_name,
                            reason,
                        )
                        # Tạm nhả khóa và ngủ một nhịp ngắn để nhường chỗ
                        if os.name == "nt":
                            import msvcrt
                            try:
                                handle.seek(0)
                                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                            except Exception:
                                pass
                        else:
                            import fcntl
                            try:
                                fcntl.flock(handle, fcntl.LOCK_UN)
                            except Exception:
                                pass
                        acquired = False
                        time.sleep(1.0)
                        continue

                # VRAM an toàn và đã giữ khóa độc quyền
                break

        if not acquired:
            v_stats = get_vram_stats()
            raise TimeoutError(
                f"[GPU_GATEKEEPER] Không thể chiếm quyền GPU cho '{stage_name}' sau {timeout_seconds:.1f}s. "
                f"VRAM hiện tại: {v_stats.get('used_gb', 0)}GB/{v_stats.get('total_gb', 6)}GB ({v_stats.get('percent', 0)}%). "
                "Có tiến trình AI khác đang chiếm dụng GPU hoặc VRAM bị quá tải."
            )

        # Cancellation can arrive during the telemetry probe after acquisition.
        # Close the acquired handle without handing GPU work to that caller.
        _check_stop()
        with _PROCESS_LOCK:
            _CURRENT_HOLDER = holder_id
            _RECURSION_DEPTH = 1
            _HELD_HANDLE = handle

        logger.info("[GPU_GATEKEEPER] >>> ĐÃ CẤP QUYỀN GPU cho stage: '%s' (holder=%s)", stage_name, cid)
        yield

    finally:
        with _PROCESS_LOCK:
            if _CURRENT_HOLDER == holder_id:
                _RECURSION_DEPTH -= 1
                if _RECURSION_DEPTH <= 0:
                    _RECURSION_DEPTH = 0
                    _CURRENT_HOLDER = None
                    cur_h = _HELD_HANDLE or handle
                    _HELD_HANDLE = None
                    if cur_h:
                        try:
                            if os.name == "nt":
                                import msvcrt
                                cur_h.seek(0)
                                msvcrt.locking(cur_h.fileno(), msvcrt.LK_UNLCK, 1)
                            else:
                                import fcntl
                                fcntl.flock(cur_h, fcntl.LOCK_UN)
                        except Exception as e:
                            logger.debug("Lỗi khi nhả GPU lock: %s", e)
                        finally:
                            try:
                                cur_h.close()
                            except Exception:
                                pass
                    logger.info("[GPU_GATEKEEPER] <<< ĐÃ GIẢI PHÓNG QUYỀN GPU từ stage: '%s' (holder=%s)", stage_name, cid)
                    # Dọn dẹp rác bộ nhớ sau khi hoàn tất bước GPU
                    try:
                        gc.collect()
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
        # Timeout/cancellation before holder registration still owns an open
        # handle (and may own the OS byte lock). Closing it always releases it.
        if handle is not None and not handle.closed:
            handle.close()


@asynccontextmanager
async def async_gpu_gatekeeper(
    stage_name: str = "gpu_task",
    timeout_seconds: float = 900.0,
    min_free_mb: float = 1800.0,
):
    """Phiên bản Async của GPU Gatekeeper, không chặn event loop trong lúc xếp hàng chờ."""
    import asyncio
    from batch_control import stop_check, drain_task, to_thread_drained
    _, scope_token = _enter_scope()
    cancelled = threading.Event()
    inherited = stop_check.get()
    token = stop_check.set(lambda: cancelled.is_set() or bool(inherited and inherited()))
    manager = gpu_gatekeeper(stage_name, timeout_seconds, min_free_mb=min_free_mb)
    entered = False
    async def enter():
        nonlocal entered
        await to_thread_drained(manager.__enter__)
        entered = True
    task = asyncio.create_task(enter())
    try:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            await drain_task(task)
            raise
        yield
    finally:
        try:
            if entered:
                await to_thread_drained(manager.__exit__, None, None, None)
        finally:
            # Drained cleanup still re-raises cancellation. Restore the caller's
            # context even when another cancellation arrives during __exit__.
            stop_check.reset(token)
            if scope_token is not None:
                _GPU_SCOPE.reset(scope_token)

