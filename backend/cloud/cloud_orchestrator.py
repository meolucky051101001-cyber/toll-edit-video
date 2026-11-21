"""
cloud_orchestrator.py - Bộ điều phối thực thi công việc Cloud GPU cho Tool V1.
Quản lý chu kỳ sống có lease, hủy thực sự và thu hồi session theo job/attempt:
  provisioning -> uploading -> preparing -> processing -> downloading -> validating -> completed
  (hoặc cancel_requested -> cancelling -> cancelled khi đã xác nhận máy ảo dừng).
"""

from __future__ import annotations

import base64
import errno
from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import inspect
import io
import json
import logging
import math
import os
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .cloud_config import DEFAULT_CONFIG
from .cloud_contract import (
    CloudCostMeasurementSource,
    CloudErrorCode,
    GPU_CU_RATES,
    build_input_fingerprint,
    probe_video_metadata,
)
from .cloud_job_store import CloudJob, CloudJobStore
from .cloud_security import (
    CloudValidationError,
    redact_secrets,
    sanitize_remote_filename,
    validate_gpu_type,
    validate_session_name,
    validate_task_type,
    validate_video_input,
    validate_voice,
)
from .colab_bridge import ColabProviderAdapter, _is_session_aborted, _wait_interruptible

logger = logging.getLogger("cloud.orchestrator")

_LEASE_LOST_REASONS = frozenset({"lease_lost", "lease_expired", "attempt_mismatch", "owner_mismatch", "not_found"})
_VALID_ERROR_CODES = frozenset(code.value for code in CloudErrorCode)
_SETUP_RESULT_PREFIX = "SETUP_ENV_RESULT_JSON:"
_PIPELINE_RESULT_PREFIX = "PIPELINE_RESULT_JSON:"
_DOWNLOADABLE_REMOTE_ARTIFACTS = frozenset(
    {"output_video.mp4", "result_metadata.json", "subtitles.srt", "subtitles_vi.srt", "subtitles_vi.ass",
     "asr_timing.json", "subtitles.ocr.json", "caption_review.json", "tts_timing.json", "subtitle_timing.json", "pipeline_segments.json", "caption_geometry.json"}
)
_V1_CORE_MODULE_NAMES = (
    "ass_utils.py",
    "subtitle_blur.py",
    "subtitle_layout.py",
    "subtitle_text.py",
    "v1_ocr_geometry.py",
    "ocr_subtitle_locator.py",
    "v1_timing_solver.py",
    "v1_speech_guard.py",
    "visual_caption_timing.py",
)

_FONT_EXTENSIONS = frozenset({".ttf", ".otf", ".ttc"})
_MAX_BUNDLED_FONT_BYTES = 25 * 1024 * 1024


@lru_cache(maxsize=8)
def _resolve_local_font_files_for_bundle(sub_font_name: str = "Arial") -> Tuple[Tuple[str, Path], ...]:
    """Locate local subtitle TrueType/OpenType files so Colab mirrors Tool V1's exact host fonts."""
    selected: List[Tuple[str, Path]] = []
    seen_names: set[str] = set()

    def _add_font(font_path: Path) -> None:
        try:
            if (
                not font_path.is_file()
                or font_path.is_symlink()
                or font_path.suffix.lower() not in _FONT_EXTENSIONS
            ):
                return
            st_size = font_path.stat().st_size
            if st_size <= 0 or st_size > _MAX_BUNDLED_FONT_BYTES:
                return
        except OSError:
            return
        key = font_path.name.casefold()
        if key in seen_names:
            return
        seen_names.add(key)
        selected.append((f"fonts/{font_path.name}", font_path))

    win_dir = os.environ.get("WINDIR", r"C:\Windows")
    for cand_dir in (
        Path(win_dir) / "Fonts",
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
    ):
        for fname in ("arial.ttf", "arialbd.ttf"):
            _add_font(cand_dir / fname)

    requested = str(sub_font_name or "Arial").strip()
    if requested:
        try:
            try:
                from backend.subtitle_layout import _installed_fonts
            except ImportError:
                from subtitle_layout import _installed_fonts
            req_cf = requested.casefold()
            added_custom = 0
            for fam, _style, raw_fp in _installed_fonts():
                if str(fam).casefold() == req_cf:
                    before = len(selected)
                    _add_font(Path(raw_fp))
                    if len(selected) > before:
                        added_custom += 1
                    if added_custom >= 4:
                        break
        except Exception:
            pass

    return tuple(selected)


def _resolve_gemini_key_and_secrets() -> Tuple[str, Tuple[str, ...]]:
    """Resolve primary Gemini API key and all configured rotation keys (GEMINI_API_KEYS / GEMINI_API_KEY_N)."""
    fpt_key = os.getenv("FPT_API_KEY", "").strip()
    voice_key = os.getenv("VOICE_API_KEY", "").strip()

    def _valid(raw_k: str) -> str:
        k = raw_k.strip().strip('"').strip("'")
        if len(k) < 10 or k in (fpt_key, voice_key) or "fpt" in k.lower():
            return ""
        return k

    pool_keys: List[str] = []
    multi_raw = os.getenv("GEMINI_API_KEYS", "").strip()
    if multi_raw:
        for part in re.split(r"[\s,;]+", multi_raw):
            cleaned = _valid(part)
            if cleaned and cleaned not in pool_keys:
                pool_keys.append(cleaned)

    for env_name in sorted(os.environ):
        if re.match(r"^GEMINI_API_KEY_\d+$", env_name):
            numbered = _valid(os.environ.get(env_name, ""))
            if numbered and numbered not in pool_keys:
                pool_keys.append(numbered)

    primary = _valid(os.getenv("GEMINI_API_KEY", ""))
    all_keys: List[str] = []
    if primary:
        all_keys.append(primary)
        if primary in pool_keys:
            for pk in pool_keys:
                if pk not in all_keys:
                    all_keys.append(pk)
    else:
        all_keys.extend(pool_keys)

    chosen = primary or (all_keys[0] if all_keys else "")
    return chosen, tuple(all_keys)


def _build_gemini_env_vars(gemini_key: str, extra_secrets: Sequence[str]) -> Optional[Dict[str, str]]:
    if not gemini_key:
        return None
    env_vars = {"GEMINI_API_KEY": gemini_key}
    max_keys = max(0, int(DEFAULT_CONFIG.get("cloud_gemini_keys_max", 2)))
    if max_keys > 1:
        remote_keys = [gemini_key]
        remote_keys.extend(key for key in extra_secrets if key and key not in remote_keys)
        remote_keys = remote_keys[:max_keys]
        if len(remote_keys) > 1:
            env_vars["GEMINI_API_KEYS"] = ",".join(remote_keys)
    return env_vars


def _build_pipeline_env_vars(gemini_key: str, extra_secrets: Sequence[str]) -> Optional[Dict[str, str]]:
    env_vars = _build_gemini_env_vars(gemini_key, extra_secrets) or {}
    asr_engine = _configured_cloud_asr_engine()
    if asr_engine and asr_engine != "openai-whisper":
        env_vars["AUTODUB_CLOUD_ASR_ENGINE"] = asr_engine
    return env_vars or None


def _configured_cloud_asr_engine() -> str:
    return (
        str(DEFAULT_CONFIG.get("cloud_asr_engine", "openai-whisper")).strip().lower()
        or "openai-whisper"
    )


def _build_asr_engine_env_vars() -> Dict[str, str]:
    return {"AUTODUB_CLOUD_ASR_ENGINE": _configured_cloud_asr_engine()}


