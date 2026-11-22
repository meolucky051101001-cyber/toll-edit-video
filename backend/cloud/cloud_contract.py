"""
cloud_contract.py - Phase 0 canonical job contract, state machine, and unified admission point.

Defines:
- CloudJobState & ALLOWED_TRANSITIONS + normalize_state / to_legacy_state helpers
- CloudErrorCode
- CloudJobOptionsSnapshot, CloudSessionOwnership, CloudOutputManifest, CloudJobContract
- build_input_fingerprint
- Unified admit_and_enqueue_job(...) used by both REST routes and Telegram handlers
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .cloud_config import DEFAULT_CONFIG
from .cloud_security import (
    CloudValidationError,
    sanitize_remote_filename,
    validate_task_type,
    validate_video_gpu_type,
    validate_video_input,
    validate_voice,
)


class CloudJobState(str, Enum):
    """Canonical lifecycle states for Cloud GPU jobs."""

    QUEUED = "queued"
    PROVISIONING = "provisioning"
    UPLOADING = "uploading"
    PREPARING = "preparing"
    PROCESSING = "processing"
    DOWNLOADING = "downloading"
    VALIDATING = "validating"
    COMPLETED = "completed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FAILED = "failed"


TERMINAL_STATES: frozenset[CloudJobState] = frozenset(
    {CloudJobState.COMPLETED, CloudJobState.CANCELLED, CloudJobState.FAILED}
)

ACTIVE_STATES: frozenset[CloudJobState] = frozenset(
    {
        CloudJobState.PROVISIONING,
        CloudJobState.UPLOADING,
        CloudJobState.PREPARING,
        CloudJobState.PROCESSING,
        CloudJobState.DOWNLOADING,
        CloudJobState.VALIDATING,
        CloudJobState.CANCEL_REQUESTED,
        CloudJobState.CANCELLING,
    }
)

CANCELLABLE_STATES: frozenset[CloudJobState] = frozenset(
    {
        CloudJobState.QUEUED,
        CloudJobState.PROVISIONING,
        CloudJobState.UPLOADING,
        CloudJobState.PREPARING,
        CloudJobState.PROCESSING,
        CloudJobState.DOWNLOADING,
        CloudJobState.VALIDATING,
    }
)

ALLOWED_TRANSITIONS: Dict[CloudJobState, frozenset[CloudJobState]] = {
    CloudJobState.QUEUED: frozenset(
        {
            CloudJobState.PROVISIONING,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.CANCELLED,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.PROVISIONING: frozenset(
        {
            CloudJobState.UPLOADING,
            CloudJobState.PREPARING,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.UPLOADING: frozenset(
        {
            CloudJobState.PREPARING,
            CloudJobState.PROCESSING,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.PREPARING: frozenset(
        {
            CloudJobState.PROCESSING,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.PROCESSING: frozenset(
        {
            CloudJobState.DOWNLOADING,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.DOWNLOADING: frozenset(
        {
            CloudJobState.VALIDATING,
            CloudJobState.COMPLETED,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.VALIDATING: frozenset(
        {
            CloudJobState.COMPLETED,
            CloudJobState.CANCEL_REQUESTED,
            CloudJobState.CANCELLING,
            CloudJobState.FAILED,
        }
    ),
    CloudJobState.CANCEL_REQUESTED: frozenset(
        {CloudJobState.CANCELLING, CloudJobState.CANCELLED, CloudJobState.FAILED}
    ),
    CloudJobState.CANCELLING: frozenset(
        {CloudJobState.CANCELLED, CloudJobState.FAILED}
    ),
    CloudJobState.COMPLETED: frozenset(),
    CloudJobState.CANCELLED: frozenset(),
    CloudJobState.FAILED: frozenset(),
}

_LEGACY_TO_CANONICAL: Dict[str, CloudJobState] = {
    "queued": CloudJobState.QUEUED,
    "creating_session": CloudJobState.PROVISIONING,
    "provisioning": CloudJobState.PROVISIONING,
    "uploading": CloudJobState.UPLOADING,
    "preparing": CloudJobState.PREPARING,
    "processing": CloudJobState.PROCESSING,
    "downloading": CloudJobState.DOWNLOADING,
    "validating": CloudJobState.VALIDATING,
    "completed": CloudJobState.COMPLETED,
    "cancel_requested": CloudJobState.CANCEL_REQUESTED,
    "cancelling": CloudJobState.CANCELLING,
    "stopped": CloudJobState.CANCELLED,
    "cancelled": CloudJobState.CANCELLED,
    "error": CloudJobState.FAILED,
    "failed": CloudJobState.FAILED,
}

_CANONICAL_TO_LEGACY: Dict[CloudJobState, str] = {
    CloudJobState.QUEUED: "queued",
    CloudJobState.PROVISIONING: "creating_session",
    CloudJobState.UPLOADING: "uploading",
    CloudJobState.PREPARING: "processing",
    CloudJobState.PROCESSING: "processing",
    CloudJobState.DOWNLOADING: "downloading",
    CloudJobState.VALIDATING: "downloading",
    CloudJobState.COMPLETED: "completed",
    CloudJobState.CANCEL_REQUESTED: "stopped",
    CloudJobState.CANCELLING: "stopped",
    CloudJobState.CANCELLED: "stopped",
    CloudJobState.FAILED: "error",
}


class CloudErrorCode(str, Enum):
    """Structured error codes for Cloud job failures."""

    INVALID_INPUT = "INVALID_INPUT"
    INVALID_PATH = "INVALID_PATH"
    UNSUPPORTED_GPU = "UNSUPPORTED_GPU"
    UNSUPPORTED_TASK = "UNSUPPORTED_TASK"
    UNSUPPORTED_VOICE = "UNSUPPORTED_VOICE"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    BUDGET_UNCERTAIN = "BUDGET_UNCERTAIN"
    CONFLICT_ALREADY_CLAIMED = "CONFLICT_ALREADY_CLAIMED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    SESSION_CREATE_FAILED = "SESSION_CREATE_FAILED"
    GPU_ALLOCATION_REFUSED = "GPU_ALLOCATION_REFUSED"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    EXEC_FAILED = "EXEC_FAILED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    CANCELLED_BY_USER = "CANCELLED_BY_USER"
    CANCELLED = "CANCELLED_BY_USER"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    INSUFFICIENT_CU = "INSUFFICIENT_CU"
    PROVIDER_503 = "PROVIDER_503"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    ENV_SETUP_FAILED = "ENV_SETUP_FAILED"
    ENV_NETWORK_ERROR = "ENV_NETWORK_ERROR"
    ENV_PACKAGE_MISSING = "ENV_PACKAGE_MISSING"
    ENV_GPU_MISMATCH = "ENV_GPU_MISMATCH"
    ENV_DISK_INSUFFICIENT = "ENV_DISK_INSUFFICIENT"
    ENV_MODEL_LOAD_FAILED = "ENV_MODEL_LOAD_FAILED"
    UPLOAD_VERIFICATION_FAILED = "UPLOAD_VERIFICATION_FAILED"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    TTS_GENERATION_FAILED = "TTS_GENERATION_FAILED"
    NO_SPEECH_DETECTED = "NO_SPEECH_DETECTED"
    SUBTITLE_BUILD_FAILED = "SUBTITLE_BUILD_FAILED"
    SUBTITLE_COVERAGE_GAP = "SUBTITLE_COVERAGE_GAP"
    RENDER_FAILED = "RENDER_FAILED"
    QC_FAILED = "QC_FAILED"
    ROLLOUT_PAUSED = "ROLLOUT_PAUSED"
    ROLLOUT_BUSY = "ROLLOUT_BUSY"
    ASR_TIMING_INVALID = "ASR_TIMING_INVALID"
    EXPORT_FAILED = "EXPORT_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


GPU_CHECK_TASK = "gpu_check"
CHECKABLE_GPUS = ("T4", "L4", "A100", "H100")
DASHBOARD_GPU_SETTING = "dashboard_default_gpu"

# Reference hourly CU estimates only (NOT confirmed provider billing rates).
GPU_CU_RATES: Dict[str, float] = {
    "T4": 1.15,
    "L4": 1.94,
    "A100": 3.85,
    "H100": 8.00,
    "G4": 1.50,
    "V5E1": 2.00,
    "V6E1": 3.50,
    "CPU": 0.10,
}


class CloudCostMeasurementSource(str, Enum):
    """How CU usage in a ledger entry was measured or estimated."""

    RUNTIME_RATE_ESTIMATE = "runtime_rate_estimate"
    PROVIDER_RATE_ESTIMATE = "provider_rate_estimate"
    PROVIDER_BALANCE_DELTA = "provider_balance_delta"
    MANUAL_OVERRIDE = "manual_override"
    UNVERIFIED_ESTIMATE = "unverified_estimate"


class CloudLedgerStatus(str, Enum):
    """Lifecycle states of a budget reservation / cost ledger entry."""

    RESERVED = "reserved"
    ACTIVE = "active"
    PENDING_CLEANUP = "pending_cleanup"
    RECONCILED = "reconciled"
    UNRECONCILED = "unreconciled"
    RELEASED = "released"


class CloudCleanupStatus(str, Enum):
    """Verification status for remote GPU session teardown."""

    NONE = "none"
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED_RETRY_NEEDED = "failed_retry_needed"
    PROVIDER_UNREACHABLE = "provider_unreachable"


ERROR_REMEDIATION_MAP: Dict[str, str] = {
    CloudErrorCode.INVALID_INPUT.value: "Kiểm tra lại đường dẫn video, URL, loại GPU hoặc tuỳ chọn đầu vào.",
    CloudErrorCode.INVALID_PATH.value: "Đảm bảo file video tồn tại, là file hợp lệ và nằm trong thư mục được phép.",
    CloudErrorCode.UNSUPPORTED_GPU.value: "Chọn loại GPU được hỗ trợ (ví dụ: T4, L4, A100, H100).",
    CloudErrorCode.UNSUPPORTED_TASK.value: "Chỉ chọn tác vụ hợp lệ (full_pipeline hoặc asr_only).",
    CloudErrorCode.UNSUPPORTED_VOICE.value: "Chọn giọng đọc CapCut nằm trong danh sách được phép.",
    CloudErrorCode.BUDGET_EXCEEDED.value: "Đã chạm hạn mức CU ngày. Tăng hạn mức CU hoặc chờ sang ngày mới.",
    CloudErrorCode.BUDGET_UNCERTAIN.value: "Chưa xác minh được số dư hoặc phiên đang mở trên Colab. Kiểm tra kết nối và các session treo trước khi gửi job mới.",
    CloudErrorCode.CONFLICT_ALREADY_CLAIMED.value: "Job đã được worker khác tiếp nhận. Làm mới hàng đợi để xem tiến độ.",
    CloudErrorCode.PROVIDER_UNAVAILABLE.value: "Không kết nối được Google Colab CLI/WSL. Kiểm tra đăng nhập Colab và mạng rồi thử lại.",
    CloudErrorCode.SESSION_CREATE_FAILED.value: "Không khởi tạo được máy ảo Colab (có thể hết quota GPU). Thử đổi GPU khác (T4/L4) hoặc thử lại sau.",
    CloudErrorCode.UPLOAD_FAILED.value: "Upload video lên Colab thất bại. Kiểm tra kết nối mạng và dung lượng trống trên runtime.",
    CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value: "Checksum file upload lên Colab không khớp. Gửi lại job để upload lại.",
    CloudErrorCode.EXEC_FAILED.value: "Pipeline trên Colab gặp lỗi thực thi. Xem thông báo bước lỗi trước khi chạy lại.",
    CloudErrorCode.DOWNLOAD_FAILED.value: "Tải kết quả từ Colab về máy thất bại. Kiểm tra mạng và dung lượng ổ đĩa local.",
    CloudErrorCode.VALIDATION_FAILED.value: "Video tải về không đạt kiểm tra hợp lệ (hỏng stream hoặc sai thời lượng). Kiểm tra log render.",
    CloudErrorCode.CANCELLED_BY_USER.value: "Công việc đã được huỷ theo yêu cầu người dùng.",
    CloudErrorCode.LEASE_EXPIRED.value: "Mất heartbeat từ worker khi đang chạy. Kiểm tra và thu hồi session cũ nếu còn mở trước khi thử lại.",
    CloudErrorCode.INSUFFICIENT_CU.value: "Số dư Compute Units Colab không đủ. Nạp thêm CU hoặc chạy ở chế độ Local.",
    CloudErrorCode.PROVIDER_503.value: "Google Colab tạm quá tải (503). Chờ ít phút rồi thử lại hoặc đổi loại GPU.",
    CloudErrorCode.CLEANUP_FAILED.value: "Chưa thu hồi được máy ảo Colab. Bấm Thu Hồi Mọi Máy Colab trên Dashboard hoặc kiểm tra bằng tay để tránh hao CU.",
    CloudErrorCode.ENV_SETUP_FAILED.value: "Cài đặt môi trường GPU thất bại. Kiểm tra lockfile gpu_scripts/requirements_cloud.txt.",
    CloudErrorCode.ENV_NETWORK_ERROR.value: "Máy ảo Colab gặp lỗi mạng khi tải gói hoặc model. Thử lại sau.",
    CloudErrorCode.ENV_PACKAGE_MISSING.value: "Thiếu thư viện bắt buộc trên GPU runtime. Kiểm tra log bước kiểm tra môi trường.",
    CloudErrorCode.ENV_GPU_MISMATCH.value: "Máy ảo Colab không có GPU CUDA phù hợp. Đổi loại GPU hoặc tạo session mới.",
    CloudErrorCode.ENV_DISK_INSUFFICIENT.value: "Ổ đĩa trống trên máy ảo Colab không đủ. Thử video ngắn hơn hoặc runtime dung lượng cao hơn.",
    CloudErrorCode.ENV_MODEL_LOAD_FAILED.value: "Không nạp được model Whisper/Demucs/LLM. Kiểm tra VRAM của GPU đã chọn.",
    CloudErrorCode.TRANSLATION_FAILED.value: "Dịch phụ đề AI bị thiếu hoặc lỗi. Kiểm tra API key dịch thuật và thử lại.",
    CloudErrorCode.TTS_GENERATION_FAILED.value: "Tạo giọng đọc CapCut TTS thất bại. Kiểm tra kết nối CapCut TTS và mã giọng.",
    CloudErrorCode.NO_SPEECH_DETECTED.value: "Video không phát hiện được thoại hoặc phụ đề rỗng. Nếu muốn giữ nhạc nền, bật tuỳ chọn allow_no_speech.",
    CloudErrorCode.SUBTITLE_BUILD_FAILED.value: "Không tạo được file phụ đề .ass hợp lệ từ bản dịch.",
    CloudErrorCode.SUBTITLE_COVERAGE_GAP.value: "Phụ đề gốc đổi vị trí theo cảnh và chưa được che đủ. Job bị chặn để tránh lộ chữ Trung; thử lại hoặc báo đội kỹ thuật.",
    CloudErrorCode.RENDER_FAILED.value: "FFmpeg render video đầu ra thất bại. Kiểm tra log render trên Colab.",
    CloudErrorCode.QC_FAILED.value: "Thành phẩm không vượt qua cổng QC (thiếu hình, mất tiếng, sai thời lượng hoặc màn hình đen/im lặng).",
    CloudErrorCode.ASR_TIMING_INVALID.value: "Mốc ASR không khớp thời lượng video nguồn. Kiểm tra transcript và mô hình nhận dạng; không tăng thời lượng nguồn hoặc bỏ câu để vượt QC.",
    CloudErrorCode.EXPORT_FAILED.value: "Xuất file ra thư mục đích thất bại. Kiểm tra dung lượng ổ đĩa đích và quyền ghi file.",
    CloudErrorCode.INTERNAL_ERROR.value: "Lỗi nội bộ khi điều phối job Cloud. Kiểm tra log backend.",
}


def get_error_remediation(error_code: Optional[str | CloudErrorCode]) -> Optional[str]:
    """Map a CloudErrorCode (or cleanup failure code) to actionable Vietnamese guidance."""
    if not error_code:
        return None
    raw = error_code.value if isinstance(error_code, CloudErrorCode) else str(error_code).strip()
    if not raw:
        return None
    if raw in ERROR_REMEDIATION_MAP:
        return ERROR_REMEDIATION_MAP[raw]
    if raw.upper().startswith("CLEANUP_"):
        return ERROR_REMEDIATION_MAP[CloudErrorCode.CLEANUP_FAILED.value]
    return "Kiểm tra log chi tiết của job và trạng thái session Colab trước khi thử lại."


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


def validate_idempotency_key(raw_key: Any) -> Optional[str]:
    """Validate optional idempotency key for retry-safe enqueue operations."""
    if raw_key is None:
        return None
    if not isinstance(raw_key, str):
        raise CloudValidationError(
            "idempotency_key phải là chuỗi ký tự hợp lệ.",
            code=CloudErrorCode.INVALID_INPUT.value,
            http_status=400,
        )
    key = raw_key.strip()
    if not key:
        return None
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise CloudValidationError(
            "idempotency_key chỉ được chứa 1-128 ký tự ASCII (A-Z, a-z, 0-9, _ . : / -).",
            code=CloudErrorCode.INVALID_INPUT.value,
            http_status=400,
        )
    return key


_PROVIDER_PROBE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="CloudProviderProbe",
)
_PROVIDER_CONTROL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="CloudProviderControl",
)
_PROVIDER_CACHE_LOCK = threading.Lock()
_DEFAULT_USAGE_FALLBACK: Dict[str, Any] = {
    "ok": False,
    "balance": 0.0,
    "usage_rate": 0.0,
    "active_assignments": 0,
    "source": "uninitialized",
}
_PROVIDER_CACHE: Dict[str, Any] = {
    "usage": dict(_DEFAULT_USAGE_FALLBACK),
    "sessions": [],
    "usage_fetched_at": 0.0,
    "sessions_fetched_at": 0.0,
    "last_success_at": 0.0,
    "last_attempt_at": 0.0,
    "last_error": None,
    "usage_future": None,
    "sessions_future": None,
    "usage_fn": None,
    "sessions_fn": None,
}


def reset_provider_status_cache() -> None:
    """Reset in-memory provider status TTL cache and single-flight handles (mainly for tests)."""
    with _PROVIDER_CACHE_LOCK:
        _PROVIDER_CACHE["usage"] = dict(_DEFAULT_USAGE_FALLBACK)
        _PROVIDER_CACHE["sessions"] = []
        _PROVIDER_CACHE["usage_fetched_at"] = 0.0
        _PROVIDER_CACHE["sessions_fetched_at"] = 0.0
        _PROVIDER_CACHE["last_success_at"] = 0.0
        _PROVIDER_CACHE["last_attempt_at"] = 0.0
        _PROVIDER_CACHE["last_error"] = None
        _PROVIDER_CACHE["usage_future"] = None
        _PROVIDER_CACHE["sessions_future"] = None
        _PROVIDER_CACHE["usage_fn"] = None
        _PROVIDER_CACHE["sessions_fn"] = None


def _resolve_patched_colab_attr(attr_name: str) -> Any:
    """Resolve colab_bridge attribute while supporting both `cloud.*` and `backend.cloud.*` imports."""
    from . import colab_bridge as rel_mod

    rel_fn = getattr(rel_mod, attr_name)
    alt_name = (
        "backend.cloud.colab_bridge"
        if rel_mod.__name__ == "cloud.colab_bridge"
        else "cloud.colab_bridge"
    )
    alt_mod = sys.modules.get(alt_name)
    if alt_mod is not None:
        alt_fn = getattr(alt_mod, attr_name, None)
        if alt_fn is not None and alt_fn is not rel_fn:
            from unittest.mock import Base as _MockBase

            if isinstance(alt_fn, _MockBase) or hasattr(alt_fn, "assert_called"):
                return alt_fn
    return rel_fn


def _default_usage_callable() -> Callable[[], Dict[str, Any]]:
    return _resolve_patched_colab_attr("get_usage")


def _default_sessions_callable() -> Callable[[], Any]:
    return _resolve_patched_colab_attr("get_sessions")


def stamp_provider_usage_observation(usage: Dict[str, Any], *, observed_at: float) -> Dict[str, Any]:
    """Attach the acquisition time to a newly queried usage payload with no provider timestamp."""
    sample = dict(usage)
    if any(sample.get(key) not in (None, "") for key in ("observed_at", "as_of", "timestamp")):
        return sample
    try:
        age = float(sample.get("age_seconds") or 0.0)
    except (TypeError, ValueError):
        return sample  # Preserve invalid age for the store's fail-closed validation.
    if not math.isfinite(age):
        return sample
    sample["observed_at"] = observed_at - max(0.0, age)
    return sample


def _run_usage_probe_task(fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        raw = fn()
        if not isinstance(raw, dict):
            res: Dict[str, Any] = {"ok": False, "error": "Invalid usage payload"}
        else:
            res = stamp_provider_usage_observation(raw, observed_at=time.time())
    except Exception as exc:
        res = {"ok": False, "error": str(exc)}

    done_at = time.time()
    with _PROVIDER_CACHE_LOCK:
        _PROVIDER_CACHE["last_attempt_at"] = done_at
        if res.get("ok"):
            _PROVIDER_CACHE["usage"] = dict(res)
            _PROVIDER_CACHE["usage_fetched_at"] = done_at
            _PROVIDER_CACHE["last_success_at"] = done_at
            _PROVIDER_CACHE["last_error"] = None
        else:
            err_msg = str(res.get("error") or "Provider usage unavailable")
            _PROVIDER_CACHE["last_error"] = err_msg
            if _PROVIDER_CACHE["last_success_at"] <= 0:
                fallback = dict(_DEFAULT_USAGE_FALLBACK)
                fallback.update({k: v for k, v in res.items() if k in ("balance", "usage_rate", "active_assignments")})
                fallback["ok"] = False
                fallback["error"] = err_msg
                _PROVIDER_CACHE["usage"] = fallback
        _PROVIDER_CACHE["usage_fetched_at"] = (
            done_at if res.get("ok") else _PROVIDER_CACHE["usage_fetched_at"]
        )
    return res


def _run_sessions_probe_task(fn: Callable[[], Any]) -> Dict[str, Any]:
    try:
        raw = fn()
        if isinstance(raw, dict):
            sessions = list(raw.get("sessions") or [])
            ok = bool(raw.get("ok", True))
            err = raw.get("error")
        elif isinstance(raw, list):
            sessions = list(raw)
            ok = True
            err = None
        else:
            sessions = []
            ok = False
            err = "Invalid sessions payload"
    except Exception as exc:
        sessions = []
        ok = False
        err = str(exc)

    done_at = time.time()
    with _PROVIDER_CACHE_LOCK:
        _PROVIDER_CACHE["last_attempt_at"] = done_at
        if ok:
            _PROVIDER_CACHE["sessions"] = sessions
            _PROVIDER_CACHE["sessions_fetched_at"] = done_at
            _PROVIDER_CACHE["last_success_at"] = max(_PROVIDER_CACHE["last_success_at"], done_at)
        elif err:
            _PROVIDER_CACHE["last_error"] = str(err)
    return {"ok": ok, "sessions": sessions, "error": err}


def fetch_provider_status_nonblocking(
    *,
    usage_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    sessions_fn: Optional[Callable[[], Any]] = None,
    ttl_seconds: float = 15.0,
    wait_timeout_seconds: float = 0.5,
    force_refresh: bool = False,
    include_sessions: bool = True,
) -> Dict[str, Any]:
    """
    Fetch Colab usage & sessions through a bounded ThreadPoolExecutor + single-flight TTL cache.

    If the provider hangs (e.g. 30s WSL/Colab freeze), waits at most wait_timeout_seconds
    and returns cached/fallback data immediately with explicit staleness metadata instead of
    blocking the web or Telegram event loop or spawning unbounded polling threads.
    """
    now = time.time()
    ttl = max(0.0, float(ttl_seconds))
    wait_sec = max(0.02, float(wait_timeout_seconds))

    resolved_usage_fn = usage_fn or _default_usage_callable()
    resolved_sessions_fn = sessions_fn or _default_sessions_callable()

    futures_to_wait = []
    u_fut = None
    s_fut = None
    used_cache = False

    with _PROVIDER_CACHE_LOCK:
        u_fresh = (
            not force_refresh
            and _PROVIDER_CACHE["usage_fetched_at"] > 0
            and (now - _PROVIDER_CACHE["usage_fetched_at"]) <= ttl
        )
        s_fresh = (
            not include_sessions
            or (
                not force_refresh
                and _PROVIDER_CACHE["sessions_fetched_at"] > 0
                and (now - _PROVIDER_CACHE["sessions_fetched_at"]) <= ttl
            )
        )

        if u_fresh and s_fresh:
            used_cache = True
        else:
            _PROVIDER_CACHE["last_attempt_at"] = now
            cur_u_fut = _PROVIDER_CACHE["usage_future"]
            if not u_fresh or force_refresh:
                if (
                    cur_u_fut is None
                    or cur_u_fut.done()
                    or _PROVIDER_CACHE.get("usage_fn") is not resolved_usage_fn
                ):
                    cur_u_fut = _PROVIDER_PROBE_EXECUTOR.submit(_run_usage_probe_task, resolved_usage_fn)
                    _PROVIDER_CACHE["usage_future"] = cur_u_fut
                    _PROVIDER_CACHE["usage_fn"] = resolved_usage_fn
                u_fut = cur_u_fut
                futures_to_wait.append(u_fut)

            if include_sessions:
                cur_s_fut = _PROVIDER_CACHE["sessions_future"]
                if not s_fresh or force_refresh:
                    if (
                        cur_s_fut is None
                        or cur_s_fut.done()
                        or _PROVIDER_CACHE.get("sessions_fn") is not resolved_sessions_fn
                    ):
                        cur_s_fut = _PROVIDER_PROBE_EXECUTOR.submit(_run_sessions_probe_task, resolved_sessions_fn)
                        _PROVIDER_CACHE["sessions_future"] = cur_s_fut
                        _PROVIDER_CACHE["sessions_fn"] = resolved_sessions_fn
                    s_fut = cur_s_fut
                    futures_to_wait.append(s_fut)

    not_done = set()
    if futures_to_wait:
        _, not_done = concurrent.futures.wait(
            futures_to_wait,
            timeout=wait_sec,
            return_when=concurrent.futures.ALL_COMPLETED,
        )

    usage_timed_out = bool(u_fut is not None and u_fut in not_done)
    sessions_timed_out = bool(s_fut is not None and s_fut in not_done)
    timed_out = usage_timed_out or sessions_timed_out

    now_after = time.time()
    with _PROVIDER_CACHE_LOCK:
        live_u_fut = _PROVIDER_CACHE["usage_future"]
        live_s_fut = _PROVIDER_CACHE["sessions_future"]
        in_flight = bool(
            (live_u_fut and not live_u_fut.done())
            or (include_sessions and live_s_fut and not live_s_fut.done())
        )
        last_ok = float(_PROVIDER_CACHE["last_success_at"] or 0.0)
        usage_fetched_at = float(_PROVIDER_CACHE["usage_fetched_at"] or 0.0)
        age_seconds = round(max(0.0, now_after - last_ok), 2) if last_ok > 0 else None
        balance_age_seconds = (
            round(max(0.0, now_after - usage_fetched_at), 2) if usage_fetched_at > 0 else None
        )
        usage_copy = dict(_PROVIDER_CACHE["usage"])
        raw_balance = usage_copy.get("balance")
        try:
            balance_available = (
                usage_fetched_at > 0
                and not isinstance(raw_balance, bool)
                and math.isfinite(float(raw_balance))
            )
        except (TypeError, ValueError, OverflowError):
            balance_available = False
        sessions_copy = [dict(s) if isinstance(s, dict) else s for s in _PROVIDER_CACHE["sessions"]]
        last_err = _PROVIDER_CACHE["last_error"]
        stale = bool(
            usage_timed_out
            or (include_sessions and sessions_timed_out)
            or last_ok <= 0
            or (now_after - float(_PROVIDER_CACHE["usage_fetched_at"] or 0.0)) > ttl
            or not usage_copy.get("ok", False)
        )
        provider_unreachable = bool(timed_out or last_err or not usage_copy.get("ok", False))
        if stale:
            usage_copy["stale"] = True
            if usage_timed_out or not usage_copy.get("ok"):
                usage_copy["ok"] = False
        if timed_out and not last_err:
            last_err = f"Provider probe timed out after {wait_sec:.2f}s"

    return {
        "usage": usage_copy,
        "sessions": sessions_copy,
        "provider_cache": {
            "cached": used_cache or stale,
            "stale": stale,
            "in_flight": in_flight,
            "timed_out": timed_out,
            "provider_unreachable": provider_unreachable,
            "age_seconds": age_seconds,
            "last_success_utc": epoch_to_utc_iso(last_ok) if last_ok > 0 else None,
            "balance_available": balance_available,
            "balance_age_seconds": balance_age_seconds,
            "balance_updated_utc": epoch_to_utc_iso(usage_fetched_at) if usage_fetched_at > 0 else None,
            "balance_stale": bool(
                balance_available and balance_age_seconds is not None and balance_age_seconds > ttl
            ),
            "ttl_seconds": ttl,
            "error": last_err,
        },
    }


def run_provider_action_nonblocking(
    fn: Callable[..., Any],
    *args: Any,
    timeout_seconds: float = 0.8,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    Execute a mutating/control provider call in a bounded executor and return within
    timeout_seconds even if the provider CLI hangs.
    """
    timeout = max(0.05, float(timeout_seconds))
    future = _PROVIDER_CONTROL_EXECUTOR.submit(fn, *args, **kwargs)
    try:
        result = future.result(timeout=timeout)
        return {
            "completed": True,
            "timed_out": False,
            "result": result,
            "error": None,
        }
    except concurrent.futures.TimeoutError:
        return {
            "completed": False,
            "timed_out": True,
            "result": None,
            "error": f"Provider action timed out after {timeout:.2f}s",
        }
    except Exception as exc:
        return {
            "completed": False,
            "timed_out": False,
            "result": None,
            "error": str(exc),
        }


