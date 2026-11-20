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
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger("v1_gpu_gatekeeper")

ROOT_DIR = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT_DIR.parent / "workspace")))
GPU_LOCK_FILE = WORKSPACE / "bot_system" / "v1_gpu_execution.lock"

# ContextVar để nhận diện task/worker hiện tại (tự động kế thừa sang thread khi dùng asyncio.to_thread)
_GPU_CONTEXT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("_v1_gpu_context_id", default=None)

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


def _check_gpu_stop(check_stop=None):
    """Use the job callback before stage runtime exists, plus any inherited scope."""
    from batch_control import stop_check
    if check_stop is not None and check_stop():
        raise RuntimeError("Batch stop requested while waiting for GPU")
    predicate = stop_check.get()
    if predicate is not None and predicate():
        raise RuntimeError("Batch stop requested while waiting for GPU")


def _release_gpu_context(cid, stage_name, handle=None):
    """The last same-context lease releases the file, regardless of exit order."""
    global _CURRENT_HOLDER, _RECURSION_DEPTH, _HELD_HANDLE
    release_owner = False
    with _PROCESS_LOCK:
        if _CURRENT_HOLDER == cid and (handle is None or handle is _HELD_HANDLE):
            _RECURSION_DEPTH -= 1
            if _RECURSION_DEPTH > 0:
                return
            _RECURSION_DEPTH = 0
            _CURRENT_HOLDER = None
            cur_h = _HELD_HANDLE or handle
            _HELD_HANDLE = None
            release_owner = True
    if not release_owner:
        # An acquisition may stop or time out before registering its owner.
        if handle is not None:
            handle.close()
        return

    # Keep the OS lease through local cleanup, but leave admission's process
    # mutex free so queued callers can observe stop and drain their attempts.
    try:
        gc.collect()
        torch = _sys.modules.get("torch")
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
    except Exception:
        pass
    finally:
        if cur_h:
            try:
                if os.name == "nt":
                    import msvcrt
                    cur_h.seek(0)
                    msvcrt.locking(cur_h.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(cur_h, fcntl.LOCK_UN)
            except Exception as error:
                logger.debug("Lỗi khi nhả GPU lock: %s", error)
            finally:
                try:
                    cur_h.close()
                except Exception:
                    pass
        logger.info("[GPU_GATEKEEPER] <<< ĐÃ GIẢI PHÓNG QUYỀN GPU từ stage: '%s' (holder=%s)", stage_name, cid)


@contextmanager
def gpu_gatekeeper(
    stage_name: str = "gpu_task",
    timeout_seconds: float = 900.0,
    poll_interval: float = 0.5,
    min_free_mb: float = 1800.0,
    check_stop=None,
):
    """
    Khóa độc quyền GPU đa tiến trình & hỗ trợ tái nhập (Re-entrant Mutex).
    - Cùng 1 worker/job lồng nhau: tăng recursion depth, không tự khóa chính mình.
    - Khác worker/job: bắt buộc chờ đối phương giải phóng hoàn toàn GPU.
    """
    global _CURRENT_HOLDER, _RECURSION_DEPTH, _HELD_HANDLE

    _check_gpu_stop(check_stop)
    cid = get_gpu_context_id()

    # Kiểm tra xem chính context này đã đang giữ khóa chưa (Re-entrancy)
    with _PROCESS_LOCK:
        if _CURRENT_HOLDER == cid and _RECURSION_DEPTH > 0:
            _RECURSION_DEPTH += 1
            logger.debug("[GPU_GATEKEEPER] Tái nhập khóa GPU cho '%s' (depth=%d, cid=%s)", stage_name, _RECURSION_DEPTH, cid)
            reentrant = True
        else:
            reentrant = False

    if reentrant:
        try:
            yield
        finally:
            _release_gpu_context(cid, stage_name)
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
            _check_gpu_stop(check_stop)
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

        _check_gpu_stop(check_stop)
        if not acquired:
            v_stats = get_vram_stats()
            raise TimeoutError(
                f"[GPU_GATEKEEPER] Không thể chiếm quyền GPU cho '{stage_name}' sau {timeout_seconds:.1f}s. "
                f"VRAM hiện tại: {v_stats.get('used_gb', 0)}GB/{v_stats.get('total_gb', 6)}GB ({v_stats.get('percent', 0)}%). "
                "Có tiến trình AI khác đang chiếm dụng GPU hoặc VRAM bị quá tải."
            )

        with _PROCESS_LOCK:
            _CURRENT_HOLDER = cid
            _RECURSION_DEPTH = 1
            _HELD_HANDLE = handle

        logger.info("[GPU_GATEKEEPER] >>> ĐÃ CẤP QUYỀN GPU cho stage: '%s' (holder=%s)", stage_name, cid)
        yield

    finally:
        _release_gpu_context(cid, stage_name, handle)


@asynccontextmanager
async def async_gpu_gatekeeper(
    stage_name: str = "gpu_task",
    timeout_seconds: float = 900.0,
    min_free_mb: float = 1800.0,
    check_stop=None,
):
    """Phiên bản Async của GPU Gatekeeper, không chặn event loop trong lúc xếp hàng chờ."""
    import asyncio
    _check_gpu_stop(check_stop)
    get_gpu_context_id()  # Establish the identity before copying context to a thread.
    deadline = time.monotonic() + timeout_seconds
    acquisition_stopped = threading.Event()

    async def drain(task):
        # Repeated cancellation must not cancel a to_thread wrapper whose worker
        # can still acquire the file lock after the HTTP task has disappeared.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        return task.result()

    async def release(manager, exc_type=None, exc=None, traceback=None):
        task = asyncio.create_task(asyncio.to_thread(manager.__exit__, exc_type, exc, traceback))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # A cancelled caller must not leave the file lease held.
            await drain(task)
            raise

    while time.monotonic() < deadline:
        _check_gpu_stop(check_stop)
        manager = gpu_gatekeeper(
            stage_name=stage_name,
            timeout_seconds=min(1.5, max(0.001, deadline - time.monotonic())),
            min_free_mb=min_free_mb,
            check_stop=acquisition_stopped.is_set,
        )
        acquisition = asyncio.create_task(asyncio.to_thread(manager.__enter__))
        try:
            while not acquisition.done():
                _check_gpu_stop(check_stop)
                await asyncio.wait({acquisition}, timeout=0.1)
        except BaseException:
            # Callback stops and repeated async cancellation must drain a late
            # acquisition before returning, just like running native work.
            acquisition_stopped.set()
            try:
                await drain(acquisition)
            except Exception:
                pass
            else:
                await release(manager)
            raise
        try:
            acquisition.result()
        except TimeoutError:
            await asyncio.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
            continue

        # Body exceptions are not acquisition failures: never retry the stage or
        # yield a second time when inference raises TimeoutError.
        try:
            _check_gpu_stop(check_stop)
            yield
        except BaseException as error:
            if not await release(manager, type(error), error, error.__traceback__):
                raise
        else:
            await release(manager)
        return

    _check_gpu_stop(check_stop)
    raise TimeoutError(
        f"[GPU_GATEKEEPER] Hết thời gian chờ ({timeout_seconds}s) để lấy quyền GPU cho '{stage_name}'."
    )


import sys as _sys  # noqa: E402
_sys.modules.setdefault("v1_gpu_gatekeeper", _sys.modules[__name__])
_sys.modules.setdefault("backend.v1_gpu_gatekeeper", _sys.modules[__name__])