def _file_sha256(path_obj: Path) -> str:
    h = hashlib.sha256()
    with open(path_obj, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _parse_setup_env_result(output_text: str) -> Optional[Dict[str, Any]]:
    for line in reversed((output_text or "").splitlines()):
        stripped = line.strip()
        idx = stripped.find(_SETUP_RESULT_PREFIX)
        if idx >= 0:
            raw_json = stripped[idx + len(_SETUP_RESULT_PREFIX) :].strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _extract_setup_error_code(output_text: str) -> str:
    parsed = _parse_setup_env_result(output_text)
    if parsed:
        code = str(parsed.get("error_code") or "").strip()
        if code in _VALID_ERROR_CODES:
            return code
    lowered = (output_text or "").lower()
    if any(
        tok in lowered
        for tok in (
            "newconnectionerror",
            "nameresolutionerror",
            "temporary failure in name resolution",
            "timed out",
            "readtimeouterror",
            "connectionerror",
            "connection was lost",
        )
    ):
        return CloudErrorCode.ENV_NETWORK_ERROR.value
    if "gpu" in lowered and ("mismatch" in lowered or "cuda" in lowered or "nvidia-smi" in lowered):
        return CloudErrorCode.ENV_GPU_MISMATCH.value
    if "disk" in lowered and ("insufficient" in lowered or "space" in lowered):
        return CloudErrorCode.ENV_DISK_INSUFFICIENT.value
    if "model" in lowered and ("load" in lowered or "checksum" in lowered):
        return CloudErrorCode.ENV_MODEL_LOAD_FAILED.value
    if "upload" in lowered or "bundle" in lowered:
        return CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value
    return CloudErrorCode.ENV_SETUP_FAILED.value


def _parse_json_prefix_line(output_text: str, prefix: str) -> Optional[Dict[str, Any]]:
    for line in reversed((output_text or "").splitlines()):
        stripped = line.strip()
        idx = stripped.find(prefix)
        if idx >= 0:
            raw_json = stripped[idx + len(prefix) :].strip()
            try:
                parsed = json.loads(raw_json)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None


def _parse_pipeline_result(output_text: str) -> Optional[Dict[str, Any]]:
    return _parse_json_prefix_line(output_text, _PIPELINE_RESULT_PREFIX)


def _extract_pipeline_error_code(output_text: str) -> str:
    parsed = _parse_pipeline_result(output_text)
    if parsed:
        code = str(parsed.get("error_code") or "").strip()
        if code in _VALID_ERROR_CODES:
            return code
    return CloudErrorCode.EXEC_FAILED.value


class _OrchestratorStageError(RuntimeError):
    """Internal runtime error preserving structured CloudErrorCode across cleanup/transition."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class _JobAborted(Exception):
    """Internal control-flow exception raised when a job is cancelled, exceeds budget, or loses its lease."""

    def __init__(self, reason: str, *, is_lease_lost: bool = False, is_budget_exceeded: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.is_lease_lost = is_lease_lost
        self.is_budget_exceeded = is_budget_exceeded


def _supports_kwarg(fn: Any, kwarg_name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg_name:
            return True
    return False


def _call_with_cancel(fn: Any, *args: Any, cancel_check: Optional[Callable[[], bool]] = None, **kwargs: Any) -> Any:
    if cancel_check is not None and _supports_kwarg(fn, "cancel_check"):
        kwargs["cancel_check"] = cancel_check
    return fn(*args, **kwargs)


def _build_warm_reset_script(content_root: str = "/content") -> str:
    return f"""import os
from pathlib import Path

root = Path({content_root!r})
root_resolved = root.resolve()
keep={{'.autodub_venv', '.autodub_bootstrap', '.autodub_python', '.autodub_uv_cache', '.autodub_env_state.json', 'model_cache', 'fonts', 'drive'}}

def is_mount_or_nested(path):
    try:
        if os.path.ismount(path):
            return True
        resolved = path.resolve()
    except OSError:
        return True
    if not resolved.is_relative_to(root_resolved):
        return True
    current = resolved
    while current != root_resolved:
        if os.path.ismount(current):
            return True
        current = current.parent
    return False

def remove(path):
    if is_mount_or_nested(path):
        return
    if path.is_symlink() or not path.is_dir():
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    try:
        children = list(path.iterdir())
    except OSError:
        return
    for child in children:
        remove(child)
    if not is_mount_or_nested(path):
        try:
            path.rmdir()
        except OSError:
            pass

for path in root.iterdir():
    if path.name not in keep:
        remove(path)
"""


_WARM_RESET_SCRIPT = _build_warm_reset_script()


def _build_warm_reset_argv() -> List[str]:
    payload = base64.b64encode(_WARM_RESET_SCRIPT.encode("utf-8")).decode("ascii")
    command = f"import base64;exec(base64.b64decode({payload!r}).decode())"
    return ["python3", "-c", command]


_GLOBAL_WARM_LOCK = threading.Lock()
_GLOBAL_WARM_TIMERS: Dict[Tuple[str, str], threading.Timer] = {}
_GLOBAL_ORCHESTRATORS: Dict[str, "weakref.WeakSet[CloudOrchestrator]"] = {}


def _store_scope_key(store: Any) -> str:
    db_path = getattr(store, "db_path", None)
    if db_path:
        try:
            return os.path.normcase(str(Path(db_path).resolve()))
        except OSError:
            return os.path.normcase(str(db_path))
    return f"mem:{id(store)}"


def clear_warm_registry_for_store(store: Any, gpu_type: Optional[str] = None) -> List[str]:
    """Xoá sạch bản ghi warm session & huỷ bộ đếm hẹn giờ thuộc store (dùng sau stop_all_sessions)."""
    scope = _store_scope_key(store)
    pop_all_fn = getattr(store, "pop_all_warm_sessions", None)
    rows = pop_all_fn(gpu_type=gpu_type) if callable(pop_all_fn) else []
    snames = [str(r["session_name"]) for r in rows if r.get("session_name")]
    with _GLOBAL_WARM_LOCK:
        keys = [
            k for k in list(_GLOBAL_WARM_TIMERS.keys())
            if k[0] == scope and (not gpu_type or k[1] in snames)
        ]
        for k in keys:
            tmr = _GLOBAL_WARM_TIMERS.pop(k, None)
            if tmr is not None:
                try:
                    tmr.cancel()
                except Exception:
                    pass
    return snames


@dataclass
class _JobRun:
    orchestrator: Any
    job: CloudJob
    progress_callback: Optional[Callable[[int, str], None]]
    cancel_event: Optional[threading.Event]
    job_id: str
    raw_attempt: int
    session_attempt: int
    effective_owner: Optional[str]
    ownership_attempt: Optional[int]
    session_name: str
    gemini_key: str
    extra_secrets: Sequence[str]
    start_time: float
    last_budget_check_ts: float
    budget_abort_reason: str
    budget_abort_code: str
    rate_per_hour: float
    latest_provider_balance: Optional[float]
    warm_idle_ledgered: bool
    session_may_exist: bool
    cleanup_done: bool
    cleanup_result: Optional[Dict[str, Any]]
    runtime_end_time: Optional[float]
    cu_accrued: float
    cu_accrual_ts: Optional[float]
    provider_rate_used: bool
    provider_rate_eligible: bool
    setup_phase_seconds: Dict[str, Any]
    stage_timings: Dict[str, float]
    stage_state: Dict[str, Any]
    runtime_start_time: Optional[float] = None
    gpu_type: Optional[str] = None
    voice_choice: Optional[str] = None
    validated_input: Any = None
    preflight_info: Dict[str, Any] = field(default_factory=dict)
    source_snapshot: Optional[Dict[str, Any]] = None
    reused_warm_session: Optional[str] = None
    warm_parked_at: Optional[float] = None
    is_url: bool = False
    input_arg: Optional[str] = None
    use_monitored_exec: bool = False
    attempt_res_dir: Optional[Path] = None
    dl_path: Optional[str] = None
    qc_res: Optional[Dict[str, Any]] = None
    parked_warm: bool = False
    final_video_path: Optional[Path] = None
    output_manifest: Dict[str, Any] = field(default_factory=dict)
    cu_spent: float = 0.0
    elapsed_sec: float = 0.0
    cache_backend: str = "none"
    cache_mounted: bool = False
    cache_status: str = "disabled"

    @classmethod
    def from_job(
        cls,
        orchestrator: Any,
        job: CloudJob,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        *,
        owner_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> "_JobRun":
        job_id = job.job_id
        raw_attempt = int(getattr(job, "attempt", 0) or 0)
        session_attempt = raw_attempt if raw_attempt > 0 else 1
        effective_owner = owner_id if owner_id is not None else getattr(job, "owner_id", None)
        ownership_attempt = raw_attempt if raw_attempt > 0 else None

        clean_id = re.sub(r"[^a-zA-Z0-9]", "", job_id)[-8:] or uuid.uuid4().hex[:6]
        session_name = validate_session_name(f"cjob{clean_id}a{session_attempt}")
        gemini_key, extra_secrets = _resolve_gemini_key_and_secrets()
        start_time = time.time()
        runtime_start_time: Optional[float] = None
        last_budget_check_ts = start_time
        budget_abort_reason = ""
        budget_abort_code = ""
        rate_per_hour = GPU_CU_RATES.get("T4", 1.15)
        latest_provider_balance: Optional[float] = None
        warm_idle_ledgered = False

        session_may_exist = False
        cleanup_done = False
        cleanup_result: Optional[Dict[str, Any]] = None
        runtime_end_time: Optional[float] = None

        cu_accrued = 0.0
        cu_accrual_ts: Optional[float] = None
        provider_rate_used = False
        provider_rate_eligible = False
        setup_phase_seconds: Dict[str, Any] = {}
        stage_timings: Dict[str, float] = {}
        stage_state: Dict[str, Any] = {"name": None, "started": None}
        return cls(
            orchestrator=orchestrator, job=job, progress_callback=progress_callback, cancel_event=cancel_event,
            job_id=job_id, raw_attempt=raw_attempt, session_attempt=session_attempt,
            effective_owner=effective_owner, ownership_attempt=ownership_attempt, session_name=session_name,
            gemini_key=gemini_key, extra_secrets=extra_secrets, start_time=start_time,
            last_budget_check_ts=last_budget_check_ts, budget_abort_reason=budget_abort_reason,
            budget_abort_code=budget_abort_code, rate_per_hour=rate_per_hour,
            latest_provider_balance=latest_provider_balance, warm_idle_ledgered=warm_idle_ledgered,
            session_may_exist=session_may_exist, cleanup_done=cleanup_done, cleanup_result=cleanup_result,
            runtime_end_time=runtime_end_time, cu_accrued=cu_accrued, cu_accrual_ts=cu_accrual_ts,
            provider_rate_used=provider_rate_used, provider_rate_eligible=provider_rate_eligible,
            setup_phase_seconds=setup_phase_seconds, stage_timings=stage_timings, stage_state=stage_state,
        )

    def _mark_stage(self, name: Optional[str]) -> None:
        now_m = time.monotonic()
        prev, started = self.stage_state["name"], self.stage_state["started"]
        if prev is not None and started is not None:
            self.stage_timings[prev] = round(self.stage_timings.get(prev, 0.0) + (now_m - started), 3)
        self.stage_state["name"], self.stage_state["started"] = name, (now_m if name else None)

    def _raw_elapsed_cu(self, at_ts: Optional[float] = None) -> float:
        if not self.session_may_exist or self.runtime_start_time is None:
            return 0.0
        end_ts = at_ts if at_ts is not None else (self.runtime_end_time if self.runtime_end_time is not None else time.time())
        base_ts = self.cu_accrual_ts if self.cu_accrual_ts is not None else self.runtime_start_time
        return self.cu_accrued + (max(0.0, end_ts - base_ts) / 3600.0) * self.rate_per_hour

    def _estimate_elapsed_cu(self) -> float:
        return round(self._raw_elapsed_cu(), 4)

    def _adopt_provider_rate(self, snap: Dict[str, Any]) -> None:
        if (
            not self.provider_rate_eligible
            or not self.session_may_exist
            or self.runtime_start_time is None
            or self.runtime_end_time is not None
        ):
            return
        now_ts = time.time()
        try:
            candidate_rate = float(snap.get("usage_rate"))
        except (TypeError, ValueError):
            candidate_rate = 0.0
        rate_is_valid = math.isfinite(candidate_rate) and candidate_rate > 0.0
        if snap.get("active_assignments") == 1 and rate_is_valid:
            self.cu_accrued = self._raw_elapsed_cu(now_ts)
            self.cu_accrual_ts = now_ts
            self.rate_per_hour = candidate_rate
            self.provider_rate_used = True
            return
        if self.provider_rate_used:
            self.cu_accrued = self._raw_elapsed_cu(now_ts)
            self.cu_accrual_ts = now_ts
            self.rate_per_hour = GPU_CU_RATES.get(self.gpu_type, 1.15)

    def _cu_measurement_source(self) -> str:
        if self.provider_rate_used:
            return CloudCostMeasurementSource.PROVIDER_RATE_ESTIMATE.value
        return CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value

    def _sync_runtime_usage(self, *, check_provider: bool = False) -> Tuple[bool, str]:
        if not hasattr(self.orchestrator.store, "record_runtime_usage"):
            return True, "ok"
        est_cu = self._estimate_elapsed_cu()
        prov_bal: Optional[float] = None
        if check_provider:
            try:
                u = self.orchestrator.adapter.get_usage()
                if hasattr(self.orchestrator.store, "record_provider_usage_snapshot"):
                    snap = self.orchestrator.store.record_provider_usage_snapshot(u)
                    if not snap.get("ok") or snap.get("is_stale"):
                        self.budget_abort_reason = (
                            "Mất kết nối hoặc số dư Colab bị cũ trong lúc đang chạy session."
                        )
                        self.budget_abort_code = "budget_uncertain"
                        return False, "budget_uncertain"
                    prov_bal = snap.get("balance")
                    if prov_bal is not None:
                        self.latest_provider_balance = float(prov_bal)
                    self._adopt_provider_rate(snap)
                elif u.get("ok") and u.get("balance") is not None:
                    prov_bal = float(u["balance"])
                    self.latest_provider_balance = prov_bal
            except Exception as probe_exc:
                self.budget_abort_reason = redact_secrets(str(probe_exc), extra_secrets=self.extra_secrets)
                self.budget_abort_code = "budget_uncertain"
                return False, "budget_uncertain"

        res = self.orchestrator.store.record_runtime_usage(
            self.job_id,
            self.session_attempt,
            est_cu,
            measurement_source=self._cu_measurement_source(),
            provider_balance=prov_bal,
        )
        if not res.get("within_budget", True):
            self.budget_abort_reason = (
                "Đã vượt hạn mức CU trong ngày hoặc hết số dư trong khi đang chạy."
            )
            self.budget_abort_code = "budget_exceeded"
            return False, "budget_exceeded"
        return True, "ok"

    def _check_abort(self) -> Tuple[bool, str]:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True, "cancel_event"
        lost, reason = self.orchestrator.store.is_cancel_or_lease_lost(
            self.job_id,
            owner_id=self.effective_owner,
            attempt=self.ownership_attempt,
        )
        if lost:
            return True, reason
        if self.budget_abort_code:
            return True, self.budget_abort_code

        now_ts = time.time()
        interval = max(0.01, float(DEFAULT_CONFIG.get("runtime_budget_check_interval_seconds", 15)))
        if self.session_may_exist and (now_ts - self.last_budget_check_ts) >= interval:
            self.last_budget_check_ts = now_ts
            ok_budget, b_reason = self._sync_runtime_usage(check_provider=True)
            if not ok_budget:
                return True, b_reason
        return False, "ok"

    def _cancel_probe(self) -> bool:
        aborted, _ = self._check_abort()
        return aborted

    def _raise_if_aborted(self) -> None:
        aborted, reason = self._check_abort()
        if aborted:
            is_lost = reason in _LEASE_LOST_REASONS
            is_budget = reason in ("budget_exceeded", "budget_uncertain")
            raise _JobAborted(
                reason,
                is_lease_lost=is_lost,
                is_budget_exceeded=is_budget,
            )

    def _upload_with_retry(self, local_path: str | Path, remote_filename: str) -> Tuple[bool, str]:
        retry_delays = DEFAULT_CONFIG.get("cloud_upload_retry_delays_seconds", [5, 15])
        try:
            delays = [max(0.0, float(delay)) for delay in retry_delays][:2]
        except (TypeError, ValueError):
            delays = [5.0, 15.0]
        for attempt_idx in range(len(delays) + 1):
            self._raise_if_aborted()
            if _is_session_aborted(self.session_name):
                self._raise_if_aborted()
                raise _JobAborted("cancel_event")
            up_ok, up_msg = _call_with_cancel(
                self.orchestrator.adapter.upload_file,
                self.session_name,
                local_path,
                remote_filename,
                cancel_check=self._cancel_probe,
            )
            self._raise_if_aborted()
            if up_ok:
                return True, up_msg
            failure = str(up_msg or "").lower()
            if (
                attempt_idx >= len(delays)
                or _is_session_aborted(self.session_name)
                or any(token in failure for token in ("cancel", "abort", "hủy", "huy"))
            ):
                return False, up_msg
            if _wait_interruptible(
                delays[attempt_idx],
                session_name=self.session_name,
                cancel_check=self._cancel_probe,
            ):
                self._raise_if_aborted()
                raise _JobAborted("cancel_event")
        return False, up_msg

    def _update(self, percent: int, step_name: str, **kwargs) -> bool:
        safe_step = redact_secrets(step_name, extra_secrets=self.extra_secrets)
        if "error" in kwargs and kwargs["error"] is not None:
            kwargs["error"] = redact_secrets(str(kwargs["error"]), extra_secrets=self.extra_secrets)
        if self.effective_owner is not None and "owner_id" not in kwargs:
            kwargs["owner_id"] = self.effective_owner
        if self.ownership_attempt is not None and "attempt" not in kwargs:
            kwargs["attempt"] = self.ownership_attempt
        ok = self.orchestrator.store.update_progress(self.job_id, percent=percent, step_name=safe_step, **kwargs)
        if ok and self.progress_callback:
            try:
                self.progress_callback(percent, safe_step)
            except Exception:
                pass
        return ok

    def _finalize_session_once(self) -> Dict[str, Any]:
        if self.cleanup_done:
            return dict(self.cleanup_result or {"verified_stopped": False,
                                           "cleanup_status": "failed_retry_needed",
                                           "error": "Cleanup did not return a verified result"})
        if not self.session_may_exist:
            return {
                "verified_stopped": True,
                "cleanup_status": "verified",
                "attempts": 0,
                "error": None,
            }
        self.cleanup_done = True
        logger.info("Dọn dẹp: Dừng và xác nhận session Colab %s...", self.session_name)
        res = self.orchestrator._verify_or_stop_session(self.session_name, extra_secrets=self.extra_secrets)
        self.cleanup_result = dict(res)
        if res.get("verified_stopped"):
            self.runtime_end_time = time.time()
        # The VM can consume CU while the provider is stopping it. Record
        # that interval before reconciliation freezes the cost ledger.
        self._sync_runtime_usage(check_provider=False)
        safe_cleanup_err = (
            redact_secrets(str(res.get("error")), extra_secrets=self.extra_secrets)
            if res.get("error")
            else None
        )
        self.orchestrator.store.record_cleanup_result(
            self.job_id,
            self.raw_attempt,
            self.session_name,
            verified_stopped=bool(res.get("verified_stopped")),
            cleanup_status=str(res.get("cleanup_status") or "failed_retry_needed"),
            attempts=int(res.get("attempts") or 1),
            error=safe_cleanup_err,
        )
        if not res.get("verified_stopped"):
            logger.error(
                "Cảnh báo: Chưa xác nhận dừng session %s (%s): %s",
                self.session_name,
                res.get("cleanup_status"),
                safe_cleanup_err,
            )
        return res

    def _stage_preflight(self):
        self._mark_stage("preflight")
        # 0. Kiểm tra allowlist GPU/task/voice, đầu vào và bundle cục bộ (0 CU) trước khi cấp phát GPU
        self.preflight_info: Dict[str, Any] = {}
        self.source_snapshot: Optional[Dict[str, Any]] = None
        try:
            self.gpu_type = validate_gpu_type(self.job.gpu_type)
            validate_task_type(getattr(self.job, "task_type", "full_pipeline"))
            self.voice_choice = validate_voice(getattr(self.job, "voice", "BV562_streaming") or "BV562_streaming")
            self.validated_input = validate_video_input(
                self.job.video_path,
                allowed_roots=self.orchestrator.allowed_roots,
                require_exists=True,
            )
            self.preflight_info = self.orchestrator.verify_local_preflight(self.validated_input)
            self.source_snapshot = self.preflight_info.get("source_snapshot")
            self.rate_per_hour = GPU_CU_RATES.get(self.gpu_type, 1.15)
        except Exception as val_exc:
            safe_err = redact_secrets(str(val_exc), extra_secrets=self.extra_secrets)
            logger.warning("Từ chối Cloud Job %s ở bước preflight cục bộ: %s", self.job_id, safe_err)
            if isinstance(val_exc, CloudValidationError):
                err_code = val_exc.code
            elif isinstance(val_exc, _OrchestratorStageError):
                err_code = val_exc.error_code
            else:
                err_code = CloudErrorCode.INVALID_INPUT.value
            self._update(
                0,
                f"Lỗi tiền kiểm cục bộ: {safe_err[:100]}",
                state="failed",
                error=safe_err,
                error_code=err_code,
                cu_actual=0.0,
            )
            return False

        logger.info(
            "=== BẮT ĐẦU XỬ LÝ CLOUD JOB %s (Attempt=%d, Session=%s, Video=%s, GPU=%s) ===",
            self.job_id,
            self.session_attempt,
            self.session_name,
            self.validated_input.display_name,
            self.gpu_type,
        )

        self._raise_if_aborted()

        return True

    def _stage_budget_check(self):
        self._mark_stage("budget_check")
        # 1. Kiểm tra số dư Compute Units, độ mới snapshot và ngân sách trước khi cấp phát máy
        usage = self.orchestrator.adapter.get_usage()
        if hasattr(self.orchestrator.store, "record_provider_usage_snapshot"):
            snap = self.orchestrator.store.record_provider_usage_snapshot(usage)
        else:
            snap = {
                "ok": bool(usage.get("ok")),
                "balance": usage.get("balance"),
                "is_stale": bool(usage.get("stale", False)),
                "error": usage.get("error"),
            }

        if not snap.get("ok") or snap.get("is_stale"):
            err = redact_secrets(
                f"Không thể xác nhận số dư Colab mới ({snap.get('error') or 'stale/disconnected'}). Chặn mở GPU.",
                extra_secrets=self.extra_secrets,
            )
            self._update(
                0,
                "Lỗi: Chưa xác nhận được số dư Colab",
                state="failed",
                error=err,
                error_code=CloudErrorCode.BUDGET_UNCERTAIN.value,
            )
            return False

        balance = snap.get("balance")
        required_bal = max(
            0.5,
            float(getattr(self.job, "cu_reserved", 0.0) or getattr(self.job, "cu_estimated", 0.0) or 0.5),
            float(DEFAULT_CONFIG.get("min_provider_balance_cu", 0.0)),
        )
        if balance is not None and float(balance) < required_bal - 1e-6:
            err = f"Số dư Colab không đủ (còn {float(balance):.2f} < {required_bal:.2f} CU). Vui lòng nạp thêm CU."
            self._update(
                0,
                "Lỗi: Hết Compute Units",
                state="failed",
                error=err,
                error_code=CloudErrorCode.INSUFFICIENT_CU.value,
            )
            return False
        if balance is not None:
            self.latest_provider_balance = float(balance)

        budget_info = self.orchestrator.store.get_cu_budget_info()
        if int(budget_info.get("unreconciled_count", 0)) > 0:
            err = "Còn phiên GPU trước chưa xác nhận thu hồi/đối soát chi phí; tạm chặn cấp phát mới."
            logger.warning(err)
            self._update(
                0,
                "Lỗi: Chờ đối soát chi phí phiên trước",
                state="failed",
                error=err,
                error_code=CloudErrorCode.BUDGET_UNCERTAIN.value,
            )
            return False

        budget_daily = float(budget_info["budget_daily"])
        over_daily = (
            budget_daily <= 0.0
            or float(budget_info["spent_today"]) >= budget_daily
            or float(budget_info.get("committed_today", budget_info["spent_today"])) > budget_daily + 1e-6
        )
        if over_daily:
            err = (
                f"Đã chạm hạn mức CU trong ngày "
                f"({budget_info.get('committed_today', budget_info['spent_today']):.2f} / {budget_daily:.2f} CU). "
                f"Vui lòng cập nhật hạn mức trên Dashboard hoặc đợi sang ngày mới để bảo vệ tài khoản!"
            )
            logger.warning(err)
            self._update(
                0,
                "Lỗi: Đã chạm hạn mức CU hôm nay",
                state="failed",
                error=err,
                error_code=CloudErrorCode.BUDGET_EXCEEDED.value,
            )
            return False

        self._raise_if_aborted()

        return True

    def _stage_provision(self):
        self._mark_stage("provisioning")
        # 2. Đăng ký ownership session & khởi tạo (hoặc tái sử dụng máy giữ ấm) Session Colab (`provisioning`)
        warm_entry = self.orchestrator._pop_warm_session_entry(self.gpu_type)
        self.reused_warm_session = warm_entry[0] if warm_entry else None
        self.warm_parked_at = warm_entry[1] if warm_entry else None
        if self.reused_warm_session:
            self.session_name = self.reused_warm_session
            if self.warm_parked_at is not None:
                record_warm_idle = getattr(self.orchestrator.store, "record_warm_idle_usage", None)
                if callable(record_warm_idle):
                    try:
                        record_warm_idle(
                            self.session_name,
                            self.gpu_type,
                            parked_at=self.warm_parked_at,
                            ended_at=time.time(),
                        )
                        self.warm_idle_ledgered = True
                    except Exception as exc:
                        logger.warning(
                            "Không ghi được CU giữ ấm khi tái sử dụng %s: %s",
                            self.session_name,
                            redact_secrets(str(exc)),
                        )

        reg_ok = self.orchestrator.store.register_session(self.job_id, self.raw_attempt, self.effective_owner, self.session_name)
        if self.ownership_attempt is not None and not reg_ok:
            if self.reused_warm_session:
                try:
                    self.orchestrator._verify_or_stop_session(self.reused_warm_session, extra_secrets=self.extra_secrets)
                except Exception:
                    pass
            self._raise_if_aborted()
            raise _JobAborted("attempt_mismatch", is_lease_lost=True)

        prov_msg = (
            f"Tái sử dụng máy ảo GPU {self.gpu_type} đang giữ ấm ({self.session_name})..."
            if self.reused_warm_session
            else f"Đang tạo máy ảo GPU {self.gpu_type} trên Colab..."
        )
        if not self._update(
            10,
            prov_msg,
            state="provisioning",
            session_name=self.session_name,
        ):
            if self.reused_warm_session:
                try:
                    self.orchestrator._verify_or_stop_session(self.reused_warm_session, extra_secrets=self.extra_secrets)
                except Exception:
                    pass
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        self.session_may_exist = True
        self.runtime_start_time = time.time()
        if self.reused_warm_session:
            created, msg = True, f"reused warm session {self.session_name}"
        else:
            created, msg = _call_with_cancel(
                self.orchestrator.adapter.create_session,
                self.session_name,
                gpu=self.gpu_type,
                cancel_check=self._cancel_probe,
            )

        self.provider_rate_eligible = bool(created)
        # Đóng race hủy trong lúc create_session: nếu hủy trong/sau khi tạo thì thu hồi ngay, không upload/chạy pipeline.
        aborted_after_create, abort_reason = self._check_abort()
        if aborted_after_create:
            raise _JobAborted(
                abort_reason,
                is_lease_lost=(abort_reason in _LEASE_LOST_REASONS),
                is_budget_exceeded=(abort_reason in ("budget_exceeded", "budget_uncertain")),
            )

        if not created:
            self.session_may_exist = False
            err_code = (
                CloudErrorCode.PROVIDER_503.value
                if "503" in str(msg) or "hết máy" in str(msg).lower()
                else CloudErrorCode.SESSION_CREATE_FAILED.value
            )
            self._update(
                0,
                "Lỗi: Tạo máy ảo Colab thất bại",
                state="failed",
                error=f"Không thể tạo máy Colab: {msg}",
                error_code=err_code,
                cleanup_status="verified",
            )
            self.orchestrator.store.record_cleanup_result(
                self.job_id,
                self.raw_attempt,
                self.session_name,
                verified_stopped=True,
                cleanup_status="verified",
                attempts=0,
                error=None,
            )
            return False

        return True

    def _stage_upload(self):
        self._mark_stage("upload")
        # 3. Upload mã nguồn và video lên VM (`uploading` đơn hướng, không lùi từ preparing)
        self.is_url = self.validated_input.kind == "url"
        if not self._update(
            20,
            "Đang đóng gói và tải mã nguồn AI lên máy ảo Colab...",
            state="uploading",
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        bundle_path = Path(tempfile.gettempdir()) / f"bundle_{self.session_name}_{uuid.uuid4().hex[:6]}.tar.gz"
        try:
            upload_targets: List[Path] = list(self.preflight_info.get("upload_targets") or [])
            runtime_profile = self.orchestrator._build_v1_runtime_profile(self.validated_input)
            bundled_fonts = _resolve_local_font_files_for_bundle(
                str((runtime_profile.get("canvas") or {}).get("sub_font_name") or "Arial")
            )
            runtime_profile_bytes = json.dumps(runtime_profile, indent=2, ensure_ascii=False).encode("utf-8")
            runtime_profile_sha = hashlib.sha256(runtime_profile_bytes).hexdigest()
            bundle_manifest_data: Dict[str, Any] = {
                "schema_version": 1,
                "job_id": self.job_id,
                "attempt": self.session_attempt,
                "expected_gpu": self.gpu_type,
                "files": [
                    *[
                        {
                            "name": s_file.name,
                            "size_bytes": s_file.stat().st_size,
                            "sha256": _file_sha256(s_file),
                        }
                        for s_file in upload_targets
                    ],
                    *[
                        {
                            "name": arc_rel,
                            "size_bytes": f_path.stat().st_size,
                            "sha256": _file_sha256(f_path),
                        }
                        for arc_rel, f_path in bundled_fonts
                    ],
                    {
                        "name": "v1_runtime_profile.json",
                        "size_bytes": len(runtime_profile_bytes),
                        "sha256": runtime_profile_sha,
                    },
                ],
            }
            if not self.is_url and self.source_snapshot:
                bundle_manifest_data["expected_video"] = {
                    "remote_filename": sanitize_remote_filename(self.validated_input.display_name),
                    "size_bytes": self.source_snapshot["size_bytes"],
                    "sha256": self.source_snapshot["sha256"],
                }

            bundle_manifest_bytes = json.dumps(bundle_manifest_data, indent=2, ensure_ascii=False).encode("utf-8")
            now_mtime = int(time.time())
            with tarfile.open(bundle_path, "w:gz") as tar:
                for s_file in upload_targets:
                    tar.add(s_file, arcname=s_file.name)
                for arc_rel, f_path in bundled_fonts:
                    tar.add(f_path, arcname=arc_rel)
                profile_tarinfo = tarfile.TarInfo(name="v1_runtime_profile.json")
                profile_tarinfo.size = len(runtime_profile_bytes)
                profile_tarinfo.mtime = now_mtime
                tar.addfile(profile_tarinfo, io.BytesIO(runtime_profile_bytes))
                manifest_tarinfo = tarfile.TarInfo(name="bundle_manifest.json")
                manifest_tarinfo.size = len(bundle_manifest_bytes)
                manifest_tarinfo.mtime = now_mtime
                tar.addfile(manifest_tarinfo, io.BytesIO(bundle_manifest_bytes))

            self._raise_if_aborted()
            up_ok, up_msg = self._upload_with_retry(bundle_path, "cloud_bundle.tar.gz")
            self._raise_if_aborted()
            if not up_ok:
                raise _OrchestratorStageError(
                    CloudErrorCode.UPLOAD_FAILED.value,
                    f"Upload mã nguồn lên Colab thất bại: {up_msg}",
                )
        finally:
            bundle_path.unlink(missing_ok=True)

        if self.is_url:
            self._update(
                30,
                f"Đã nhận link: {self.validated_input.display_name}. Colab sẽ tải video trực tiếp trên cloud...",
                state="uploading",
            )
            self.input_arg = self.validated_input.normalized_value
        else:
            self._update(
                30,
                f"Đang tải video {self.validated_input.display_name} lên máy ảo...",
                state="uploading",
            )
            remote_video_name = sanitize_remote_filename(self.validated_input.display_name)
            self._raise_if_aborted()
            v_ok, v_msg = self._upload_with_retry(self.validated_input.normalized_value, remote_video_name)
            self._raise_if_aborted()
            if not v_ok:
                raise _OrchestratorStageError(
                    CloudErrorCode.UPLOAD_FAILED.value,
                    f"Upload video thất bại: {v_msg}",
                )
            src_check = Path(self.validated_input.normalized_value)
            if self.source_snapshot and (
                not src_check.is_file()
                or src_check.stat().st_size != self.source_snapshot["size_bytes"]
                or _file_sha256(src_check) != self.source_snapshot["sha256"]
            ):
                raise _OrchestratorStageError(
                    CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value,
                    "Phát hiện file video nguồn cục bộ bị thay đổi hoặc mất sau bước upload.",
                )
            self.input_arg = f"/content/{remote_video_name}"

    def _stage_setup(self):
        self._mark_stage("setup")
        # 4. Giải nén và cài đặt môi trường trên VM (`preparing`)
        if not self._update(
            40,
            "Đang giải nén và chuẩn bị môi trường AI (Demucs, Whisper Large-v3-turbo, CapCut TTS)...",
            state="preparing",
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        tar_ok, tar_msg = _call_with_cancel(
            self.orchestrator.adapter.exec_argv,
            self.session_name,
            ["tar", "-xzf", "/content/cloud_bundle.tar.gz", "-C", "/content"],
            cwd="/content",
            cancel_check=self._cancel_probe,
        )
        self._raise_if_aborted()
        if not tar_ok:
            raise _OrchestratorStageError(
                CloudErrorCode.UPLOAD_VERIFICATION_FAILED.value,
                f"Giải nén mã nguồn trên Colab thất bại: {tar_msg}",
            )

        self.cache_backend = str(DEFAULT_CONFIG.get("cloud_cache_backend", "none")).strip().lower()
        if self.cache_backend not in {"none", "gdrive"}:
            self.cache_backend = "none"
        self.cache_mounted = False
        self.cache_status = "disabled"
        if self.cache_backend == "gdrive" and not self.reused_warm_session:
            mount_drive = getattr(self.orchestrator.adapter, "mount_drive", None)
            if callable(mount_drive):
                try:
                    mount_ok, mount_msg = mount_drive(
                        self.session_name,
                        "/content/drive",
                        timeout=int(DEFAULT_CONFIG.get("cloud_drive_mount_timeout_seconds", 180) or 180),
                        cancel_check=self._cancel_probe,
                    )
                except Exception:
                    mount_ok, mount_msg = False, ""
                if mount_ok:
                    self.cache_mounted = True
                else:
                    reason = (
                        "cần cấp quyền Google Drive một lần trong WSL"
                        if "authorization needed" in str(mount_msg).lower()
                        or "ủy quyền" in str(mount_msg).lower()
                        else "gắn Drive thất bại"
                    )
                    logger.warning(
                        "Không gắn được Google Drive cho session %s (%s); tiếp tục setup không cache.",
                        self.session_name,
                        reason,
                    )
            else:
                logger.warning(
                    "Adapter không hỗ trợ gắn Google Drive cho session %s; tiếp tục setup không cache.",
                    self.session_name,
                )
            self._raise_if_aborted()

        setup_timeout = int(DEFAULT_CONFIG.get("setup_timeout_seconds", 600) or 600)
        min_disk_mb = int(DEFAULT_CONFIG.get("min_remote_disk_mb", 4096) or 4096)
        setup_argv = [
            "python3",
            "setup_env.py",
            "--expected-gpu",
            self.gpu_type,
            "--min-disk-mb",
            str(min_disk_mb),
            "--verify-bundle",
            "bundle_manifest.json",
            "--pip-timeout",
            str(min(300, setup_timeout)),
        ]
        if self.cache_mounted:
            cache_root = str(
                DEFAULT_CONFIG.get(
                    "cloud_cache_drive_root",
                    "/content/drive/MyDrive/AutoDubCloudCache",
                )
                or "/content/drive/MyDrive/AutoDubCloudCache"
            )
            setup_argv.extend(["--cache-dir", cache_root])
            get_setting = getattr(self.orchestrator.store, "get_setting", None)
            if callable(get_setting):
                trusted_cache_key = str(
                    get_setting("drive_cache_latest_key", "") or ""
                ).strip()
                if re.fullmatch(r"[0-9a-f]{64}", trusted_cache_key):
                    trusted_sha256 = str(
                        get_setting(f"drive_cache_sha256:{trusted_cache_key}", "") or ""
                    ).strip().lower()
                    if re.fullmatch(r"[0-9a-f]{64}", trusted_sha256):
                        setup_argv.extend(
                            ["--cache-expected-sha256", trusted_sha256]
                        )
            if not DEFAULT_CONFIG.get("cloud_cache_save_enabled", True):
                setup_argv.append("--no-cache-save")
        setup_env_vars = _build_asr_engine_env_vars()
        self.use_monitored_exec = isinstance(self.orchestrator.adapter, ColabProviderAdapter)
        if self.use_monitored_exec:
            env_ok, env_msg = self.orchestrator.adapter.run_argv_monitored(
                self.session_name,
                setup_argv,
                cwd="/content",
                env_vars=setup_env_vars,
                timeout=setup_timeout,
                cancel_check=self._cancel_probe,
                job_tag="setup",
            )
        else:
            env_ok, env_msg = _call_with_cancel(
                self.orchestrator.adapter.exec_argv,
                self.session_name,
                setup_argv,
                cwd="/content",
                env_vars=setup_env_vars,
                timeout=setup_timeout,
                cancel_check=self._cancel_probe,
            )
        self._raise_if_aborted()
        safe_env_msg = redact_secrets(env_msg, extra_secrets=self.extra_secrets)
        logger.info("Env setup output:\n%s", safe_env_msg)
        setup_result = _parse_setup_env_result(safe_env_msg) or {}
        self.setup_phase_seconds = setup_result.get("phase_seconds") or {}
        reported_cache_status = setup_result.get("cache_status")
        if reported_cache_status in {
            "restored",
            "saved",
            "miss",
            "invalid",
            "disabled",
            "save_failed",
        }:
            self.cache_status = reported_cache_status
        else:
            self.cache_status = "miss" if self.cache_mounted else "disabled"
        if not env_ok:
            setup_err_code = _extract_setup_error_code(safe_env_msg)
            setup_result = _parse_setup_env_result(safe_env_msg) or {}
            setup_detail = str(setup_result.get("message") or safe_env_msg[-2000:])
            raise _OrchestratorStageError(
                setup_err_code,
                f"Cài đặt môi trường AI trên Colab thất bại ({setup_err_code}):\n{setup_detail}",
            )

    def _stage_pipeline(self):
        self._mark_stage("pipeline")
        # 5. Chạy Pipeline xử lý video chính (`processing`)
        if not self._update(
            55,
            "Đang chạy lồng tiếng chuẩn V1 (Demucs tách âm, Whisper GPU, CapCut TTS, EBU R128)...",
            state="processing",
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        pipeline_argv = [
            "/content/.autodub_venv/bin/python"
            if (self.preflight_info.get("env_manifest") or {}).get("isolated_runtime") else "python3",
            "full_pipeline.py",
            "--input",
            self.input_arg,
            "--voice",
            self.voice_choice,
        ]
        job_opts = getattr(self.job, "options", None) or {}
        if isinstance(job_opts, dict) and job_opts.get("allow_no_speech"):
            pipeline_argv.append("--allow-no-speech")
        env_vars = _build_pipeline_env_vars(self.gemini_key, self.extra_secrets)

        highest_pipe_pct = [55]

        def _on_remote_pipeline_progress(remote_pct: int, remote_msg: str) -> None:
            clamped = max(highest_pipe_pct[0], min(84, int(remote_pct)))
            highest_pipe_pct[0] = clamped
            self._update(clamped, remote_msg, state="processing")

        if self.use_monitored_exec:
            pipe_ok, pipe_output = self.orchestrator.adapter.run_argv_monitored(
                self.session_name,
                pipeline_argv,
                cwd="/content",
                env_vars=env_vars,
                timeout=DEFAULT_CONFIG["exec_timeout_seconds"],
                cancel_check=self._cancel_probe,
                progress_callback=_on_remote_pipeline_progress,
                job_tag="pipeline",
            )
        else:
            pipe_ok, pipe_output = _call_with_cancel(
                self.orchestrator.adapter.exec_argv,
                self.session_name,
                pipeline_argv,
                cwd="/content",
                env_vars=env_vars,
                timeout=DEFAULT_CONFIG["exec_timeout_seconds"],
                cancel_check=self._cancel_probe,
            )
        self._raise_if_aborted()
        safe_pipe_output = redact_secrets(pipe_output, extra_secrets=self.extra_secrets)
        logger.info("Pipeline Colab output:\n%s", safe_pipe_output)
        if not pipe_ok:
            pipe_err_code = _extract_pipeline_error_code(safe_pipe_output)
            pipeline_result = _parse_pipeline_result(safe_pipe_output) or {}
            pipeline_detail = str(pipeline_result.get("error") or safe_pipe_output[-2000:])
            raise _OrchestratorStageError(
                pipe_err_code,
                f"Pipeline Colab thất bại ({pipe_err_code}):\n{pipeline_detail}",
            )

    def _stage_cache_save(self):
        if (
            self.cache_backend != "gdrive"
            or not self.cache_mounted
            or not DEFAULT_CONFIG.get("cloud_cache_save_enabled", True)
            or self.cache_status not in {"miss", "invalid"}
        ):
            return

        self._mark_stage("cache_save")
        cache_root = str(
            DEFAULT_CONFIG.get(
                "cloud_cache_drive_root",
                "/content/drive/MyDrive/AutoDubCloudCache",
            )
            or "/content/drive/MyDrive/AutoDubCloudCache"
        )
        save_timeout = max(
            1,
            min(900, int(DEFAULT_CONFIG.get("cloud_cache_save_timeout_seconds", 900) or 900)),
        )

        def _set_cache_result(status: str) -> None:
            self.cache_status = status
            manifest = getattr(self, "output_manifest", None)
            if isinstance(manifest, dict):
                manifest["cache_status"] = status
                manifest["setup_phase_seconds"] = self.setup_phase_seconds

        def _clear_save_budget_abort() -> None:
            if self.budget_abort_code in {"budget_exceeded", "budget_uncertain"}:
                self.budget_abort_code = ""
                self.budget_abort_reason = ""
            self.last_budget_check_ts = time.time()

        try:
            budget_ok, _budget_reason = self._sync_runtime_usage(check_provider=True)
            budget_info = self.orchestrator.store.get_cu_budget_info()
            budget_daily = float(budget_info.get("budget_daily", 0.0) or 0.0)
            committed_today = float(
                budget_info.get(
                    "committed_today",
                    float(budget_info.get("spent_today", 0.0) or 0.0)
                    + float(budget_info.get("reserved_today", 0.0) or 0.0),
                )
                or 0.0
            )
            save_cu = (save_timeout / 3600.0) * max(0.0, float(self.rate_per_hour))
            budget_ok = budget_ok and budget_daily > 0.0 and (
                committed_today + save_cu <= budget_daily + 1e-6
            )
        except Exception:
            budget_ok = False

        if not budget_ok:
            _clear_save_budget_abort()
            _set_cache_result("save_skipped_budget")
            logger.warning(
                "Bỏ qua lưu Google Drive cache cho session %s vì không đủ ngân sách CU; tiếp tục công việc.",
                self.session_name,
            )
            return

        save_argv = [
            "python3",
            "setup_env.py",
            "--cache-save-only",
            "--cache-dir",
            cache_root,
            "--manifest-out",
            "cache_save_result.json",
        ]
        try:
            if self.use_monitored_exec:
            save_ok, save_output = self.orchestrator.adapter.run_argv_monitored(
                self.session_name,
                save_argv,
                cwd="/content",
                env_vars=_build_asr_engine_env_vars(),
                timeout=save_timeout,
                cancel_check=self._cancel_probe,
                job_tag="cache_save",
                )
            else:
                save_ok, save_output = _call_with_cancel(
                    self.orchestrator.adapter.exec_argv,
                self.session_name,
                save_argv,
                cwd="/content",
                env_vars=_build_asr_engine_env_vars(),
                timeout=save_timeout,
                cancel_check=self._cancel_probe,
            )
            self._raise_if_aborted()
        except _JobAborted as abort_exc:
            if not abort_exc.is_budget_exceeded:
                raise
            _clear_save_budget_abort()
            _set_cache_result("save_skipped_budget")
            logger.warning(
                "Bỏ qua lưu Google Drive cache cho session %s do chạm hạn mức CU; tiếp tục công việc.",
                self.session_name,
            )
            return
        except Exception:
            if self.budget_abort_code in {"budget_exceeded", "budget_uncertain"}:
                _clear_save_budget_abort()
                _set_cache_result("save_skipped_budget")
            else:
                _set_cache_result("save_failed")
            logger.warning(
                "Không lưu được Google Drive cache cho session %s; tiếp tục công việc.",
                self.session_name,
            )
            return

        try:
            safe_output = redact_secrets(save_output, extra_secrets=self.extra_secrets)
            save_result = _parse_setup_env_result(safe_output) or {}
            save_phases = save_result.get("phase_seconds")
            if isinstance(save_phases, dict) and "cache_save" in save_phases:
                self.setup_phase_seconds = dict(self.setup_phase_seconds or {})
                self.setup_phase_seconds["cache_save"] = save_phases["cache_save"]
        except Exception:
            _set_cache_result("save_failed")
            logger.warning(
                "Không xử lý được kết quả Google Drive cache cho session %s; tiếp tục công việc.",
                self.session_name,
            )
            return
        if save_ok and save_result.get("cache_status") == "saved":
            cache_key = str(save_result.get("cache_key") or "").strip()
            cache_sha256 = str(save_result.get("cache_sha256") or "").strip().lower()
            set_setting = getattr(self.orchestrator.store, "set_setting", None)
            if (
                re.fullmatch(r"[0-9a-f]{64}", cache_key)
                and re.fullmatch(r"[0-9a-f]{64}", cache_sha256)
                and callable(set_setting)
            ):
                try:
                    set_setting(f"drive_cache_sha256:{cache_key}", cache_sha256)
                    set_setting("drive_cache_latest_key", cache_key)
                except Exception:
                    _set_cache_result("save_failed")
                    logger.warning(
                        "Không thể ghi dấu tin cậy Google Drive cache cho session %s; tiếp tục công việc.",
                        self.session_name,
                    )
                    return
            else:
                _set_cache_result("save_failed")
                logger.warning(
                    "Không nhận được SHA-256 cache hợp lệ cho session %s; tiếp tục công việc.",
                    self.session_name,
                )
                return
            _set_cache_result("saved")
            return

        _set_cache_result("save_failed")
        logger.warning(
            "Không lưu được Google Drive cache cho session %s; tiếp tục công việc.",
            self.session_name,
        )

    def _stage_download(self):
        self._mark_stage("download")
        # 6. Tải video thành phẩm và kết quả về vùng staging riêng theo job/attempt (`downloading`)
        if not self._update(
            85,
            "Đang tải video thành phẩm về máy tính...",
            state="downloading",
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        job_res_dir = Path(DEFAULT_CONFIG["results_dir"]) / self.job_id
        self.attempt_res_dir = job_res_dir / f"attempt_{self.session_attempt}"
        self.attempt_res_dir.mkdir(parents=True, exist_ok=True)

        self._raise_if_aborted()
        dl_ok, self.dl_path = _call_with_cancel(
            self.orchestrator.adapter.download_file,
            self.session_name,
            "output_video.mp4",
            self.attempt_res_dir,
            cancel_check=self._cancel_probe,
        )
        self._raise_if_aborted()
        if not dl_ok:
            raise _OrchestratorStageError(
                CloudErrorCode.DOWNLOAD_FAILED.value,
                "Không tải được video thành phẩm từ Colab về.",
            )

        for remote_art in ("result_metadata.json", "subtitles.srt", "subtitles_vi.srt", "subtitles_vi.ass"):
            if remote_art in _DOWNLOADABLE_REMOTE_ARTIFACTS:
                _call_with_cancel(
                    self.orchestrator.adapter.download_file,
                    self.session_name,
                    remote_art,
                    self.attempt_res_dir,
                    cancel_check=self._cancel_probe,
                )
        self._raise_if_aborted()

    def _unmount_drive_before_warm_park(self) -> bool:
        exec_fn = getattr(self.orchestrator.adapter, "exec_argv", None)
        if not callable(exec_fn):
            logger.warning("Không tháo được Google Drive khỏi session %s trước khi giữ ấm.", self.session_name)
            return False
        try:
            unmount_ok, unmount_msg = _call_with_cancel(
                exec_fn,
                self.session_name,
                [
                    "python3",
                    "-c",
                    "from google.colab import drive; drive.flush_and_unmount()",
                ],
                cwd="/content",
                timeout=30,
                cancel_check=self._cancel_probe,
            )
        except Exception as exc:
            unmount_ok, unmount_msg = False, str(exc)
        if not unmount_ok:
            logger.warning(
                "Không tháo được Google Drive khỏi session %s trước khi giữ ấm; session sẽ được thu hồi: %s",
                self.session_name,
                redact_secrets(str(unmount_msg), extra_secrets=self.extra_secrets),
            )
            return False
        return True

    def _stage_validate(self):
        self._mark_stage("validate")
        # 7. Kiểm tra QC tại vùng staging & xuất thành phẩm atomic an toàn (`validating`)
        self._raise_if_aborted()
        if not self._update(
            95,
            "Đang kiểm tra QC và xuất video thành phẩm an toàn...",
            state="validating",
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        dl_file = Path(self.dl_path)
        self._raise_if_aborted()
        self.qc_res = self.orchestrator.validate_downloaded_output(
            dl_file,
            self.attempt_res_dir,
            source_fingerprint=self.preflight_info.get("fingerprint"),
            extra_secrets=self.extra_secrets,
        )
        self._raise_if_aborted()

        self.parked_warm = False

        from .cloud_config import _is_local_default_banve, get_cloud_output_dir

        cloud_out_override = os.getenv("AUTODUB_CLOUD_OUTPUT_DIR", "").strip()
        out_env_raw = os.getenv("AUTODUB_OUTPUT_DIR", "").strip()
        pytest_default_cloud_leak = os.getenv("AUTODUB_CLOUD_TEST_OUTPUT_GUARD") == "1" and (
            not cloud_out_override
            or os.path.normcase(cloud_out_override.rstrip("\\/")) == os.path.normcase(r"D:\ToolV1_Cloud")
        )
        if pytest_default_cloud_leak:
            if out_env_raw and not _is_local_default_banve(out_env_raw) and Path(out_env_raw).exists():
                final_out_dir = Path(out_env_raw)
            else:
                final_out_dir = self.attempt_res_dir
        elif cloud_out_override:
            final_out_dir = get_cloud_output_dir()
        elif out_env_raw and not _is_local_default_banve(out_env_raw):
            final_out_dir = Path(out_env_raw) if Path(out_env_raw).exists() else self.attempt_res_dir
        else:
            default_cloud_dir = Path(r"D:\ToolV1_Cloud")
            final_out_dir = default_cloud_dir if Path("D:/").exists() else self.attempt_res_dir
        self.final_video_path = self.orchestrator.export_deliverable_atomic(
            dl_file,
            final_out_dir,
            job_id=self.job_id,
            attempt=self.session_attempt,
            video_name=self.job.video_name or self.validated_input.display_name,
            expected_sha256=self.qc_res["sha256"],
        )
        try:
            if not self.is_url and self.source_snapshot:
                src_final = Path(self.validated_input.normalized_value)
                if not src_final.is_file() or src_final.stat().st_size != self.source_snapshot["size_bytes"]:
                    raise _OrchestratorStageError(
                        CloudErrorCode.VALIDATION_FAILED.value,
                        "File video nguồn cục bộ không còn nguyên vẹn sau quá trình xử lý Cloud.",
                    )

            remote_meta = self.qc_res.get("remote_metadata") or {}
            local_probe = self.qc_res.get("local_probe") or {}
            self.output_manifest: Dict[str, Any] = {
                "schema_version": 1,
                "job_id": self.job_id,
                "attempt": self.session_attempt,
                "result_path": str(self.final_video_path),
                "staging_dir": str(self.attempt_res_dir),
                "sha256": self.qc_res["sha256"],
                "size_bytes": self.qc_res["size_bytes"],
                "duration_seconds": (
                    local_probe.get("duration_seconds")
                    or (remote_meta.get("output_video_probe") or {}).get("duration_seconds")
                ),
                "video_codec": (
                    local_probe.get("video_codec")
                    or (remote_meta.get("output_video_probe") or {}).get("video_codec")
                ),
                "audio_codec": (
                    local_probe.get("audio_codec")
                    or (remote_meta.get("output_video_probe") or {}).get("audio_codec")
                ),
                "qc_passed": True,
                "qc_report": self.qc_res["qc_report"],
                "remote_metadata": remote_meta,
                "setup_phase_seconds": self.setup_phase_seconds,
                "cache_backend": self.cache_backend,
                "cache_mounted": self.cache_mounted,
                "cache_status": self.cache_status,
                "pipeline_step_seconds": remote_meta.get("step_timings_seconds") or {},
            }
            (self.attempt_res_dir / "output_manifest.json").write_text(
                redact_secrets(json.dumps(self.output_manifest, indent=2, ensure_ascii=False), extra_secrets=self.extra_secrets),
                encoding="utf-8",
            )

            self._raise_if_aborted()
        except Exception:
            if self.parked_warm:
                self.orchestrator.release_warm_sessions(self.gpu_type)
            if self.final_video_path != dl_file:
                try:
                    Path(self.final_video_path).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _stage_cleanup(self):
        self._mark_stage("cleanup")
        # 8. Thu hồi & xác nhận session trước bước hoàn tất; tính lượng CU ước tính theo thời gian chạy
        self.parked_warm = False
        if (
            self.session_may_exist
            and not self.cleanup_done
            and self.orchestrator._park_warm_session_on_success(
                self.session_name,
                self.gpu_type,
                source_job_id=self.job_id,
                before_park=self._unmount_drive_before_warm_park if self.cache_mounted else None,
                rate_per_hour=self.rate_per_hour,
            )
        ):
            self.parked_warm = True
            self.cleanup_done = True
            self.runtime_end_time = time.time()
            self._sync_runtime_usage(check_provider=False)
            self.cleanup_result = {
                "verified_stopped": True,
                "cleanup_status": "verified",
                "attempts": 0,
                "error": None,
            }
            self.orchestrator.store.record_cleanup_result(
                self.job_id,
                self.raw_attempt,
                self.session_name,
                verified_stopped=True,
                cleanup_status="verified",
                attempts=0,
                error=None,
            )
            cleanup_res = dict(self.cleanup_result)
        else:
            cleanup_res = self._finalize_session_once()
        self.cu_spent = self._estimate_elapsed_cu()
        self.elapsed_sec = max(
            0.0,
            (self.runtime_end_time if self.runtime_end_time is not None else time.time())
            - (self.runtime_start_time or self.start_time),
        )
        if not cleanup_res.get("verified_stopped"):
            raise _OrchestratorStageError(
                CloudErrorCode.CLEANUP_FAILED.value,
                cleanup_res.get("error") or "Cloud session cleanup is unverified.",
            )
        if self.cache_status == "save_skipped_budget":
            aborted, abort_reason = self._check_abort()
            if aborted and abort_reason not in {"budget_exceeded", "budget_uncertain"}:
                self._raise_if_aborted()
        else:
            self._raise_if_aborted()
        final_cleanup_status = str(
            cleanup_res.get("cleanup_status")
            or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
        )
        self._mark_stage(None)
        self.output_manifest["stage_timings_seconds"] = dict(self.stage_timings)
        self.output_manifest["cu_rate_per_hour"] = self.rate_per_hour
        self.output_manifest["cu_measurement_source"] = self._cu_measurement_source()
        self.output_manifest["warm_idle_ledgered"] = self.warm_idle_ledgered
        try:
            (self.attempt_res_dir / "output_manifest.json").write_text(
                redact_secrets(json.dumps(self.output_manifest, indent=2, ensure_ascii=False), extra_secrets=self.extra_secrets),
                encoding="utf-8",
            )
        except OSError:
            pass

        if not self._update(
            100,
            f"Hoàn thành! Đã kiểm tra QC & lưu thành phẩm (~{self.cu_spent} CU ước tính).",
            state="completed",
            result_path=str(self.final_video_path),
            cu_actual=self.cu_spent,
            cleanup_status=final_cleanup_status,
            measurement_source=self._cu_measurement_source(),
            output_manifest=self.output_manifest,
        ):
            self._raise_if_aborted()
            raise _JobAborted("lease_lost", is_lease_lost=True)

        logger.info(
            "=== HOÀN THÀNH CLOUD JOB %s trong %.1fs (Ước tính tiêu thụ ~%.3f CU) ===",
            self.job_id,
            self.elapsed_sec,
            self.cu_spent,
        )
        return True

    def _handle_job_aborted(self, abort_exc: _JobAborted) -> bool:
        if abort_exc.is_lease_lost:
            logger.warning(
                "Cloud Job %s (attempt=%d) mất quyền sở hữu (%s); dừng thực thi an toàn không ghi đè.",
                self.job_id,
                self.session_attempt,
                abort_exc.reason,
            )
            return False

        self.cu_spent = self._estimate_elapsed_cu()
        if abort_exc.is_budget_exceeded:
            logger.warning(
                "Cloud Job %s bị dừng sớm do ngân sách/số dư (%s); thu hồi session %s...",
                self.job_id,
                abort_exc.reason,
                self.session_name,
            )
            self._update(
                0,
                "Đang dừng máy ảo Colab do chạm hạn mức chi phí...",
                state="cancelling",
                cu_actual=self.cu_spent,
                allow_from_cancel=True,
            )
            cleanup_res = self._finalize_session_once()
            self.cu_spent = self._estimate_elapsed_cu()
            err_code = (
                CloudErrorCode.BUDGET_UNCERTAIN.value
                if abort_exc.reason == "budget_uncertain"
                else CloudErrorCode.BUDGET_EXCEEDED.value
            )
            self.orchestrator.store.update_progress(
                self.job_id,
                state="failed",
                percent=0,
                step_name="Đã dừng do chạm hạn mức chi phí/số dư CU.",
                error=self.budget_abort_reason or "Đã chạm hạn mức chi phí CU khi đang xử lý.",
                error_code=err_code,
                cu_actual=self.cu_spent,
                cleanup_status=str(
                    cleanup_res.get("cleanup_status")
                    or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
                ),
                owner_id=self.effective_owner,
                attempt=self.ownership_attempt,
                allow_from_cancel=True,
                measurement_source=self._cu_measurement_source(),
            )
            return False

        logger.info("Cloud Job %s nhận yêu cầu hủy (%s), đang thu hồi session %s...", self.job_id, abort_exc.reason, self.session_name)
        self._update(
            0,
            "Đang dừng và xác nhận thu hồi máy ảo Colab...",
            state="cancelling",
            cu_actual=self.cu_spent,
            allow_from_cancel=True,
        )
        cleanup_res = self._finalize_session_once()
        self.cu_spent = self._estimate_elapsed_cu()
        if cleanup_res.get("verified_stopped"):
            self.orchestrator.store.update_progress(
                self.job_id,
                state="cancelled",
                percent=0,
                step_name="Đã dừng theo yêu cầu người dùng.",
                error="Đã dừng bởi người dùng.",
                error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                cu_actual=self.cu_spent,
                cleanup_status="verified",
                owner_id=self.effective_owner,
                attempt=self.ownership_attempt,
                allow_from_cancel=True,
                measurement_source=self._cu_measurement_source(),
            )
        else:
            self.orchestrator.store.update_progress(
                self.job_id,
                state="cancelling",
                percent=0,
                step_name="Chưa xác nhận dừng máy ảo Colab; cần dọn dẹp lại.",
                error=cleanup_res.get("error") or "Chưa xác nhận dừng session Colab.",
                error_code=CloudErrorCode.CLEANUP_FAILED.value,
                cu_actual=self.cu_spent,
                cleanup_status=str(cleanup_res.get("cleanup_status") or "failed_retry_needed"),
                owner_id=self.effective_owner,
                attempt=self.ownership_attempt,
                allow_from_cancel=True,
                measurement_source=self._cu_measurement_source(),
            )
        return False

    def _handle_job_exception(self, exc: Exception) -> bool:
        # Nếu trong lúc step fail mà job vừa có yêu cầu hủy hoặc mất lease thì ưu tiên nhánh đó
        self.cu_spent = self._estimate_elapsed_cu()
        aborted_now, abort_reason = self._check_abort()
        if aborted_now:
            if abort_reason in _LEASE_LOST_REASONS:
                logger.warning("Job %s mất lease sau lỗi bước (%s); không ghi đè trạng thái.", self.job_id, abort_reason)
                return False
            self._update(
                0,
                "Đang dừng và xác nhận thu hồi máy ảo Colab...",
                state="cancelling",
                cu_actual=self.cu_spent,
                allow_from_cancel=True,
            )
            cleanup_res = self._finalize_session_once()
            self.cu_spent = self._estimate_elapsed_cu()
            if cleanup_res.get("verified_stopped"):
                self.orchestrator.store.update_progress(
                    self.job_id,
                    state="cancelled",
                    percent=0,
                    step_name="Đã dừng theo yêu cầu người dùng.",
                    error="Đã dừng bởi người dùng.",
                    error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                    cu_actual=self.cu_spent,
                    cleanup_status="verified",
                    owner_id=self.effective_owner,
                    attempt=self.ownership_attempt,
                    allow_from_cancel=True,
                    measurement_source=self._cu_measurement_source(),
                )
            return False

        cleanup_res = self._finalize_session_once()
        self.cu_spent = self._estimate_elapsed_cu()
        safe_exc = redact_secrets(str(exc), extra_secrets=self.extra_secrets)
        exc_error_code = (
            exc.error_code
            if isinstance(exc, _OrchestratorStageError) and exc.error_code in _VALID_ERROR_CODES
            else CloudErrorCode.EXEC_FAILED.value
        )
        logger.error("Lỗi trong quá trình xử lý Cloud Job %s [%s]: %s", self.job_id, exc_error_code, safe_exc)
        self._update(
            0,
            f"Lỗi: {safe_exc[:120]}",
            state="failed",
            error=safe_exc,
            error_code=exc_error_code,
            cu_actual=self.cu_spent,
            cleanup_status=str(
                cleanup_res.get("cleanup_status")
                or ("verified" if cleanup_res.get("verified_stopped") else "failed_retry_needed")
            ),
            measurement_source=self._cu_measurement_source(),
        )
        return False



class CloudOrchestrator:
    def __init__(
        self,
        store: Optional[CloudJobStore] = None,
        adapter: Optional[ColabProviderAdapter] = None,
        allowed_roots: Optional[Sequence[str | Path]] = None,
        *,
        enable_warm_session_reuse: Optional[bool] = None,
        has_pending_work_callback: Optional[Callable[[str], bool]] = None,
    ):
        self.store = store or CloudJobStore()
        self.adapter = adapter or ColabProviderAdapter()
        self.allowed_roots = allowed_roots
        self.backend_dir = Path(__file__).resolve().parent.parent
        self.gpu_scripts_dir = Path(__file__).resolve().parent / "gpu_scripts"
        if enable_warm_session_reuse is None:
            self.enable_warm_session_reuse = (
                isinstance(self.adapter, ColabProviderAdapter)
                and bool(DEFAULT_CONFIG.get("warm_session_reuse_enabled", True))
            )
        else:
            self.enable_warm_session_reuse = bool(enable_warm_session_reuse)
        self.has_pending_work_callback = has_pending_work_callback
        self._warm_lock = threading.Lock()
        self._warm_sessions: Dict[str, Dict[str, Any]] = {}
        self._warm_parked_at: Dict[str, float] = {}
        self._scope_key = _store_scope_key(self.store)
        with _GLOBAL_WARM_LOCK:
            _GLOBAL_ORCHESTRATORS.setdefault(self._scope_key, weakref.WeakSet()).add(self)

    def _cancel_warm_timer(self, session_name: str) -> None:
        with _GLOBAL_WARM_LOCK:
            tmr = _GLOBAL_WARM_TIMERS.pop((self._scope_key, session_name), None)
        if tmr is not None:
            try:
                tmr.cancel()
            except Exception:
                pass

    def _requeue_unstopped_warm_session(
        self,
        session_name: str,
        gpu_type: str,
        source_job_id: Optional[str] = None,
        parked_at: Optional[float] = None,
    ) -> None:
        park_fn = getattr(self.store, "park_warm_session", None)
        if callable(park_fn):
            with self._warm_lock:
                local_parked_at = self._warm_parked_at.get(session_name)
            if local_parked_at is None:
                local_parked_at = parked_at
            requeue_ts = time.time() - 5.0
            try:
                park_fn(
                    session_name,
                    gpu_type,
                    ttl_seconds=1.0,
                    source_job_id=source_job_id,
                    keepalive_mode=False,
                    now=min(requeue_ts, local_parked_at) if local_parked_at is not None else requeue_ts,
                )
            except Exception:
                pass

    def _stop_warm_session_with_retry_guard(
        self,
        session_name: str,
        gpu_type: str,
        source_job_id: Optional[str] = None,
        parked_at: Optional[float] = None,
    ) -> bool:
        try:
            res = self._verify_or_stop_session(session_name)
            if res.get("verified_stopped"):
                self._record_warm_idle(
                    session_name,
                    gpu_type,
                    source_job_id=source_job_id,
                    parked_at=parked_at,
                )
                return True
        except Exception as exc:
            logger.warning("Cảnh báo khi dừng session giữ ấm %s: %s", session_name, redact_secrets(str(exc)))
        self._requeue_unstopped_warm_session(
            session_name,
            gpu_type,
            source_job_id=source_job_id,
            parked_at=parked_at,
        )
        return False

    def has_waiting_work(self, gpu_type: str) -> bool:
        gpu_norm = str(gpu_type or "").strip().upper()
        has_db_fn = getattr(self.store, "has_queued_jobs", None)
        if callable(has_db_fn):
            try:
                if has_db_fn(gpu_type=gpu_norm):
                    return True
            except Exception:
                pass
        with _GLOBAL_WARM_LOCK:
            peers = list(_GLOBAL_ORCHESTRATORS.get(self._scope_key) or ())
        if self not in peers:
            peers.append(self)
        for peer in peers:
            cb = getattr(peer, "has_pending_work_callback", None)
            if callable(cb):
                try:
                    if cb(gpu_norm):
                        return True
                except Exception:
                    pass
        return False

    def get_warm_session_names(self) -> Tuple[str, ...]:
        list_fn = getattr(self.store, "list_warm_sessions", None)
        if callable(list_fn):
            rows = list_fn(include_expired=False)
            return tuple(str(r["session_name"]) for r in rows if r.get("session_name"))
        with self._warm_lock:
            return tuple(str(v["session_name"]) for v in self._warm_sessions.values() if v.get("session_name"))

    def release_warm_sessions(self, gpu_type: Optional[str] = None) -> int:
        target_gpu = str(gpu_type).strip().upper() if gpu_type else None
        to_stop: List[Tuple[str, str, Optional[str], Optional[float]]] = []
        pop_all_fn = getattr(self.store, "pop_all_warm_sessions", None)
        if callable(pop_all_fn):
            for row in pop_all_fn(gpu_type=target_gpu):
                sname = str(row.get("session_name") or "")
                if sname:
                    self._cancel_warm_timer(sname)
                    to_stop.append(
                        (
                            sname,
                            str(row.get("gpu_type") or target_gpu or "T4"),
                            row.get("source_job_id"),
                            row.get("parked_at"),
                        )
                    )
        with self._warm_lock:
            keys = [target_gpu] if target_gpu else list(self._warm_sessions.keys())
            for k in keys:
                entry = self._warm_sessions.pop(k, None)
                if entry and entry.get("session_name"):
                    sname = str(entry["session_name"])
                    self._cancel_warm_timer(sname)
                    if not any(x[0] == sname for x in to_stop):
                        to_stop.append(
                            (
                                sname,
                                str(entry.get("gpu_type") or k or "T4"),
                                entry.get("source_job_id"),
                                entry.get("parked_at"),
                            )
                        )
        for sname, g_type, src_jid, parked_at in to_stop:
            logger.info("Giải phóng máy ảo giữ ấm %s...", sname)
            self._stop_warm_session_with_retry_guard(
                sname,
                g_type,
                source_job_id=src_jid,
                parked_at=parked_at,
            )
        return len(to_stop)

    def release_idle_warm_sessions_if_no_work(self) -> int:
        to_stop: List[Tuple[str, str, Optional[str], Optional[float]]] = []
        now_ts = time.time()
        list_fn = getattr(self.store, "list_warm_sessions", None)
        rem_fn = getattr(self.store, "remove_warm_session", None)
        if callable(list_fn) and callable(rem_fn):
            for row in list_fn(include_expired=True):
                sname = str(row.get("session_name") or "")
                g_type = str(row.get("gpu_type") or "T4").strip().upper()
                exp_ts = float(row.get("expires_at") or 0.0)
                if not sname:
                    continue
                if bool(row.get("keepalive_mode")) and now_ts < exp_ts:
                    continue
                if self.has_waiting_work(g_type) and now_ts < exp_ts:
                    continue
                if rem_fn(sname):
                    self._cancel_warm_timer(sname)
                    to_stop.append((sname, g_type, row.get("source_job_id"), row.get("parked_at")))
        now_mono = time.monotonic()
        with self._warm_lock:
            for k in list(self._warm_sessions.keys()):
                entry = self._warm_sessions.get(k)
                if not entry:
                    continue
                if entry.get("keepalive_mode") and now_mono < float(entry.get("expires_at", 0.0)):
                    continue
                if self.has_waiting_work(k) and now_mono < float(entry.get("expires_at", 0.0)):
                    continue
                popped = self._warm_sessions.pop(k, None)
                if popped and popped.get("session_name"):
                    sname = str(popped["session_name"])
                    self._cancel_warm_timer(sname)
                    if not any(x[0] == sname for x in to_stop):
                        to_stop.append(
                            (sname, k, popped.get("source_job_id"), popped.get("parked_at"))
                        )
        for sname, g_type, src_jid, parked_at in to_stop:
            logger.info("Không còn video chờ — thu hồi máy ảo giữ ấm %s...", sname)
            self._stop_warm_session_with_retry_guard(
                sname,
                g_type,
                source_job_id=src_jid,
                parked_at=parked_at,
            )
        return len(to_stop)

    def _sanitize_warm_workdir(self, session_name: str) -> bool:
        exec_fn = getattr(self.adapter, "exec_argv", None)
        if not callable(exec_fn):
            return False
        try:
            ok, _ = exec_fn(
                session_name,
                _build_warm_reset_argv(),
                cwd="/content",
                timeout=30,
            )
            return bool(ok)
        except Exception:
            return False

    def _record_warm_idle(
        self,
        session_name: str,
        gpu_type: str,
        *,
        source_job_id: Optional[str] = None,
        parked_at: Optional[float] = None,
    ) -> None:
        with self._warm_lock:
            local_parked_at = self._warm_parked_at.pop(session_name, None)
        if local_parked_at is None:
            local_parked_at = parked_at
        record_fn = getattr(self.store, "record_warm_idle_usage", None)
        if local_parked_at is None or not callable(record_fn):
            return
        try:
            res = record_fn(
                session_name,
                gpu_type,
                parked_at=local_parked_at,
                source_job_id=source_job_id,
            )
            logger.info("CU giữ ấm không tải của %s: %s", session_name, res)
        except Exception as exc:
            logger.warning("Không ghi được CU giữ ấm của %s: %s", session_name, redact_secrets(str(exc)))

    def _pop_warm_session(self, gpu_type: str) -> Optional[str]:
        entry = self._pop_warm_session_entry(gpu_type)
        return entry[0] if entry else None

    def _pop_warm_session_entry(self, gpu_type: str) -> Optional[Tuple[str, Optional[float]]]:
        """Trả về (session_name, parked_at epoch) để job mới chịu CU của khoảng giữ ấm không tải."""
        if not self.enable_warm_session_reuse:
            return None
        gpu_norm = str(gpu_type or "").strip().upper()
        entry: Optional[Dict[str, Any]] = None
        expired = False
        pop_fn = getattr(self.store, "pop_warm_session", None)
        if callable(pop_fn):
            entry = pop_fn(gpu_norm)
            if entry is not None:
                expired = bool(entry.get("expired"))
        with self._warm_lock:
            mem_entry = self._warm_sessions.pop(gpu_norm, None)
        if entry is None and mem_entry is not None:
            entry = mem_entry
            expired = time.monotonic() > float(mem_entry.get("expires_at", 0.0))
        if not entry:
            return None
        sname = str(entry.get("session_name") or "")
        src_jid = entry.get("source_job_id")
        if not sname:
            return None
        self._cancel_warm_timer(sname)
        if expired:
            self._stop_warm_session_with_retry_guard(
                sname,
                gpu_norm,
                source_job_id=src_jid,
                parked_at=entry.get("parked_at"),
            )
            return None
        if self._sanitize_warm_workdir(sname):
            logger.info("Tái sử dụng máy ảo GPU %s đang giữ ấm: %s (bỏ qua cold-start setup).", gpu_norm, sname)
            with self._warm_lock:
                local_parked = self._warm_parked_at.pop(sname, None)
            parked_at: Optional[float] = local_parked
            try:
                if entry.get("parked_at") is not None and entry is not mem_entry:
                    parked_at = float(entry["parked_at"])
            except (TypeError, ValueError):
                pass
            return sname, parked_at
        logger.warning("Máy ảo giữ ấm %s không phản hồi lệnh dọn workdir; thu hồi và tạo máy mới.", sname)
        self._stop_warm_session_with_retry_guard(
            sname,
            gpu_norm,
            source_job_id=src_jid,
            parked_at=entry.get("parked_at"),
        )
        return None

    def _park_warm_session_on_success(
        self,
        session_name: str,
        gpu_type: str,
        *,
        source_job_id: Optional[str] = None,
        before_park: Optional[Callable[[], bool]] = None,
        rate_per_hour: Optional[float] = None,
    ) -> bool:
        if not self.enable_warm_session_reuse:
            return False
        gpu_norm = str(gpu_type or "").strip().upper()
        waiting_next = self.has_waiting_work(gpu_norm)
        max_keepalive_s = max(0, int(DEFAULT_CONFIG.get("warm_session_keepalive_max_seconds", 1800) or 0))
        keepalive_s = min(
            max_keepalive_s,
            max(0, int(DEFAULT_CONFIG.get("warm_session_keepalive_seconds", 300) or 0)),
        )
        handoff_s = min(
            max_keepalive_s,
            max(15, int(DEFAULT_CONFIG.get("warm_session_handoff_seconds", 180) or 180)),
        )
        ttl_s = handoff_s if waiting_next else keepalive_s
        if ttl_s <= 0:
            return False
        get_budget_info = getattr(self.store, "get_cu_budget_info", None)
        if callable(get_budget_info):
            try:
                budget_info = get_budget_info()
                daily_limit = float(budget_info.get("budget_daily", 0.0) or 0.0)
                used_today = float(budget_info.get("spent_today", 0.0) or 0.0)
                reserved_today = float(budget_info.get("reserved_today", 0.0) or 0.0)
                hourly_rate = (
                    float(rate_per_hour)
                    if rate_per_hour is not None
                    else float(GPU_CU_RATES.get(gpu_norm, 1.15))
                )
                idle_cu = (ttl_s / 3600.0) * hourly_rate
                if daily_limit <= 0.0 or used_today + reserved_today + idle_cu > daily_limit:
                    logger.info(
                        "Không giữ ấm session %s vì CU dự kiến %.4f vượt phần hạn mức ngày còn lại.",
                        session_name,
                        idle_cu,
                    )
                    return False
            except Exception as exc:
                logger.warning(
                    "Không xác định được CU còn lại để giữ ấm session %s; session sẽ được thu hồi: %s",
                    session_name,
                    redact_secrets(str(exc)),
                )
                return False
        if before_park is not None and not before_park():
            return False
        if not self._sanitize_warm_workdir(session_name):
            return False

        evicted_snames: List[str] = []
        with self._warm_lock:
            self._warm_parked_at[session_name] = time.time()
        park_fn = getattr(self.store, "park_warm_session", None)
        rem_fn = getattr(self.store, "remove_warm_session", None)
        if callable(park_fn):
            evicted_snames = park_fn(
                session_name,
                gpu_norm,
                ttl_seconds=float(ttl_s),
                source_job_id=source_job_id,
                keepalive_mode=not waiting_next,
            )

        def _expire_warm() -> None:
            with _GLOBAL_WARM_LOCK:
                _GLOBAL_WARM_TIMERS.pop((self._scope_key, session_name), None)
            should_stop = False
            if callable(rem_fn):
                should_stop = bool(rem_fn(session_name))
            with self._warm_lock:
                cur = self._warm_sessions.get(gpu_norm)
                if cur and cur.get("session_name") == session_name:
                    self._warm_sessions.pop(gpu_norm, None)
                    should_stop = True
            if should_stop:
                logger.info("Hết thời gian giữ ấm (%ds) — tự động thu hồi session %s...", ttl_s, session_name)
                self._stop_warm_session_with_retry_guard(session_name, gpu_norm, source_job_id=source_job_id)

        timer = threading.Timer(float(ttl_s), _expire_warm)
        timer.daemon = True
        with self._warm_lock:
            prev = self._warm_sessions.pop(gpu_norm, None)
            if prev and prev.get("session_name") and prev.get("session_name") != session_name:
                if str(prev["session_name"]) not in evicted_snames:
                    evicted_snames.append(str(prev["session_name"]))
            if not callable(park_fn):
                self._warm_sessions[gpu_norm] = {
                    "session_name": session_name,
                    "gpu_type": gpu_norm,
                    "source_job_id": source_job_id,
                    "parked_at": time.time(),
                    "expires_at": time.monotonic() + float(ttl_s),
                    "keepalive_mode": not waiting_next,
                }
        for ev_name in evicted_snames:
            self._cancel_warm_timer(ev_name)
        with _GLOBAL_WARM_LOCK:
            prev_tmr = _GLOBAL_WARM_TIMERS.pop((self._scope_key, session_name), None)
            if prev_tmr is not None:
                try:
                    prev_tmr.cancel()
                except Exception:
                    pass
            _GLOBAL_WARM_TIMERS[(self._scope_key, session_name)] = timer
            timer.start()

        for ev_name in evicted_snames:
            self._stop_warm_session_with_retry_guard(ev_name, gpu_norm)
        logger.info("Giữ ấm máy ảo %s (%s) tối đa %ds chờ video kế tiếp.", session_name, gpu_norm, ttl_s)
        return True

    def _verify_or_stop_session(self, session_name: str, extra_secrets: Sequence[str] = ()) -> Dict[str, Any]:
        verify_fn = getattr(self.adapter, "verify_session_stopped", None)
        if callable(verify_fn):
            try:
                res = verify_fn(session_name)
                if isinstance(res, dict) and "verified_stopped" in res:
                    cleaned = dict(res)
                    if cleaned.get("error"):
                        cleaned["error"] = redact_secrets(
                            str(cleaned["error"]), extra_secrets=extra_secrets
                        )
                    return cleaned
            except Exception as exc:
                safe_err = redact_secrets(str(exc), extra_secrets=extra_secrets)
                return {
                    "verified_stopped": False,
                    "cleanup_status": "failed_retry_needed",
                    "attempts": 1,
                    "error": safe_err,
                }

        try:
            stop_ok, stop_msg = self.adapter.stop_session(session_name)
        except Exception as exc:
            safe_err = redact_secrets(str(exc), extra_secrets=extra_secrets)
            return {
                "verified_stopped": False,
                "cleanup_status": "failed_retry_needed",
                "attempts": 1,
                "error": safe_err,
            }

        status_fn = getattr(self.adapter, "get_sessions_status", None)
        if callable(status_fn):
            try:
                st = status_fn()
                if not st.get("ok"):
                    return {
                        "verified_stopped": False,
                        "cleanup_status": "provider_unreachable",
                        "attempts": 1,
                        "error": redact_secrets(str(st.get("error") or stop_msg), extra_secrets=extra_secrets),
                    }
                still_present = any(
                    str(item.get("name")) == session_name for item in (st.get("sessions") or [])
                )
                if stop_ok and not still_present:
                    return {
                        "verified_stopped": True,
                        "cleanup_status": "verified",
                        "attempts": 1,
                        "error": None,
                    }
                return {
                    "verified_stopped": False,
                    "cleanup_status": "failed_retry_needed",
                    "attempts": 1,
                    "error": redact_secrets(
                        f"Session {session_name} vẫn tồn tại sau lệnh dừng." if still_present else str(stop_msg),
                        extra_secrets=extra_secrets,
                    ),
                }
            except Exception as exc:
                return {
                    "verified_stopped": False,
                    "cleanup_status": "provider_unreachable",
                    "attempts": 1,
                    "error": redact_secrets(str(exc), extra_secrets=extra_secrets),
                }

        return {
            "verified_stopped": bool(stop_ok),
            "cleanup_status": "verified" if stop_ok else "failed_retry_needed",
            "attempts": 1,
            "error": None if stop_ok else redact_secrets(str(stop_msg), extra_secrets=extra_secrets),
        }

    def _resolve_gpu_scripts_dir(self) -> Path:
        if self.gpu_scripts_dir.is_dir():
            return self.gpu_scripts_dir
        alt = Path(__file__).resolve().parent / "gpu_scripts"
        if alt.is_dir():
            return alt
        return self.gpu_scripts_dir

    def _build_v1_runtime_profile(self, validated_input: Any = None) -> Dict[str, Any]:
        """Snapshot non-secret Tool V1 Local settings (Canvas, Audio, Auto-Voice, Glossary) for Cloud parity."""
        ws_raw = os.getenv("AUTODUB_WORKSPACE", "").strip()
        ws = Path(ws_raw) if ws_raw else (self.backend_dir.parent / "workspace")

        def _read_first_json(candidates: Sequence[Path]) -> Dict[str, Any]:
            for cand in candidates:
                try:
                    if cand.is_file() and cand.stat().st_size <= 2 * 1024 * 1024:
                        parsed = json.loads(cand.read_text(encoding="utf-8-sig"))
                        if isinstance(parsed, dict):
                            return parsed
                except Exception:
                    continue
            return {}

        canvas_raw = _read_first_json([ws / "bot_system" / "canvas_settings.json", ws / "canvas_settings.json"])
        cover_env = os.getenv("V1_SUBTITLE_COVER_STYLE", "").strip().lower()
        cover_style = cover_env or str(canvas_raw.get("sub_cover_style") or "localized_blur").strip().lower()
        if cover_style not in {"solid_box", "adaptive_box", "frosted_blur", "localized_blur"}:
            cover_style = "localized_blur"
        aspect_ratio = str(canvas_raw.get("aspect_ratio") or "9:16").strip()
        if aspect_ratio not in {"9:16", "original", "16:9", "1:1"}:
            aspect_ratio = "9:16"
        delogo_mode = str(
            os.getenv("V1_DELOGO_MODE", "").strip()
            or canvas_raw.get("delogo_mode")
            or "off"
        ).strip().lower()
        if delogo_mode not in {"off", "corners", "smart"}:
            delogo_mode = "off"

        def _clamped_float(val: Any, default: float, lo: float, hi: float) -> float:
            try:
                f = float(val)
                if math.isfinite(f):
                    return max(lo, min(hi, f))
            except (TypeError, ValueError):
                pass
            return default

        canvas_cfg = {
            "aspect_ratio": aspect_ratio,
            "auto_crop_black_bars": bool(canvas_raw.get("auto_crop_black_bars", True)),
            "sub_cover_style": cover_style,
            "sub_blur_radius": int(round(_clamped_float(canvas_raw.get("sub_blur_radius"), 16.0, 2.0, 32.0))),
            "sub_blur_padding_x": _clamped_float(canvas_raw.get("sub_blur_padding_x"), 0.015, 0.0, 0.05),
            "sub_blur_padding_y": _clamped_float(canvas_raw.get("sub_blur_padding_y"), 0.008, 0.0, 0.05),
            "sub_font_name": str(canvas_raw.get("sub_font_name") or "Arial").strip() or "Arial",
            "sub_font_color": str(canvas_raw.get("sub_font_color") or "#000000").strip() or "#000000",
            "sub_font_bold": bool(canvas_raw.get("sub_font_bold", True)),
            "delogo_mode": delogo_mode,
        }

        audio_raw = _read_first_json([ws / "bot_system" / "audio_settings.json", ws / "audio_settings.json"])
        sep_mode = str(audio_raw.get("separation_mode") or "roformer").strip().lower()
        if sep_mode not in {"roformer", "demucs", "bypass"}:
            sep_mode = "roformer"
        duck_mode = str(audio_raw.get("ducking_mode") or "soft").strip().lower()
        if duck_mode not in {"soft", "medium", "off"}:
            duck_mode = "soft"
        script_mode = str(audio_raw.get("script_mode") or os.getenv("SCRIPT_MODE", "default")).strip().lower()
        if script_mode not in {"default", "humorous"}:
            script_mode = "default"
        audio_cfg = {
            "bgm_volume_db": round(_clamped_float(audio_raw.get("bgm_volume_db"), -2.0, -40.0, 10.0), 1),
            "dubbing_volume_db": round(_clamped_float(audio_raw.get("dubbing_volume_db"), 1.0, -20.0, 15.0), 1),
            "separation_mode": sep_mode,
            "ducking_mode": duck_mode,
            "script_mode": script_mode,
        }

        voice_raw = _read_first_json(
            [ws / "bot_system" / "control" / "v1_auto_voice.json", ws / "control" / "v1_auto_voice.json"]
        )
        auto_voice_cfg = {
            "enabled": bool(voice_raw.get("enabled", True)),
            "female_voice_id": str(voice_raw.get("female_voice_id") or "capcut-BV562_streaming").strip(),
            "male_voice_id": str(voice_raw.get("male_voice_id") or "capcut-BV075_streaming").strip(),
            "dual_voice": bool(voice_raw.get("dual_voice", False)),
        }

        glossary: Dict[str, str] = {}
        global_gloss_raw = _read_first_json(
            [ws / "bot_system" / "control" / "custom_glossary.json", ws / "control" / "custom_glossary.json"]
        )
        entries = global_gloss_raw.get("entries", global_gloss_raw) if isinstance(global_gloss_raw, dict) else {}
        if isinstance(entries, dict):
            for k, v in list(entries.items())[:300]:
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs and not ks.startswith("_"):
                    glossary[ks] = vs

        if getattr(validated_input, "kind", "") == "local_file":
            try:
                folder_name = Path(validated_input.normalized_value).parent.name.lower()
                folder_slug = re.sub(r"[^\w\-.]+", "_", folder_name).strip("_")
                if folder_slug:
                    prof_raw = _read_first_json(
                        [ws / "bot_system" / "control" / "glossary_profiles.json", ws / "control" / "glossary_profiles.json"]
                    )
                    scopes = prof_raw.get("scopes", prof_raw) if isinstance(prof_raw, dict) else {}
                    scoped_entries = scopes.get(f"folder:{folder_slug}") if isinstance(scopes, dict) else None
                    if isinstance(scoped_entries, dict):
                        for k, v in list(scoped_entries.items())[:150]:
                            ks, vs = str(k).strip(), str(v).strip()
                            if ks and vs and not ks.startswith("_"):
                                glossary[ks] = vs
            except Exception:
                pass

        return {
            "schema_version": 1,
            "canvas": canvas_cfg,
            "audio": audio_cfg,
            "auto_voice": auto_voice_cfg,
            "glossary": glossary,
        }

    def verify_local_preflight(
        self,
        validated_input: Any,
        *,
        max_upload_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Kiểm tra toàn bộ điều kiện đầu vào và bundle cục bộ trước khi cấp phát GPU (0 CU):
        - Manifest môi trường, requirements_cloud.txt và checksum .whl
        - Đầy đủ module lõi V1 và gpu_scripts
        - Dung lượng & checksum file video local (không rỗng, không vượt ngưỡng)
        """
        scripts_dir = self._resolve_gpu_scripts_dir()
        manifest_path = scripts_dir / "cloud_env_manifest.json"
        if not manifest_path.is_file():
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_SETUP_FAILED.value,
                f"Thiếu cloud_env_manifest.json tại {manifest_path}",
            )
        try:
            env_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_SETUP_FAILED.value,
                f"Không đọc được cloud_env_manifest.json: {exc}",
            ) from exc

        req_path = scripts_dir / str(env_manifest.get("requirements_file", "requirements_cloud.txt"))
        if not req_path.is_file() or req_path.stat().st_size == 0:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_PACKAGE_MISSING.value,
                f"Thiếu hoặc rỗng file requirements Cloud: {req_path.name}",
            )

        for wheel_spec in env_manifest.get("wheels", []):
            wheel_name = wheel_spec.get("filename", "")
            wheel_path = scripts_dir / wheel_name
            if not wheel_path.is_file():
                raise _OrchestratorStageError(
                    CloudErrorCode.ENV_PACKAGE_MISSING.value,
                    f"Thiếu gói wheel bắt buộc: {wheel_name}",
                )
            expected_sha = str(wheel_spec.get("sha256") or "").strip()
            if expected_sha:
                actual_sha = _file_sha256(wheel_path)
                if actual_sha != expected_sha:
                    raise _OrchestratorStageError(
                        CloudErrorCode.ENV_PACKAGE_MISSING.value,
                        f"Sai checksum SHA256 cho wheel {wheel_name} (cục bộ bị hỏng hoặc chưa đồng bộ).",
                    )

        backend_root = scripts_dir.parent.parent
        v1_core_paths = [backend_root / name for name in _V1_CORE_MODULE_NAMES]
        missing_modules: List[str] = []
        for mod_name in env_manifest.get("required_v1_modules", []):
            candidate_scripts = scripts_dir / mod_name
            candidate_backend = backend_root / mod_name
            target = candidate_backend if mod_name in _V1_CORE_MODULE_NAMES else candidate_scripts
            if not target.is_file() or target.stat().st_size == 0:
                missing_modules.append(mod_name)
        if missing_modules:
            raise _OrchestratorStageError(
                CloudErrorCode.ENV_PACKAGE_MISSING.value,
                f"Thiếu module bắt buộc trước khi đóng gói lên Cloud: {', '.join(missing_modules)}",
            )

        all_files = (
            list(scripts_dir.glob("*.whl"))
            + list(scripts_dir.glob("*.py"))
            + [req_path, manifest_path]
            + v1_core_paths
        )
        upload_targets = list({p.name: p for p in all_files if p.is_file()}.values())

        source_snapshot: Optional[Dict[str, Any]] = None
        if getattr(validated_input, "input_kind", "local_file") == "local_file":
            src_path = Path(validated_input.normalized_value)
            if not src_path.is_file():
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"Không tìm thấy file video đầu vào: {src_path.name}",
                )
            size_bytes = src_path.stat().st_size
            if size_bytes <= 0:
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"File video đầu vào rỗng (0 byte): {src_path.name}",
                )
            limit_bytes = int(
                max_upload_size_bytes
                if max_upload_size_bytes is not None
                else DEFAULT_CONFIG.get("max_upload_size_bytes", 2 * 1024 * 1024 * 1024)
            )
            if limit_bytes > 0 and size_bytes > limit_bytes:
                raise _OrchestratorStageError(
                    CloudErrorCode.INVALID_INPUT.value,
                    f"File video {src_path.name} ({size_bytes} bytes) vượt giới hạn tải lên ({limit_bytes} bytes).",
                )
            fingerprint = build_input_fingerprint(str(src_path), kind="local_file")
            source_snapshot = {
                "path": str(src_path),
                "size_bytes": size_bytes,
                "sha256": fingerprint["sha256"],
            }
        else:
            fingerprint = build_input_fingerprint(validated_input.normalized_value, kind="url")

        return {
            "env_manifest": env_manifest,
            "upload_targets": upload_targets,
            "fingerprint": fingerprint,
            "source_snapshot": source_snapshot,
        }

    def validate_downloaded_output(
        self,
        dl_file: Path,
        attempt_res_dir: Path,
        *,
        source_fingerprint: Optional[Dict[str, Any]] = None,
        extra_secrets: Sequence[str] = (),
    ) -> Dict[str, Any]:
        """
        Kiểm tra toàn diện video & metadata tải về tại vùng staging theo attempt trước khi công bố.
        Nếu QC fail, lưu qc_report.json để chẩn đoán trong attempt_res_dir và chặn xuất ra final_out_dir.
        """
        qc_report: Dict[str, Any] = {
            "passed": False,
            "staging_dir": str(attempt_res_dir),
            "downloaded_file": str(dl_file),
            "checks": [],
            "error_code": None,
            "error": None,
        }

        def _persist_qc_and_fail(err_code: str, message: str) -> None:
            safe_msg = redact_secrets(message, extra_secrets=extra_secrets)
            qc_report["passed"] = False
            qc_report["error_code"] = err_code
            qc_report["error"] = safe_msg
            try:
                (attempt_res_dir / "qc_report.json").write_text(
                    redact_secrets(json.dumps(qc_report, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                    encoding="utf-8",
                )
            except OSError:
                pass
            raise _OrchestratorStageError(err_code, safe_msg)

        if not dl_file.is_file():
            _persist_qc_and_fail(CloudErrorCode.DOWNLOAD_FAILED.value, "File video tải về không tồn tại trên đĩa.")
        dl_size = dl_file.stat().st_size
        if dl_size <= 0:
            _persist_qc_and_fail(CloudErrorCode.DOWNLOAD_FAILED.value, "File video tải về bị rỗng (0 byte).")

        dl_sha = _file_sha256(dl_file)
        qc_report["size_bytes"] = dl_size
        qc_report["sha256"] = dl_sha
        qc_report["checks"].append("download_non_empty")

        remote_meta: Dict[str, Any] = {}
        meta_file = attempt_res_dir / "result_metadata.json"
        if meta_file.is_file():
            try:
                raw_meta_text = redact_secrets(
                    meta_file.read_text(encoding="utf-8"),
                    extra_secrets=extra_secrets,
                )
                meta_file.write_text(raw_meta_text, encoding="utf-8")
                parsed_meta = json.loads(raw_meta_text or "{}")
                if isinstance(parsed_meta, dict):
                    remote_meta = parsed_meta
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _persist_qc_and_fail(
                    CloudErrorCode.QC_FAILED.value,
                    f"File result_metadata.json không hợp lệ: {exc}",
                )

        if remote_meta:
            r_status = remote_meta.get("status")
            if r_status not in (None, "completed"):
                r_code = str(remote_meta.get("error_code") or "").strip()
                err_code = r_code if r_code in _VALID_ERROR_CODES else CloudErrorCode.QC_FAILED.value
                _persist_qc_and_fail(
                    err_code,
                    f"Metadata Colab báo trạng thái lỗi ({r_status}): {remote_meta.get('error') or 'unknown'}",
                )

            if "segments_count" in remote_meta:
                seg_count = int(remote_meta.get("segments_count") or 0)
                no_speech_mode = bool(remote_meta.get("no_speech_mode", False))
                if seg_count <= 0 and not no_speech_mode:
                    _persist_qc_and_fail(
                        CloudErrorCode.NO_SPEECH_DETECTED.value,
                        "Metadata báo 0 câu thoại và không bật no_speech_mode.",
                    )
                translated_count = int(remote_meta.get("translated_count", seg_count))
                if translated_count != seg_count:
                    _persist_qc_and_fail(
                        CloudErrorCode.TRANSLATION_FAILED.value,
                        f"Số câu đã dịch không khớp ASR ({translated_count}/{seg_count}).",
                    )
                tts_count = int(remote_meta.get("tts_verified_count", seg_count))
                if not no_speech_mode and tts_count != seg_count:
                    _persist_qc_and_fail(
                        CloudErrorCode.TTS_GENERATION_FAILED.value,
                        f"Số file TTS đạt chuẩn không khớp số câu ({tts_count}/{seg_count}).",
                    )
                qc_report["checks"].append("translation_and_tts_counts")


            artifacts = remote_meta.get("artifacts") or {}
            video_art = artifacts.get("output_video.mp4") if isinstance(artifacts, dict) else None
            if isinstance(video_art, dict):
                exp_size = video_art.get("size_bytes")
                exp_sha = str(video_art.get("sha256") or "").strip()
                if exp_size is not None and int(exp_size) != dl_size:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"File tải về bị cắt cụt/sai dung lượng ({dl_size} != {exp_size} bytes).",
                    )
                if exp_sha and exp_sha != dl_sha:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"Checksum SHA256 file tải về không khớp artifact từ Colab.",
                    )
                qc_report["checks"].append("artifact_size_sha256")

            remote_probe = remote_meta.get("output_video_probe")
            if isinstance(remote_probe, dict):
                if remote_probe.get("video_codec") is None or remote_probe.get("audio_codec") is None:
                    _persist_qc_and_fail(
                        CloudErrorCode.QC_FAILED.value,
                        "Probe từ Colab thiếu video_codec hoặc audio_codec.",
                    )
                if remote_probe.get("size_bytes") is not None and int(remote_probe["size_bytes"]) != dl_size:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        f"Kích thước tải về ({dl_size}) khác output_video_probe ({remote_probe['size_bytes']}).",
                    )
                if remote_probe.get("sha256") and str(remote_probe["sha256"]) != dl_sha:
                    _persist_qc_and_fail(
                        CloudErrorCode.DOWNLOAD_FAILED.value,
                        "SHA256 tải về không khớp output_video_probe.",
                    )

        local_probe = probe_video_metadata(dl_file)
        qc_report["local_probe"] = local_probe
        if local_probe.get("probe_method") == "ffprobe":
            if not local_probe.get("video_codec"):
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, "QC local: Output thiếu stream hình ảnh.")
            if not local_probe.get("audio_codec"):
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, "QC local: Output thiếu stream âm thanh.")
            dur_s = local_probe.get("duration_seconds")
            if dur_s is None or not math.isfinite(float(dur_s)) or float(dur_s) <= 0.0:
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, f"QC local: Thời lượng output không hợp lệ ({dur_s}).")
            expected_dur = (
                (source_fingerprint or {}).get("duration_seconds")
                or remote_meta.get("input_duration_seconds")
            )
            if expected_dur is not None and float(expected_dur) > 0.0:
                tol = max(1.5, float(expected_dur) * 0.15)
                if abs(float(dur_s) - float(expected_dur)) > tol:
                    _persist_qc_and_fail(
                        CloudErrorCode.QC_FAILED.value,
                        f"QC local: Thời lượng output ({float(dur_s):.2f}s) lệch quá ngưỡng so với nguồn ({float(expected_dur):.2f}s).",
                    )
            qc_report["checks"].append("local_ffprobe_streams_and_duration")
        else:
            if not local_probe.get("header_valid") or dl_size < 24:
                _persist_qc_and_fail(
                    CloudErrorCode.QC_FAILED.value,
                    "QC local: File tải về không có header container video hợp lệ hoặc quá nhỏ.",
                )
            qc_report["checks"].append("local_container_header")

        if "segments_count" not in remote_meta:
            _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value,
                                 "Full-pipeline output is missing its caption verification metadata.")
        if "segments_count" in remote_meta:
            from .gpu_scripts.caption_geometry import CaptionGeometryError, validate_caption_evidence
            try:
                validate_caption_evidence(remote_meta.get("source_caption_evidence"), cue_count=seg_count)
            except CaptionGeometryError as exc:
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value, str(exc))
            timing = remote_meta.get("subtitle_timing")
            if seg_count and (not isinstance(timing, dict) or timing.get("passed") is not True
                              or timing.get("cue_count") != seg_count
                              or timing.get("source_mask_count", 0) != remote_meta["source_caption_evidence"].get("source_mask_count")):
                _persist_qc_and_fail(CloudErrorCode.QC_FAILED.value,
                                     "Rendered source-caption coverage has not been verified.")
            qc_report["checks"].append("source_caption_geometry_and_ass_coverage")

        qc_report["passed"] = True
        try:
            (attempt_res_dir / "qc_report.json").write_text(
                redact_secrets(json.dumps(qc_report, indent=2, ensure_ascii=False), extra_secrets=extra_secrets),
                encoding="utf-8",
            )
        except OSError:
            pass

        return {
            "qc_report": qc_report,
            "local_probe": local_probe,
            "remote_metadata": remote_meta,
            "sha256": dl_sha,
            "size_bytes": dl_size,
        }

    def export_deliverable_atomic(
        self,
        dl_file: Path,
        final_out_dir: Path,
        *,
        job_id: str,
        attempt: int,
        video_name: str,
        expected_sha256: str,
    ) -> Path:
        """
        Xuất file thành phẩm theo tên chứa job_id & attempt (chống ghi đè bản cũ),
        ghi vào file .part trên cùng filesystem rồi os.replace nguyên tử sau khi xác minh checksum.
        """
        final_out_dir.mkdir(parents=True, exist_ok=True)
        raw_base = Path(str(video_name or "video.mp4").replace("\x00", "")).name
        parsed_base = Path(raw_base)
        safe_stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", parsed_base.stem).strip(". ") or "video"
        raw_ext = parsed_base.suffix.lower()
        safe_ext = raw_ext if re.fullmatch(r"\.[a-z0-9]{1,8}", raw_ext or "") else ".mp4"
        safe_job = re.sub(r"[^a-zA-Z0-9_-]", "", str(job_id)) or "job"
        safe_attempt = max(1, int(attempt or 1))
        prefix = str(DEFAULT_CONFIG.get("output_prefix") or "cloud_")

        candidate = final_out_dir / f"{prefix}{safe_stem}__{safe_job}_a{safe_attempt}{safe_ext}"
        version_idx = 2
        while candidate.exists():
            candidate = final_out_dir / f"{prefix}{safe_stem}__{safe_job}_a{safe_attempt}_v{version_idx}{safe_ext}"
            version_idx += 1

        tmp_part = final_out_dir / f".{candidate.name}.{uuid.uuid4().hex[:8]}.part"
        expected_size = dl_file.stat().st_size
        try:
            final_out_dir.mkdir(parents=True, exist_ok=True)
            with open(dl_file, "rb") as src, open(tmp_part, "wb") as dst:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    dst.write(buf)
                dst.flush()
                os.fsync(dst.fileno())

            if not tmp_part.is_file() or tmp_part.stat().st_size != expected_size:
                raise _OrchestratorStageError(
                    CloudErrorCode.EXPORT_FAILED.value,
                    "Kích thước file tạm .part không khớp sau khi sao chép.",
                )
            if expected_sha256 and _file_sha256(tmp_part) != expected_sha256:
                raise _OrchestratorStageError(
                    CloudErrorCode.EXPORT_FAILED.value,
                    "Checksum SHA256 file tạm .part bị sai lệch khi xuất thành phẩm.",
                )
            os.replace(tmp_part, candidate)
        except OSError as exc:
            err_code = (
                CloudErrorCode.ENV_DISK_INSUFFICIENT.value
                if getattr(exc, "errno", None) == errno.ENOSPC
                else CloudErrorCode.EXPORT_FAILED.value
            )
            raise _OrchestratorStageError(err_code, f"Lỗi xuất file thành phẩm an toàn: {exc}") from exc
        finally:
            if tmp_part.exists():
                try:
                    tmp_part.unlink()
                except OSError:
                    pass

        if not candidate.is_file() or candidate.stat().st_size != expected_size:
            raise _OrchestratorStageError(
                CloudErrorCode.EXPORT_FAILED.value,
                "Không xác nhận được file thành phẩm sau os.replace.",
            )
        return candidate

    def process_job(
        self,
        job: CloudJob,
        progress_callback: Optional[Callable[[int, str], None]] = None,
        *,
        owner_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> bool:
        """
        Thực thi trọn vẹn 1 công việc trên Colab GPU theo hợp đồng trạng thái Phase 2.
        Kiểm tra hủy & lease trước/sau mỗi bước chặn và chỉ xác nhận `cancelled`
        sau khi đã xác nhận session của job/attempt dừng thực sự.
        """
        run = _JobRun.from_job(
            self, job, progress_callback, owner_id=owner_id, cancel_event=cancel_event
        )
        try:
            run._raise_if_aborted()
            if not run._stage_preflight():
                return False
            if not run._stage_budget_check():
                return False
            if not run._stage_provision():
                return False
            run._stage_upload()
            run._stage_setup()
            run._stage_pipeline()
            run._stage_download()
            run._stage_validate()
            run._stage_cache_save()
            return run._stage_cleanup()
        except _JobAborted as abort_exc:
            return run._handle_job_aborted(abort_exc)
        except Exception as exc:
            return run._handle_job_exception(exc)
        finally:
            if run.session_may_exist and not run.cleanup_done:
                try:
                    run._finalize_session_once()
                except Exception as stop_exc:
                    logger.warning(
                        "Lỗi thu hồi session %s: %s",
                        run.session_name,
                        redact_secrets(str(stop_exc), extra_secrets=run.extra_secrets),
                    )
            run._mark_stage(None)
            logger.info(
                "CLOUD_STAGE_TIMINGS job=%s %s",
                run.job_id,
                json.dumps(
                    {
                        "stages": run.stage_timings,
                        "cu_rate_per_hour": run.rate_per_hour,
                        "measurement_source": run._cu_measurement_source(),
                    },
                    ensure_ascii=False,
                ),
            )