def get_cloud_status_snapshot(
    store: Any,
    *,
    worker: Any = None,
    usage_fn: Optional[Callable[[], Dict[str, Any]]] = None,
    sessions_fn: Optional[Callable[[], Any]] = None,
    ttl_seconds: float = 15.0,
    wait_timeout_seconds: float = 0.5,
    force_refresh: bool = False,
    include_sessions: bool = True,
) -> Dict[str, Any]:
    """
    Shared read-only status service for Dashboard / API and Telegram.

    Never instantiates or starts CloudWorker, never opens sessions, and never mutates DB rows.
    """
    started = time.monotonic()
    probe = fetch_provider_status_nonblocking(
        usage_fn=usage_fn,
        sessions_fn=sessions_fn,
        ttl_seconds=ttl_seconds,
        wait_timeout_seconds=wait_timeout_seconds,
        force_refresh=force_refresh,
        include_sessions=include_sessions,
    )
    worker_jobs_fn = getattr(worker, "get_current_jobs", None) if worker is not None else None
    try:
        worker_jobs = worker_jobs_fn() if callable(worker_jobs_fn) else []
    except Exception:
        worker_jobs = []
    worker_job = worker_jobs[0] if worker_jobs else (worker.get_current_job() if worker is not None else None)
    list_active_fn = getattr(store, "list_active_jobs", None)
    db_active_jobs = list_active_fn() if callable(list_active_fn) else []
    if not db_active_jobs:
        fallback_active = store.get_active_job()
        db_active_jobs = [fallback_active] if fallback_active is not None else []
    if (
        worker_job is not None
        and db_active_jobs
        and any(getattr(worker_job, "job_id", None) == getattr(job, "job_id", None) for job in db_active_jobs)
    ):
        current_job = next(job for job in db_active_jobs if job.job_id == worker_job.job_id)
    else:
        current_job = worker_job or (db_active_jobs[0] if db_active_jobs else None)
    budget = store.get_cu_budget_info()
    pending_cleanup = (
        store.list_pending_cleanup_events()
        if hasattr(store, "list_pending_cleanup_events")
        else []
    )
    warm_sessions = (
        store.list_warm_sessions(include_expired=False)
        if hasattr(store, "list_warm_sessions")
        else []
    )
    active_job_dict = current_job.to_dict() if current_job is not None else None
    if active_job_dict is not None and "error_remediation" not in active_job_dict:
        active_job_dict["error_remediation"] = get_error_remediation(active_job_dict.get("error_code"))
    active_jobs_dict = []
    for active_job in db_active_jobs:
        active_dict = active_job.to_dict()
        if "error_remediation" not in active_dict:
            active_dict["error_remediation"] = get_error_remediation(active_dict.get("error_code"))
        active_jobs_dict.append(active_dict)
    rollout = store.get_rollout() if hasattr(store, "get_rollout") else {"phase": "paused"}
    alerts = store.list_rollout_alerts() if hasattr(store, "list_rollout_alerts") else []
    status_latency_ms = round((time.monotonic() - started) * 1000)
    if status_latency_ms > 1000:
        alerts.append({"code": "STATUS_SLOW"})

    return {
        "status": "ok",
        "usage": probe["usage"],
        "sessions": probe["sessions"],
        "provider_cache": probe["provider_cache"],
        "worker_running": bool(worker.is_running()) if worker is not None else False,
        "worker_state": worker.get_worker_state() if worker is not None else "idle",
        "accepting_jobs": bool(worker.is_accepting_jobs()) if worker is not None else False,
        "active_job": active_job_dict,
        "active_jobs": active_jobs_dict,
        "active_job_count": len(active_jobs_dict),
        "max_concurrent_jobs": (
            store.get_concurrency_limit() if hasattr(store, "get_concurrency_limit") else 1
        ),
        "budget": budget,
        "pending_cleanup_events": pending_cleanup,
        "warm_sessions": warm_sessions,
        "unrecovered_resources_count": len(pending_cleanup),
        "rollout": rollout,
        "rollout_alerts": alerts,
        "status_latency_ms": status_latency_ms,
    }


def request_job_cancellation(
    store: Any,
    job_id: str,
    *,
    worker: Any = None,
    adapter: Any = None,
    abort_fn: Optional[Callable[[str], Any]] = None,
    reason: str = "Huỷ theo yêu cầu người dùng.",
    remote_timeout_seconds: float = 0.8,
) -> Dict[str, Any]:
    """
    Shared non-blocking cancellation entrypoint for REST/Dashboard and Telegram.

    Transitions queued jobs immediately to cancelled; for active jobs signals worker / runs
    remote stop within a short timeout so a hanging provider cannot freeze the caller.
    """
    job = store.get_job(job_id)
    if not job:
        return {
            "found": False,
            "cancelled": False,
            "cancel_requested": False,
            "state": None,
            "timed_out": False,
            "message": "Không tìm thấy job.",
        }

    req = store.request_cancel(job_id, reason=reason)
    if not req.get("cancelled"):
        current_state = str(req.get("state") or job.state)
        latest_job = store.get_job(job_id) or job
        if current_state == CloudJobState.CANCELLED.value:
            return {
                "found": True,
                "cancelled": True,
                "cancel_requested": True,
                "idempotent": True,
                "state": CloudJobState.CANCELLED.value,
                "timed_out": False,
                "message": f"Job {job_id} đã ở trạng thái đã huỷ.",
                "job": latest_job.to_dict(),
            }
        return {
            "found": True,
            "cancelled": False,
            "cancel_requested": False,
            "state": current_state,
            "timed_out": False,
            "message": "Job đã kết thúc trước đó hoặc không thể huỷ.",
            "job": latest_job.to_dict(),
        }

    if req.get("immediate"):
        latest = store.get_job(job_id)
        return {
            "found": True,
            "cancelled": True,
            "cancel_requested": True,
            "state": latest.state if latest else "cancelled",
            "timed_out": False,
            "message": f"Đã huỷ job {job_id} khỏi hàng đợi.",
            "job": latest.to_dict() if latest else None,
        }

    session_name = req.get("session_name") or job.session_name
    if session_name and abort_fn is not None:
        run_provider_action_nonblocking(
            abort_fn,
            session_name,
            timeout_seconds=min(0.4, remote_timeout_seconds),
        )

    cur_job = None
    if worker is not None:
        try:
            cur_job = worker.get_current_job(job_id)
        except TypeError:
            candidate = worker.get_current_job()
            cur_job = candidate if getattr(candidate, "job_id", None) == job_id else None
    if worker is not None and cur_job is not None and worker.is_running():
        cancel_job = getattr(worker, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_id, reason=reason)
        else:
            worker.cancel_current_job(reason=reason)
        latest = store.get_job(job_id)
        final_state = latest.state if latest else str(req.get("state") or "cancel_requested")
        is_cancelled = final_state == CloudJobState.CANCELLED.value
        return {
            "found": True,
            "cancelled": is_cancelled,
            "cancel_requested": final_state in ("cancel_requested", "cancelling", "cancelled"),
            "state": final_state,
            "timed_out": False,
            "message": (
                f"Đã huỷ job {job_id} và xác nhận thu hồi session thành công."
                if is_cancelled
                else f"Đã gửi yêu cầu dừng job {job_id} (trạng thái: {final_state})."
            ),
            "job": latest.to_dict() if latest else None,
        }

    if not session_name:
        store.update_progress(
            job_id,
            state=CloudJobState.CANCELLING.value,
            step_name="Đang hoàn tất huỷ job (không có session đang chạy)...",
            allow_from_cancel=True,
        )
        store.update_progress(
            job_id,
            state=CloudJobState.CANCELLED.value,
            step_name="Đã huỷ công việc (không có session đang chạy)",
            cleanup_status=CloudCleanupStatus.VERIFIED.value,
            error_code=CloudErrorCode.CANCELLED_BY_USER.value,
            error=reason,
            allow_from_cancel=True,
        )
        latest = store.get_job(job_id)
        return {
            "found": True,
            "cancelled": True,
            "cancel_requested": True,
            "state": latest.state if latest else "cancelled",
            "timed_out": False,
            "message": f"Đã huỷ job {job_id} thành công.",
            "job": latest.to_dict() if latest else None,
        }

    # Transition to cancelling first so dashboard immediately shows pending resource teardown,
    # then attempt remote teardown inside bounded executor.
    store.update_progress(
        job_id,
        state=CloudJobState.CANCELLING.value,
        step_name="Đang dừng và xác minh thu hồi session Colab...",
        allow_from_cancel=True,
    )
    resolved_adapter = adapter
    if resolved_adapter is None:
        from .colab_bridge import ColabProviderAdapter

        resolved_adapter = ColabProviderAdapter()

    action_res = run_provider_action_nonblocking(
        store._stop_and_verify_with_adapter,
        resolved_adapter,
        session_name,
        timeout_seconds=remote_timeout_seconds,
    )
    attempt_num = max(1, int(job.attempt or 1))
    if action_res["completed"] and isinstance(action_res["result"], dict):
        cleanup = action_res["result"]
        store.record_cleanup_result(
            job_id,
            attempt_num,
            session_name,
            verified_stopped=bool(cleanup.get("verified_stopped")),
            cleanup_status=str(
                cleanup.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)
            ),
            attempts=int(cleanup.get("attempts", 1)),
            error=cleanup.get("error"),
        )
        if cleanup.get("verified_stopped"):
            store.update_progress(
                job_id,
                state=CloudJobState.CANCELLED.value,
                step_name="Đã huỷ và xác nhận thu hồi session GPU",
                cleanup_status=CloudCleanupStatus.VERIFIED.value,
                error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                error=reason,
                allow_from_cancel=True,
            )
            latest = store.get_job(job_id)
            return {
                "found": True,
                "cancelled": True,
                "cancel_requested": True,
                "state": latest.state if latest else "cancelled",
                "timed_out": False,
                "message": f"Đã huỷ job {job_id} và xác nhận thu hồi session thành công.",
                "job": latest.to_dict() if latest else None,
            }
        store.update_progress(
            job_id,
            state=CloudJobState.CANCELLING.value,
            step_name="Chưa xác nhận dừng được session GPU; đã ghi nhận sự kiện chờ thu hồi",
            cleanup_status=str(cleanup.get("cleanup_status")),
            error=cleanup.get("error") or "Không xác nhận được session đã dừng",
            allow_from_cancel=True,
        )
        latest = store.get_job(job_id)
        return {
            "found": True,
            "cancelled": False,
            "cancel_requested": True,
            "state": latest.state if latest else "cancelling",
            "timed_out": False,
            "message": f"Đã ghi nhận yêu cầu huỷ job {job_id}; session GPU vẫn đang chờ thu hồi/đối soát.",
            "job": latest.to_dict() if latest else None,
        }

    # Remote provider call timed out or raised: keep pending cleanup event & cancelling state
    store.record_cleanup_result(
        job_id,
        attempt_num,
        session_name,
        verified_stopped=False,
        cleanup_status=CloudCleanupStatus.PENDING.value,
        attempts=0,
        error=action_res.get("error") or "Hết thời gian chờ phản hồi từ Colab khi dừng session",
    )
    latest = store.get_job(job_id)
    final_state = latest.state if latest else CloudJobState.CANCELLING.value
    return {
        "found": True,
        "cancelled": False,
        "cancel_requested": True,
        "state": final_state,
        "timed_out": bool(action_res.get("timed_out")),
        "message": (
            f"Đã ghi nhận huỷ job {job_id}; provider phản hồi chậm nên đang tiếp tục thu hồi session."
        ),
        "job": latest.to_dict() if latest else None,
    }


def normalize_state(raw_state: str | CloudJobState) -> CloudJobState:
    """Convert either a legacy or canonical state string into CloudJobState."""
    if isinstance(raw_state, CloudJobState):
        return raw_state
    key = str(raw_state or "").strip().lower()
    if key in _LEGACY_TO_CANONICAL:
        return _LEGACY_TO_CANONICAL[key]
    raise ValueError(f"Unknown Cloud job state: {raw_state!r}")


def to_legacy_state(state: CloudJobState | str) -> str:
    """Map canonical state back to Phase 0/1 CloudJobStore legacy string."""
    canonical = normalize_state(state)
    return _CANONICAL_TO_LEGACY[canonical]


def can_transition(src: CloudJobState | str, dst: CloudJobState | str) -> bool:
    """Check whether transitioning from src to dst respects the contract graph."""
    src_c = normalize_state(src)
    dst_c = normalize_state(dst)
    if src_c in TERMINAL_STATES:
        return False
    return dst_c in ALLOWED_TRANSITIONS.get(src_c, frozenset())


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format with 'Z' suffix."""
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def epoch_to_utc_iso(epoch_ts: Optional[float]) -> Optional[str]:
    """Convert Unix epoch seconds to UTC ISO-8601 string."""
    if epoch_ts is None:
        return None
    return (
        dt.datetime.fromtimestamp(float(epoch_ts), tz=dt.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _has_known_video_signature(header: bytes, extension: str) -> bool:
    """Check container magic bytes for common video formats."""
    ext = (extension or "").lower()
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return True
    if header.startswith(b"\x1a\x45\xdf\xa3"):  # EBML: .mkv / .webm
        return True
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] in (b"AVI ", b"AVIX"):
        return True
    if header.startswith(b"FLV"):
        return True
    if header.startswith(b"OggS"):
        return True
    if header.startswith(b"\x30\x26\xb2\x75"):  # ASF / WMV
        return True
    if ext == ".ts" and len(header) >= 1 and header[0:1] == b"G":
        return True
    return False


def probe_video_metadata(video_path: str | Path) -> Dict[str, Any]:
    """
    Inspect local video container header and, when available, run ffprobe (shell=False).

    Safely falls back to header metadata if ffprobe is unavailable or fails on
    minimal synthetic test fixtures.
    """
    path_obj = Path(video_path)
    extension = path_obj.suffix.lower()
    info: Dict[str, Any] = {
        "exists": False,
        "extension": extension,
        "size_bytes": 0,
        "header_valid": False,
        "duration_seconds": None,
        "video_codec": None,
        "audio_codec": None,
        "width": None,
        "height": None,
        "probe_method": "missing",
    }
    if not path_obj.is_file():
        return info

    size_bytes = path_obj.stat().st_size
    info["exists"] = True
    info["size_bytes"] = size_bytes

    try:
        with open(path_obj, "rb") as f:
            header = f.read(64)
    except OSError:
        header = b""
    info["header_valid"] = size_bytes > 0 and _has_known_video_signature(header, extension)
    info["probe_method"] = "header_fallback"

    if size_bytes <= 0:
        return info

    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path_obj),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            payload = json.loads(proc.stdout)
            fmt = payload.get("format") or {}
            streams = payload.get("streams") or []
            raw_dur = fmt.get("duration")
            if raw_dur is not None:
                try:
                    dur_val = float(raw_dur)
                    if dur_val >= 0:
                        info["duration_seconds"] = dur_val
                except (TypeError, ValueError):
                    pass
            for stream in streams:
                ctype = stream.get("codec_type")
                if ctype == "video" and info["video_codec"] is None:
                    info["video_codec"] = stream.get("codec_name")
                    if stream.get("width") is not None:
                        try:
                            info["width"] = int(stream["width"])
                        except (TypeError, ValueError):
                            pass
                    if stream.get("height") is not None:
                        try:
                            info["height"] = int(stream["height"])
                        except (TypeError, ValueError):
                            pass
                    if info["duration_seconds"] is None and stream.get("duration") is not None:
                        try:
                            info["duration_seconds"] = float(stream["duration"])
                        except (TypeError, ValueError):
                            pass
                elif ctype == "audio" and info["audio_codec"] is None:
                    info["audio_codec"] = stream.get("codec_name")
            info["probe_method"] = "ffprobe"
    except (FileNotFoundError, subprocess.SubprocessError, OSError, json.JSONDecodeError):
        pass

    return info


def build_input_fingerprint(
    input_target: str,
    *,
    kind: str = "auto",
    duration_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Build deterministic fingerprint metadata for either a local video file or an http(s) URL.
    """
    resolved_kind = kind
    if resolved_kind == "auto":
        resolved_kind = (
            "url"
            if str(input_target).strip().lower().startswith(("http://", "https://"))
            else "local_file"
        )

    probed_duration: Optional[float] = None
    if resolved_kind == "url":
        canonical = str(input_target).strip()
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        fp: Dict[str, Any] = {
            "kind": "url",
            "sha256": digest,
            "size_bytes": None,
        }
    else:
        path_obj = Path(input_target)
        h = hashlib.sha256()
        size_bytes = 0
        probe: Optional[Dict[str, Any]] = None
        if path_obj.is_file():
            size_bytes = path_obj.stat().st_size
            with open(path_obj, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
            probe = probe_video_metadata(path_obj)
        else:
            h.update(str(path_obj).encode("utf-8"))
        fp = {
            "kind": "local_file",
            "sha256": h.hexdigest(),
            "size_bytes": size_bytes,
            "extension": path_obj.suffix.lower(),
        }
        if probe is not None:
            for meta_key in ("video_codec", "audio_codec", "width", "height"):
                if probe.get(meta_key) is not None:
                    fp[meta_key] = probe[meta_key]
            if probe.get("duration_seconds") is not None:
                probed_duration = float(probe["duration_seconds"])

    effective_duration = duration_seconds if duration_seconds is not None else probed_duration
    if effective_duration is not None:
        fp["duration_seconds"] = float(effective_duration)
    return fp


@dataclass(frozen=True)
class CloudJobOptionsSnapshot:
    gpu_type: str
    task_type: str
    voice: str
    cu_estimated: float
    cu_budget_daily: float
    output_prefix: str
    execution_mode: str = "cloud"
    allow_no_speech: bool = False
    extra_options: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CloudJobOptionsSnapshot":
        known_keys = {
            "gpu_type",
            "task_type",
            "voice",
            "cu_estimated",
            "cu_budget_daily",
            "output_prefix",
            "execution_mode",
            "allow_no_speech",
            "extra_options",
        }
        extra = dict(data.get("extra_options") or {})
        for k, v in data.items():
            if k not in known_keys:
                extra[k] = v
        return cls(
            gpu_type=str(data.get("gpu_type", "T4")),
            task_type=str(data.get("task_type", "full_pipeline")),
            voice=str(data.get("voice", "BV562_streaming")),
            cu_estimated=float(data.get("cu_estimated", 0.5)),
            cu_budget_daily=float(
                data.get("cu_budget_daily", DEFAULT_CONFIG["cu_budget_daily"])
            ),
            output_prefix=str(data.get("output_prefix", DEFAULT_CONFIG["output_prefix"])),
            execution_mode=str(data.get("execution_mode", "cloud")),
            allow_no_speech=bool(data.get("allow_no_speech", False)),
            extra_options=extra,
        )


@dataclass(frozen=True)
class CloudSessionOwnership:
    owner_id: str
    attempt: int
    session_name: Optional[str] = None
    lease_expires_at_utc: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CloudSessionOwnership":
        return cls(
            owner_id=str(data.get("owner_id", "")),
            attempt=int(data.get("attempt", 1)),
            session_name=data.get("session_name"),
            lease_expires_at_utc=data.get("lease_expires_at_utc"),
        )


@dataclass(frozen=True)
class CloudOutputManifest:
    delivery_path: Optional[str] = None
    metadata_path: Optional[str] = None
    video_sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    validated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CloudOutputManifest":
        return cls(
            delivery_path=data.get("delivery_path"),
            metadata_path=data.get("metadata_path"),
            video_sha256=data.get("video_sha256"),
            size_bytes=data.get("size_bytes"),
            validated=bool(data.get("validated", False)),
        )


@dataclass
class CloudJobContract:
    """Canonical job schema covering Phase 0 required fields."""

    job_uuid: str
    job_id: str
    source_channel: str
    source_job_id: Optional[str]
    input_target: str
    display_name: str
    remote_filename: str
    input_fingerprint: Dict[str, Any]
    options_snapshot: CloudJobOptionsSnapshot
    attempt: int = 1
    session_ownership: CloudSessionOwnership = field(
        default_factory=lambda: CloudSessionOwnership(owner_id="unclaimed", attempt=1)
    )
    state: CloudJobState = CloudJobState.QUEUED
    created_at_utc: str = field(default_factory=utc_now_iso)
    updated_at_utc: str = field(default_factory=utc_now_iso)
    started_at_utc: Optional[str] = None
    completed_at_utc: Optional[str] = None
    deadline_utc: Optional[str] = None
    error_code: Optional[CloudErrorCode] = None
    error_message: Optional[str] = None
    output_manifest: Optional[CloudOutputManifest] = None
    idempotency_key: Optional[str] = None
    deduplicated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_uuid": self.job_uuid,
            "job_id": self.job_id,
            "source_channel": self.source_channel,
            "source_job_id": self.source_job_id,
            "input_target": self.input_target,
            "display_name": self.display_name,
            "remote_filename": self.remote_filename,
            "input_fingerprint": dict(self.input_fingerprint),
            "options_snapshot": self.options_snapshot.to_dict(),
            "attempt": self.attempt,
            "session_ownership": self.session_ownership.to_dict(),
            "state": self.state.value,
            "legacy_state": to_legacy_state(self.state),
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "started_at_utc": self.started_at_utc,
            "completed_at_utc": self.completed_at_utc,
            "deadline_utc": self.deadline_utc,
            "error_code": self.error_code.value if self.error_code else None,
            "error_message": self.error_message,
            "output_manifest": self.output_manifest.to_dict()
            if self.output_manifest
            else None,
            "idempotency_key": self.idempotency_key,
            "deduplicated": self.deduplicated,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CloudJobContract":
        err_raw = data.get("error_code")
        return cls(
            job_uuid=str(data["job_uuid"]),
            job_id=str(data["job_id"]),
            source_channel=str(data.get("source_channel", "dashboard")),
            source_job_id=data.get("source_job_id"),
            input_target=str(data["input_target"]),
            display_name=str(data.get("display_name", "video.mp4")),
            remote_filename=str(data.get("remote_filename", "input_video.mp4")),
            input_fingerprint=dict(data.get("input_fingerprint") or {}),
            options_snapshot=CloudJobOptionsSnapshot.from_dict(
                data.get("options_snapshot") or {}
            ),
            attempt=int(data.get("attempt", 1)),
            session_ownership=CloudSessionOwnership.from_dict(
                data.get("session_ownership") or {}
            ),
            state=normalize_state(data.get("state", "queued")),
            created_at_utc=str(data.get("created_at_utc") or utc_now_iso()),
            updated_at_utc=str(data.get("updated_at_utc") or utc_now_iso()),
            started_at_utc=data.get("started_at_utc"),
            completed_at_utc=data.get("completed_at_utc"),
            deadline_utc=data.get("deadline_utc"),
            error_code=CloudErrorCode(err_raw) if err_raw else None,
            error_message=data.get("error_message"),
            output_manifest=CloudOutputManifest.from_dict(data["output_manifest"])
            if data.get("output_manifest")
            else None,
            idempotency_key=data.get("idempotency_key"),
            deduplicated=bool(data.get("deduplicated", False)),
        )


def _claim_specific_queued_job(
    store: Any,
    job_id: str,
    owner_id: str,
    *,
    enforce_budget: bool = True,
    provider_usage: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    Atomically claim a newly enqueued job in store so CloudWorker cannot double-claim it.
    """
    if hasattr(store, "claim_job"):
        try:
            return store.claim_job(
                job_id,
                owner_id=owner_id,
                enforce_budget=enforce_budget,
                provider_usage=provider_usage,
            )
        except TypeError:
            return store.claim_job(job_id, owner_id=owner_id)

    if not hasattr(store, "_connect"):
        store.update_progress(
            job_id,
            state="provisioning",
            percent=5,
            step_name=f"Đã giữ chỗ cho {owner_id}...",
        )
        return store.get_job(job_id)

    now = time.time()
    with store._connect() as db:
        cursor = db.execute(
            """
            UPDATE cloud_jobs
            SET state = 'provisioning',
                percent = 5,
                step_name = ?,
                started_at = COALESCE(started_at, ?)
            WHERE job_id = ? AND state = 'queued'
            """,
            (f"Đã nhận job ({owner_id})...", now, job_id),
        )
        if cursor.rowcount != 1:
            return None
    return store.get_job(job_id)


def cloud_deadline_seconds(deadline_seconds: Optional[int] = None) -> int:
    if deadline_seconds is not None:
        return int(deadline_seconds)
    return int(DEFAULT_CONFIG["exec_timeout_seconds"]) + int(DEFAULT_CONFIG.get("create_timeout_seconds", 180))


def _build_contract_from_job(
    job: Any,
    *,
    validated_input: Any,
    remote_name: str,
    fingerprint: Dict[str, Any],
    source_channel: str,
    source_job_id: Optional[str],
    budget_daily: float,
    opts_dict: Dict[str, Any],
    deadline_seconds: Optional[int],
    owner_id: str,
    state: CloudJobState,
    idempotency_key: Optional[str] = None,
    deduplicated: bool = False,
) -> CloudJobContract:
    now_epoch = time.time()
    deadline_at = getattr(job, "deadline_at", None) or (
        getattr(job, "created_at", now_epoch) + cloud_deadline_seconds(deadline_seconds)
    )
    job_opts = dict(getattr(job, "options", None) or opts_dict or {})
    job_opts["source_channel"] = normalize_cloud_source_channel(source_channel) or "dashboard"
    known_snapshot_keys = {
        "gpu_type",
        "task_type",
        "voice",
        "cu_estimated",
        "cu_budget_daily",
        "output_prefix",
        "execution_mode",
        "allow_no_speech",
        "extra_options",
    }
    extra_opts = {k: v for k, v in job_opts.items() if k not in known_snapshot_keys}
    err_code_val = getattr(job, "error_code", None)
    parsed_err_code = None
    if err_code_val:
        try:
            parsed_err_code = CloudErrorCode(str(err_code_val))
        except ValueError:
            parsed_err_code = None

    return CloudJobContract(
        job_uuid=str(uuid.uuid4()),
        job_id=job.job_id,
        source_channel=source_channel,
        source_job_id=source_job_id,
        input_target=validated_input.normalized_value,
        display_name=validated_input.display_name,
        remote_filename=remote_name,
        input_fingerprint=fingerprint,
        options_snapshot=CloudJobOptionsSnapshot(
            gpu_type=str(getattr(job, "gpu_type", None) or job_opts.get("gpu_type", "T4")),
            task_type=str(getattr(job, "task_type", None) or job_opts.get("task_type", "full_pipeline")),
            voice=str(getattr(job, "voice", None) or job_opts.get("voice", "BV562_streaming")),
            cu_estimated=float(getattr(job, "cu_estimated", 0.5)),
            cu_budget_daily=float(budget_daily),
            output_prefix=str(job_opts.get("output_prefix") or DEFAULT_CONFIG.get("output_prefix", "cloud_")),
            execution_mode=str(job_opts.get("execution_mode", "cloud")),
            allow_no_speech=bool(job_opts.get("allow_no_speech", False)),
            extra_options=extra_opts,
        ),
        attempt=int(getattr(job, "attempt", 0) or 1),
        session_ownership=CloudSessionOwnership(
            owner_id=str(getattr(job, "owner_id", None) or owner_id or "unclaimed"),
            attempt=int(getattr(job, "attempt", 0) or 1),
            session_name=getattr(job, "session_name", None),
            lease_expires_at_utc=epoch_to_utc_iso(getattr(job, "lease_expires_at", None)),
        ),
        state=state,
        created_at_utc=epoch_to_utc_iso(getattr(job, "created_at", None)) or utc_now_iso(),
        updated_at_utc=utc_now_iso(),
        started_at_utc=epoch_to_utc_iso(getattr(job, "started_at", None)),
        completed_at_utc=epoch_to_utc_iso(getattr(job, "completed_at", None)),
        deadline_utc=epoch_to_utc_iso(deadline_at),
        error_code=parsed_err_code,
        error_message=getattr(job, "error", None),
        idempotency_key=idempotency_key or getattr(job, "idempotency_key", None),
        deduplicated=deduplicated,
    )


TELEGRAM_SOURCE_CHANNEL = "cloud_telegram_bot"


def normalize_cloud_source_channel(source_channel: Any) -> str:
    """Normalize trusted source channels and legacy Telegram aliases."""
    channel = str(source_channel).strip().lower().replace("-", "_") if source_channel is not None else ""
    if channel in {"telegram", "telegram_bot", "cloud_telegram", TELEGRAM_SOURCE_CHANNEL}:
        return TELEGRAM_SOURCE_CHANNEL
    return channel


def resolve_cloud_source_channel(options: Optional[Dict[str, Any]], idempotency_key: Optional[str]) -> str:
    """Use stored provenance, falling back to the legacy key only if no channel was recorded."""
    channel = normalize_cloud_source_channel((options or {}).get("source_channel"))
    if channel:
        return channel
    if str(idempotency_key or "").startswith("cloudbot:"):
        return TELEGRAM_SOURCE_CHANNEL
    return "dashboard"


def _check_idempotent_source(job: Any, idempotency_key: str, source_channel: str) -> None:
    existing_source = resolve_cloud_source_channel(getattr(job, "options", None), idempotency_key)
    if existing_source != source_channel:
        raise CloudValidationError(
            "idempotency_key đã được sử dụng bởi một kênh Cloud khác.",
            code=CloudErrorCode.CONFLICT_ALREADY_CLAIMED.value,
            http_status=409,
        )


def admit_and_enqueue_job(
    store: Any,
    *,
    video_path: str,
    source_channel: str = "dashboard",
    source_job_id: Optional[str] = None,
    gpu_type: str = "T4",
    task_type: str = "full_pipeline",
    voice: str = "BV562_streaming",
    cu_estimated: float = 0.5,
    cu_buffer: Optional[float] = None,
    allowed_roots: Optional[Sequence[str | Path]] = None,
    deadline_seconds: Optional[int] = None,
    require_exists: bool = True,
    enforce_budget: bool = True,
    claim_immediately: bool = False,
    owner_id: Optional[str] = None,
    provider_usage: Optional[Dict[str, Any]] = None,
    require_fresh_provider: bool = False,
    idempotency_key: Optional[str] = None,
    options: Optional[Dict[str, Any]] = None,
    rollout_permit: Optional[str] = None,
) -> Tuple[Any, CloudJobContract]:
    """
    Single shared admission entrypoint for REST/Dashboard and Telegram.

    Validates input target, GPU/task/voice allowlists, optional idempotency_key & options
    snapshot, checks daily CU budget policy, constructs the Phase 0/6 CloudJobContract
    snapshot, enqueues into CloudJobStore, and optionally claims atomically.
    """
    contract_source_channel = source_channel  # Preserve the existing contract's source label/alias.
    source_channel = normalize_cloud_source_channel(source_channel) or "dashboard"
    clean_idem_key = validate_idempotency_key(idempotency_key)
    if options is not None and not isinstance(options, dict):
        raise CloudValidationError(
            "Trường options phải là JSON object.",
            code=CloudErrorCode.INVALID_INPUT.value,
            http_status=400,
        )
    raw_opts: Dict[str, Any] = dict(options or {})
    exec_mode = str(raw_opts.get("execution_mode", "cloud")).strip().lower()
    if exec_mode not in ("cloud", "local"):
        raise CloudValidationError(
            f"execution_mode không hợp lệ: '{exec_mode}'. Chỉ chấp nhận 'cloud' hoặc 'local'.",
            code=CloudErrorCode.INVALID_INPUT.value,
            http_status=400,
        )

    validated_input = validate_video_input(
        video_path,
        allowed_roots=allowed_roots,
        require_exists=require_exists,
    )
    validated_gpu = validate_video_gpu_type(gpu_type)
    validated_task = validate_task_type(task_type)
    validated_voice = validate_voice(voice)

    remote_name = (
        sanitize_remote_filename(validated_input.display_name)
        if validated_input.kind == "local_file"
        else f"url_input{validated_input.extension}"
    )
    opts_snapshot_dict: Dict[str, Any] = {
        "gpu_type": validated_gpu,
        "task_type": validated_task,
        "voice": validated_voice,
        "execution_mode": exec_mode,
        "source_channel": source_channel,
        "allow_no_speech": bool(raw_opts.get("allow_no_speech", False)),
        "output_prefix": str(
            raw_opts.get("output_prefix") or DEFAULT_CONFIG.get("output_prefix", "cloud_")
        ),
    }
    for k, v in raw_opts.items():
        if isinstance(k, str) and k not in opts_snapshot_dict:
            opts_snapshot_dict[k] = v

    # Idempotent retry hit: return existing job immediately without charging budget twice
    if clean_idem_key and hasattr(store, "get_job_by_idempotency_key"):
        existing_job = store.get_job_by_idempotency_key(clean_idem_key)
        if existing_job is not None:
            _check_idempotent_source(existing_job, clean_idem_key, source_channel)
            budget_info = store.get_cu_budget_info()
            fingerprint = build_input_fingerprint(
                validated_input.normalized_value,
                kind=validated_input.kind,
            )
            contract = _build_contract_from_job(
                existing_job,
                validated_input=validated_input,
                remote_name=remote_name,
                fingerprint=fingerprint,
                source_channel=contract_source_channel,
                source_job_id=source_job_id,
                budget_daily=float(budget_info.get("budget_daily", DEFAULT_CONFIG["cu_budget_daily"])),
                opts_dict=opts_snapshot_dict,
                deadline_seconds=deadline_seconds,
                owner_id=getattr(existing_job, "owner_id", None) or "unclaimed",
                state=normalize_state(existing_job.state),
                idempotency_key=clean_idem_key,
                deduplicated=True,
            )
            return existing_job, contract

    if hasattr(store, "check_admission_allowed"):
        decision = store.check_admission_allowed(
            cu_estimated=float(cu_estimated),
            cu_buffer=cu_buffer,
            gpu_type=validated_gpu,
            enforce_budget=enforce_budget,
            provider_usage=provider_usage,
            require_fresh_provider=require_fresh_provider,
        )
        budget_info = decision.get("budget") or store.get_cu_budget_info()
        if not decision.get("allowed", True):
            raise CloudValidationError(
                decision.get("reason") or "Đã đạt hạn mức hoặc chưa xác nhận được ngân sách CU.",
                code=str(decision.get("code") or CloudErrorCode.BUDGET_EXCEEDED.value),
                http_status=int(decision.get("http_status") or 429),
            )
    else:
        budget_info = store.get_cu_budget_info()
        if enforce_budget and budget_info.get("is_exceeded"):
            raise CloudValidationError(
                f"Đã đạt hạn mức chi phí ngày ({budget_info['spent_today']:.2f}/{budget_info['budget_daily']:.2f} CU).",
                code=CloudErrorCode.BUDGET_EXCEEDED.value,
                http_status=429,
            )

    fingerprint = build_input_fingerprint(
        validated_input.normalized_value,
        kind=validated_input.kind,
    )

    enqueue_kwargs: Dict[str, Any] = {
        "video_path": validated_input.normalized_value,
        "video_name": validated_input.display_name,
        "gpu_type": validated_gpu,
        "task_type": validated_task,
        "cu_estimated": float(cu_estimated),
        "voice": validated_voice,
    }
    if cu_buffer is not None:
        enqueue_kwargs["cu_buffer"] = float(cu_buffer)
    if clean_idem_key is not None:
        enqueue_kwargs["idempotency_key"] = clean_idem_key
    enqueue_kwargs["options"] = opts_snapshot_dict
    claim_owner = owner_id or f"{source_channel}:{uuid.uuid4().hex[:8]}"
    controlled = getattr(store, "rollout_controlled", False) is True
    atomic_claim = controlled and claim_immediately
    if deadline_seconds is not None:
        enqueue_kwargs["deadline_seconds"] = deadline_seconds
    if controlled:
        enqueue_kwargs["rollout_permit"] = rollout_permit
    if atomic_claim:
        enqueue_kwargs.update(
            immediate_owner_id=claim_owner, enforce_budget=enforce_budget, provider_usage=provider_usage,
        )
    was_deduplicated = False
    enqueue_or_get_fn = getattr(store, "enqueue_or_get", None)
    try:
        if callable(enqueue_or_get_fn):
            job, was_deduplicated = enqueue_or_get_fn(**enqueue_kwargs)
        else:
            job = store.enqueue(**enqueue_kwargs)
    except TypeError:
        if controlled:
            raise
        enqueue_kwargs.pop("deadline_seconds", None)
        enqueue_kwargs.pop("cu_buffer", None)
        enqueue_kwargs.pop("idempotency_key", None)
        enqueue_kwargs.pop("options", None)
        if callable(enqueue_or_get_fn):
            job, was_deduplicated = enqueue_or_get_fn(**enqueue_kwargs)
        else:
            job = store.enqueue(**enqueue_kwargs)

    # Detect race where concurrent caller inserted same idempotency_key just before us
    if clean_idem_key and (was_deduplicated or (not atomic_claim and job.state != "queued")):
        _check_idempotent_source(job, clean_idem_key, source_channel)
        contract = _build_contract_from_job(
            job,
            validated_input=validated_input,
            remote_name=remote_name,
            fingerprint=fingerprint,
            source_channel=contract_source_channel,
            source_job_id=source_job_id,
            budget_daily=float(budget_info.get("budget_daily", DEFAULT_CONFIG["cu_budget_daily"])),
            opts_dict=opts_snapshot_dict,
            deadline_seconds=deadline_seconds,
            owner_id=getattr(job, "owner_id", None) or "unclaimed",
            state=normalize_state(job.state),
            idempotency_key=clean_idem_key,
            deduplicated=True,
        )
        return job, contract

    initial_state = CloudJobState.QUEUED
    if atomic_claim:
        initial_state = CloudJobState.PROVISIONING
    elif claim_immediately:
        claimed = _claim_specific_queued_job(
            store,
            job.job_id,
            claim_owner,
            enforce_budget=enforce_budget,
            provider_usage=provider_usage,
        )
        if not claimed:
            rejection = (
                store.consume_last_claim_rejection(job.job_id)
                if hasattr(store, "consume_last_claim_rejection")
                else None
            )
            if rejection:
                if hasattr(store, "cancel_job"):
                    try:
                        store.cancel_job(
                            job.job_id,
                            reason=rejection.get("reason") or "Admission claim rejected",
                            error_code=rejection.get("code") or CloudErrorCode.BUDGET_EXCEEDED.value,
                            cleanup_status="verified",
                        )
                    except Exception:
                        pass
                raise CloudValidationError(
                    rejection.get("reason") or "Không thể giữ chỗ ngân sách cho job.",
                    code=str(rejection.get("code") or CloudErrorCode.BUDGET_EXCEEDED.value),
                    http_status=int(rejection.get("http_status") or 429),
                )
            raise CloudValidationError(
                f"Job {job.job_id} đã được worker khác claim trước.",
                code=CloudErrorCode.CONFLICT_ALREADY_CLAIMED.value,
                http_status=409,
            )
        refreshed = store.get_job(job.job_id)
        if refreshed is not None:
            job = refreshed
        initial_state = CloudJobState.PROVISIONING

    contract = _build_contract_from_job(
        job,
        validated_input=validated_input,
        remote_name=remote_name,
        fingerprint=fingerprint,
        source_channel=contract_source_channel,
        source_job_id=source_job_id,
        budget_daily=float(budget_info.get("budget_daily", DEFAULT_CONFIG["cu_budget_daily"])),
        opts_dict=opts_snapshot_dict,
        deadline_seconds=deadline_seconds,
        owner_id=claim_owner if claim_immediately else "unclaimed",
        state=initial_state,
        idempotency_key=clean_idem_key,
        deduplicated=False,
    )
    return job, contract
