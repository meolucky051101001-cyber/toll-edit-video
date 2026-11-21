"""
cloud_job_store.py - Quản lý hàng đợi công việc Cloud GPU bằng SQLite (WAL mode).
Giai đoạn 2: Máy trạng thái có điều kiện version/attempt, lease & heartbeat, huỷ thật
có đối soát thu hồi session và bản ghi cloud_cleanup_events.
Giai đoạn 3: Sổ chi phí cloud_cost_ledger, giữ chỗ CU nguyên tử cùng claim,
phân bổ theo timezone qua nửa đêm và đối soát provider/cleanup.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .cloud_config import DEFAULT_CONFIG
from .cloud_contract import (
    CHECKABLE_GPUS,
    CloudCleanupStatus,
    CloudCostMeasurementSource,
    CloudErrorCode,
    CloudJobState,
    CloudLedgerStatus,
    GPU_CHECK_TASK,
    cloud_deadline_seconds,
    GPU_CU_RATES,
    TERMINAL_STATES,
    can_transition,
    get_error_remediation,
    normalize_state,
    resolve_cloud_source_channel,
    to_legacy_state,
)
from .cloud_security import CloudValidationError, redact_secrets

logger = logging.getLogger("cloud.job_store")


def _parse_optional_json_dict(raw_val: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw_val:
        return None
    try:
        parsed = json.loads(raw_val)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError):
        return None

DEFAULT_LEASE_SECONDS = 60.0
MAX_CONCURRENT_JOBS = 4
_CONCURRENCY_SETTING_KEY = "max_concurrent_jobs"
_GPU_CONCURRENCY_KEY_PREFIX = "max_concurrent_jobs:"

_TERMINAL_DB_STATES = ("completed", "cancelled", "failed", "error", "stopped")
_ACTIVE_DB_STATES = (
    "provisioning",
    "creating_session",
    "uploading",
    "preparing",
    "processing",
    "downloading",
    "validating",
    "cancel_requested",
    "cancelling",
)
_CANCEL_DB_STATES = ("cancel_requested", "cancelling", "cancelled", "stopped")
_HOLD_LEDGER_STATUSES = (
    CloudLedgerStatus.RESERVED.value,
    CloudLedgerStatus.ACTIVE.value,
    CloudLedgerStatus.PENDING_CLEANUP.value,
    CloudLedgerStatus.UNRECONCILED.value,
)
_ROLLOUT_KEY = "rollout_admission"
_ACTIVE_STATES_SQL = ", ".join("?" for _ in _ACTIVE_DB_STATES)


def _resolve_tz(tz_name: Optional[str] = None) -> dt.tzinfo:
    """Resolve configured budget timezone safely (handles Windows missing tzdata)."""
    name = str(tz_name or DEFAULT_CONFIG.get("budget_timezone") or "Asia/Ho_Chi_Minh").strip()
    if not name or name.lower() in ("asia/ho_chi_minh", "asia/saigon", "ict", "+07:00", "+07"):
        try:
            return ZoneInfo("Asia/Ho_Chi_Minh")
        except Exception:
            return dt.timezone(dt.timedelta(hours=7))
    if name.upper() in ("UTC", "Z"):
        return dt.timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone(dt.timedelta(hours=7))


def _day_bounds_epoch(target_ts: float, tz_name: Optional[str] = None) -> Tuple[float, float, str]:
    """Return [start_epoch, end_epoch, YYYY-MM-DD] in the configured budget timezone."""
    tz = _resolve_tz(tz_name)
    local_dt = dt.datetime.fromtimestamp(float(target_ts), tz=tz)
    day_start = dt.datetime(local_dt.year, local_dt.month, local_dt.day, tzinfo=tz)
    day_end = day_start + dt.timedelta(days=1)
    return day_start.timestamp(), day_end.timestamp(), day_start.strftime("%Y-%m-%d")


# A parked session is stopped at expiry; allow this long for the stop to take effect.
_WARM_IDLE_STOP_GRACE_SECONDS = 60.0


def _allocate_cu_to_window(
    amount: float,
    started_at: float,
    ended_at: float,
    win_start: float,
    win_end: float,
) -> float:
    """Pro-rata allocate CU usage across midnight according to active usage duration."""
    val = max(0.0, float(amount or 0.0))
    if val <= 0.0:
        return 0.0
    s = float(started_at)
    e = float(ended_at)
    if e <= s:
        return val if win_start <= s < win_end else 0.0
    ov_start = max(s, float(win_start))
    ov_end = min(e, float(win_end))
    if ov_end <= ov_start:
        return 0.0
    return val * ((ov_end - ov_start) / (e - s))


def _extract_provider_observed_ts(usage: Dict[str, Any], now_ts: float) -> float:
    for key in ("observed_at", "as_of", "timestamp"):
        raw = usage.get(key)
        if raw is None or raw == "":
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            iso_val = str(raw).strip()
            if iso_val.endswith("Z"):
                iso_val = iso_val[:-1] + "+00:00"
            return dt.datetime.fromisoformat(iso_val).timestamp()
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Thời điểm quan sát số dư Colab không hợp lệ") from exc
    if usage.get("age_seconds") is not None:
        age = float(usage["age_seconds"])
        if not math.isfinite(age):
            raise ValueError("Tuổi snapshot số dư Colab không hợp lệ")
        return now_ts - max(0.0, age)
    return now_ts


def _normalize_provider_usage_payload(
    usage: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Normalize Colab provider usage response and evaluate staleness / connectivity."""
    now_ts = time.time() if now is None else float(now)
    stale_limit = float(DEFAULT_CONFIG.get("provider_balance_stale_seconds", 300))
    ok = bool(usage.get("ok"))
    raw_bal = usage.get("balance")
    balance: Optional[float] = None
    if raw_bal is not None and raw_bal != "":
        try:
            balance = float(raw_bal)
        except (TypeError, ValueError):
            balance = None

    invalid_observation = False
    try:
        observed_at = _extract_provider_observed_ts(usage, now_ts)
        if not math.isfinite(observed_at) or observed_at > now_ts + 5.0:
            raise ValueError("Thời điểm snapshot Colab không hợp lệ hoặc ở tương lai")
    except (TypeError, ValueError, OverflowError):
        observed_at = 0.0
        invalid_observation = True
    invalid_balance = balance is None or not math.isfinite(balance) or balance < 0.0
    if invalid_balance:
        balance = None
    ok = ok and not invalid_balance and not invalid_observation
    age_seconds = max(0.0, now_ts - observed_at)
    stale_flag = bool(usage.get("stale", False)) or invalid_observation
    is_stale = stale_flag or (age_seconds > stale_limit)
    raw_err = usage.get("error")
    safe_err = redact_secrets(str(raw_err)) if raw_err else None
    if not safe_err and (invalid_balance or invalid_observation):
        safe_err = "Số dư hoặc thời điểm snapshot Colab không hợp lệ"

    if not ok:
        status = "disconnected"
    elif is_stale:
        status = "stale"
    else:
        status = "ok"

    return {
        "status": status,
        "ok": ok,
        "balance": balance,
        "observed_at": observed_at,
        "recorded_at": now_ts,
        "age_seconds": round(age_seconds, 2),
        "stale_flag": stale_flag,
        "is_stale": is_stale,
        "error": safe_err,
    }


@dataclass
class CloudJob:
    id: int
    job_id: str
    video_path: str
    video_name: str
    gpu_type: str
    task_type: str
    state: str
    percent: int
    step_name: str
    session_name: Optional[str]
    cu_estimated: float
    cu_actual: float
    created_at: float
    started_at: Optional[float]
    completed_at: Optional[float]
    result_path: Optional[str]
    error: Optional[str]
    voice: str = "BV562_streaming"
    version: int = 1
    attempt: int = 0
    owner_id: Optional[str] = None
    lease_expires_at: Optional[float] = None
    heartbeat_at: Optional[float] = None
    cancel_requested_at: Optional[float] = None
    error_code: Optional[str] = None
    cleanup_status: str = "none"
    cleanup_attempts: int = 0
    cleanup_last_error: Optional[str] = None
    updated_at: Optional[float] = None
    cu_buffer: float = 0.0
    cu_reserved: float = 0.0
    hidden_from_ui: int = 0
    output_manifest: Optional[Dict[str, Any]] = None
    idempotency_key: Optional[str] = None
    options: Optional[Dict[str, Any]] = None
    deadline_at: Optional[float] = None

    @property
    def legacy_state(self) -> str:
        try:
            return to_legacy_state(self.state)
        except ValueError:
            return "error"

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["legacy_state"] = self.legacy_state
        data["error_remediation"] = get_error_remediation(self.error_code)
        return data


class CloudJobStore:
    def __init__(
        self, db_path: Optional[str | Path] = None, *, rollout_controlled: bool = True
    ):
        self.db_path = Path(db_path or DEFAULT_CONFIG["db_path"])
        self.rollout_controlled = rollout_controlled
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._claim_lock = threading.Lock()
        self._claim_rejections: Dict[str, Dict[str, Any]] = {}
        self._init_db()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=20, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=20000")
            yield db
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT UNIQUE NOT NULL,
                        video_path TEXT NOT NULL,
                        video_name TEXT NOT NULL,
                        gpu_type TEXT NOT NULL DEFAULT 'T4',
                        task_type TEXT NOT NULL DEFAULT 'full_pipeline',
                        state TEXT NOT NULL DEFAULT 'queued',
                        percent INTEGER DEFAULT 0,
                        step_name TEXT DEFAULT 'Đang xếp hàng...',
                        session_name TEXT,
                        cu_estimated REAL DEFAULT 0.0,
                        cu_actual REAL DEFAULT 0.0,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        completed_at REAL,
                        result_path TEXT,
                        error TEXT,
                        voice TEXT DEFAULT 'BV562_streaming',
                        version INTEGER NOT NULL DEFAULT 1,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        owner_id TEXT,
                        lease_expires_at REAL,
                        heartbeat_at REAL,
                        cancel_requested_at REAL,
                        error_code TEXT,
                        cleanup_status TEXT NOT NULL DEFAULT 'none',
                        cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                        cleanup_last_error TEXT,
                        updated_at REAL,
                        cu_buffer REAL DEFAULT 0.0,
                        cu_reserved REAL DEFAULT 0.0,
                        hidden_from_ui INTEGER NOT NULL DEFAULT 0,
                        output_manifest_json TEXT,
                        idempotency_key TEXT,
                        options_json TEXT
                    )
                    """
                )
                existing_cols = {
                    r[1] for r in db.execute("PRAGMA table_info(cloud_jobs)").fetchall()
                }
                migrations = {
                    "voice": "TEXT DEFAULT 'BV562_streaming'",
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "attempt": "INTEGER NOT NULL DEFAULT 0",
                    "owner_id": "TEXT",
                    "lease_expires_at": "REAL",
                    "heartbeat_at": "REAL",
                    "cancel_requested_at": "REAL",
                    "error_code": "TEXT",
                    "cleanup_status": "TEXT NOT NULL DEFAULT 'none'",
                    "cleanup_attempts": "INTEGER NOT NULL DEFAULT 0",
                    "cleanup_last_error": "TEXT",
                    "updated_at": "REAL",
                    "cu_buffer": "REAL DEFAULT 0.0",
                    "cu_reserved": "REAL DEFAULT 0.0",
                    "hidden_from_ui": "INTEGER NOT NULL DEFAULT 0",
                    "output_manifest_json": "TEXT",
                    "idempotency_key": "TEXT",
                    "options_json": "TEXT",
                    "deadline_at": "REAL",
                    "last_replayed_at": "REAL",
                }
                for col_name, col_def in migrations.items():
                    if col_name not in existing_cols:
                        db.execute(f"ALTER TABLE cloud_jobs ADD COLUMN {col_name} {col_def}")
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cloud_jobs_idempotency_key "
                    "ON cloud_jobs(idempotency_key) WHERE idempotency_key IS NOT NULL"
                )

                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_cleanup_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        session_name TEXT NOT NULL,
                        owner_id TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(job_id, attempt, session_name)
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_cost_ledger (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        attempt INTEGER NOT NULL,
                        gpu_type TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        cu_estimated REAL NOT NULL DEFAULT 0.0,
                        cu_buffer REAL NOT NULL DEFAULT 0.0,
                        cu_reserved REAL NOT NULL DEFAULT 0.0,
                        cu_observed REAL NOT NULL DEFAULT 0.0,
                        cu_reconciled REAL,
                        measurement_source TEXT NOT NULL DEFAULT 'unverified_estimate',
                        status TEXT NOT NULL DEFAULT 'reserved',
                        job_state TEXT NOT NULL DEFAULT 'queued',
                        cleanup_verified INTEGER NOT NULL DEFAULT 0,
                        usage_started_at REAL NOT NULL,
                        usage_ended_at REAL,
                        provider_balance_before REAL,
                        provider_balance_after REAL,
                        notes TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(job_id, attempt)
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cost_ledger_job_attempt "
                    "ON cloud_cost_ledger (job_id, attempt)"
                )
                ledger_columns = {r[1] for r in db.execute("PRAGMA table_info(cloud_cost_ledger)")}
                if "legacy_hourly_rate" not in ledger_columns:
                    db.execute("ALTER TABLE cloud_cost_ledger ADD COLUMN legacy_hourly_rate REAL NOT NULL DEFAULT 0")
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_warm_sessions (
                        session_name TEXT PRIMARY KEY,
                        gpu_type TEXT NOT NULL,
                        source_job_id TEXT,
                        parked_at REAL NOT NULL,
                        expires_at REAL NOT NULL,
                        keepalive_mode INTEGER NOT NULL DEFAULT 0,
                        owner_pid INTEGER
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS cloud_warm_idle_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_name TEXT NOT NULL,
                        gpu_type TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        ended_at REAL NOT NULL,
                        cu REAL NOT NULL
                    )
                    """
                )
                self._migrate_legacy_costs(db)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def _migrate_legacy_costs(self, db: sqlite3.Connection) -> None:
        """Import the former durable ledger once, including deleted UI history.

        An open legacy allocation remains unreconciled and keeps accruing its
        recorded rate until scoped cleanup is verified. Migration never changes
        a job's state or treats a process restart as proof the VM stopped.
        """
        now = time.time()
        jobs = {row["job_id"]: row for row in db.execute("SELECT * FROM cloud_jobs")}
        entries = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_cu_ledger'").fetchone():
            entries = {row["job_id"]: dict(row) for row in db.execute("SELECT * FROM cloud_cu_ledger")}
        for job_id, job in jobs.items():
            if job_id not in entries and float(job["cu_actual"] or 0) > 0:
                end = float(job["completed_at"] or job["started_at"] or job["created_at"])
                entries[job_id] = dict(job_id=job_id, started_at=end, ended_at=end,
                                       hourly_rate=0., cu_actual=job["cu_actual"])
        for job_id, entry in entries.items():
            if db.execute("SELECT 1 FROM cloud_cost_ledger WHERE job_id=?", (job_id,)).fetchone():
                continue
            job = jobs.get(job_id)
            start = float(entry["started_at"])
            end = float(entry["ended_at"]) if entry["ended_at"] is not None else None
            rate = max(0., float(entry.get("hourly_rate") or 0.))
            observed = max(0., float(entry.get("cu_actual") or 0.))
            if rate > 0:
                observed = max(observed, max(0., (now if end is None else end) - start) * rate / 3600)
            if job is not None:
                observed = max(observed, float(job["cu_actual"] or 0.))
            attempt = max(1, int(job["attempt"] or 0)) if job is not None else 1
            closed = end is not None
            state = self._canonical_state_str(job["state"]) if job is not None else "failed"
            db.execute("""INSERT INTO cloud_cost_ledger (
                job_id, attempt, gpu_type, task_type, cu_observed, cu_reconciled,
                measurement_source, status, job_state, cleanup_verified,
                usage_started_at, usage_ended_at, notes, created_at, updated_at, legacy_hourly_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, attempt, job["gpu_type"] if job is not None else "T4",
                 job["task_type"] if job is not None else "full_pipeline", observed,
                 observed if closed else None, CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value
                 if rate > 0 else CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value,
                 CloudLedgerStatus.RECONCILED.value if closed else CloudLedgerStatus.UNRECONCILED.value,
                 state, int(closed), start, end, "Imported from legacy Cloud billing", start, now, rate))
            if job is not None:
                # Reserve the imported attempt so a later retry cannot overwrite it.
                db.execute("UPDATE cloud_jobs SET attempt=MAX(attempt, ?) WHERE job_id=?", (attempt, job_id))
                if not closed and job["session_name"]:
                    db.execute("""INSERT OR IGNORE INTO cloud_cleanup_events
                        (job_id, attempt, session_name, status, created_at, updated_at)
                        VALUES (?, ?, ?, 'failed_retry_needed', ?, ?)""",
                        (job_id, attempt, job["session_name"], start, now))

    @staticmethod
    def _is_terminal_db_state(raw_state: str) -> bool:
        try:
            return normalize_state(raw_state) in TERMINAL_STATES
        except ValueError:
            return raw_state in _TERMINAL_DB_STATES

    @staticmethod
    def _canonical_state_str(raw_state: str) -> str:
        return normalize_state(raw_state).value

    @staticmethod
    def calculate_reservation_breakdown(
        gpu_type: str = "T4",
        cu_estimated: float = 0.0,
        *,
        cu_buffer: Optional[float] = None,
    ) -> Dict[str, float]:
        """Tính phần dự phòng stop-latency & billing rounding quantum cho reservation."""
        est = max(0.0, float(cu_estimated or 0.0))
        quantum = max(0.001, float(DEFAULT_CONFIG.get("billing_rounding_quantum_cu", 0.02)))
        rate = float(GPU_CU_RATES.get(str(gpu_type or "T4").upper(), 1.96))
        latency_sec = max(0.0, float(DEFAULT_CONFIG.get("stop_latency_buffer_seconds", 60)))

        if cu_buffer is None:
            raw_buf = (latency_sec / 3600.0) * rate + quantum
            buf = round(math.ceil(raw_buf / quantum) * quantum, 4)
        else:
            buf = max(0.0, round(float(cu_buffer), 4))

        raw_total = est + buf
        reserved = round(math.ceil(raw_total / quantum) * quantum, 4) if raw_total > 0.0 else 0.0
        return {
            "cu_estimated": round(est, 4),
            "cu_buffer": round(buf, 4),
            "cu_reserved": reserved,
        }

    def _set_claim_rejection(self, job_id: str, rejection: Dict[str, Any]) -> None:
        with self._claim_lock:
            self._claim_rejections[job_id] = dict(rejection)

    def consume_last_claim_rejection(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._claim_lock:
            return self._claim_rejections.pop(job_id, None)

    @staticmethod
    def _rollout_in_db(db: sqlite3.Connection) -> Dict[str, Any]:
        row = db.execute("SELECT value FROM cloud_settings WHERE key = ?", (_ROLLOUT_KEY,)).fetchone()
        phase, _, value = (row["value"] if row else "paused").partition(":")
        if phase == "armed" and value:
            return {"phase": phase, "permit_id": value}
        if phase == "admitted" and value:
            return {"phase": phase, "job_id": value}
        return {"phase": "paused"}

    @staticmethod
    def _write_rollout_in_db(db: sqlite3.Connection, value: str) -> None:
        db.execute(
            "INSERT INTO cloud_settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (_ROLLOUT_KEY, value, time.time()),
        )

    @staticmethod
    def _has_active_job_in_db(db: sqlite3.Connection) -> bool:
        return db.execute(
            f"SELECT 1 FROM cloud_jobs WHERE state IN ({_ACTIVE_STATES_SQL}) LIMIT 1",
            _ACTIVE_DB_STATES,
        ).fetchone() is not None

    @staticmethod
    def _concurrency_limits_in_db(db: sqlite3.Connection) -> tuple[int, dict[str, int]]:
        rows = db.execute(
            "SELECT key, value FROM cloud_settings WHERE key = ? OR key LIKE ?",
            (_CONCURRENCY_SETTING_KEY, f"{_GPU_CONCURRENCY_KEY_PREFIX}%"),
        )
        limits = {}
        for row in rows:
            try:
                limits[row["key"]] = min(MAX_CONCURRENT_JOBS, max(1, int(row["value"])))
            except (TypeError, ValueError, OverflowError):
                continue
        fallback = limits.get(_CONCURRENCY_SETTING_KEY, 1)
        return fallback, {gpu: limits[key] for gpu in CHECKABLE_GPUS
                          if (key := f"{_GPU_CONCURRENCY_KEY_PREFIX}{gpu}") in limits}

    @classmethod
    def _concurrency_limit_in_db(cls, db: sqlite3.Connection, gpu_type: Optional[str] = None) -> int:
        fallback, by_gpu = cls._concurrency_limits_in_db(db)
        if gpu_type:
            return by_gpu.get(gpu_type, fallback)
        return max([fallback, *by_gpu.values()])

    def get_concurrency_limit(self, gpu_type: Optional[str] = None) -> int:
        """Read the total capacity or the limit for a given GPU."""
        with self._connect() as db:
            return self._concurrency_limit_in_db(db, gpu_type)

    def get_gpu_concurrency_limits(self) -> dict[str, int]:
        with self._connect() as db:
            fallback, by_gpu = self._concurrency_limits_in_db(db)
            return {gpu: by_gpu.get(gpu, fallback) for gpu in CHECKABLE_GPUS}

    @staticmethod
    def _validate_concurrency_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CONCURRENT_JOBS:
            raise ValueError(f"Số luồng Cloud phải là số nguyên từ 1 đến {MAX_CONCURRENT_JOBS}.")

    def set_concurrency_limit(self, limit: int) -> int:
        """Persist a limit for future claims without interrupting running jobs."""
        self._validate_concurrency_limit(limit)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO cloud_settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (_CONCURRENCY_SETTING_KEY, str(limit), time.time()),
            )
            db.commit()
        return limit

    def set_gpu_concurrency_limits(self, limits: dict[str, int]) -> None:
        """Save GPU limits together without changing already claimed jobs."""
        for gpu, limit in limits.items():
            if gpu not in CHECKABLE_GPUS:
                raise ValueError("Chọn T4, L4, A100 hoặc H100.")
            self._validate_concurrency_limit(limit)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT INTO cloud_settings(key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                [(f"{_GPU_CONCURRENCY_KEY_PREFIX}{gpu}", str(limit), time.time()) for gpu, limit in limits.items()],
            )
            db.commit()

    @staticmethod
    def _has_pending_cleanup_in_db(db: sqlite3.Connection) -> bool:
        # Registration records a future cleanup obligation before VM creation.
        # A matching, healthy live lease owns that obligation; it is not an orphan.
        # Every mismatch, expired lease, cancellation or failed cleanup stays blocking.
        now = time.time()
        return db.execute(
            f"""
            SELECT 1 FROM cloud_cleanup_events AS event
            WHERE event.status != 'verified'
              AND NOT EXISTS (
                SELECT 1 FROM cloud_jobs AS job
                WHERE job.job_id = event.job_id
                  AND job.attempt = event.attempt
                  AND job.session_name = event.session_name
                  AND job.owner_id = event.owner_id
                  AND job.owner_id IS NOT NULL
                  AND length(trim(job.owner_id)) > 0
                  AND job.state IN ({_ACTIVE_STATES_SQL})
                  AND job.state NOT IN ('cancel_requested', 'cancelling')
                  AND job.cancel_requested_at IS NULL
                  AND job.cleanup_status = 'pending'
                  AND job.lease_expires_at > ?
                  AND (job.deadline_at IS NULL OR job.deadline_at > ?)
                  AND event.status = 'pending'
                  AND event.attempts = 0
                  AND event.last_error IS NULL
              )
            LIMIT 1
            """,
            (*_ACTIVE_DB_STATES, now, now),
        ).fetchone() is not None or db.execute(
            "SELECT 1 FROM cloud_cost_ledger WHERE status IN ('pending_cleanup', 'unreconciled') LIMIT 1"
        ).fetchone() is not None

    def get_rollout(self) -> Dict[str, Any]:
        with self._connect() as db:
            return self._rollout_in_db(db)

    def list_rollout_alerts(self, *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        observed_at = time.time() if now is None else now
        recent = observed_at - 86400
        with self._connect() as db:
            rows = db.execute(
                f"""
                SELECT 'CLEANUP_PENDING' AS code, job_id
                    FROM cloud_cleanup_events WHERE status != 'verified'
                UNION
                SELECT 'LEDGER_UNRECONCILED', job_id
                    FROM cloud_cost_ledger WHERE status IN ('pending_cleanup', 'unreconciled')
                UNION
                SELECT 'DEADLINE_EXCEEDED', job_id FROM cloud_jobs
                    WHERE state IN ('queued', {_ACTIVE_STATES_SQL})
                      AND COALESCE(deadline_at, created_at + ?) < ?
                UNION
                SELECT 'QC_FAILED', job_id FROM cloud_jobs
                    WHERE error_code = ? AND COALESCE(completed_at, updated_at) >= ?
                UNION
                SELECT 'DUPLICATE_SUBMISSION', job_id FROM cloud_jobs
                    WHERE last_replayed_at >= ?
                ORDER BY code, job_id
                """,
                (*_ACTIVE_DB_STATES, cloud_deadline_seconds(), observed_at,
                 CloudErrorCode.QC_FAILED.value, recent, recent),
            )
            return [dict(row) for row in rows]

    def set_rollout(self, *, arm: bool) -> Dict[str, Any]:
        """Arm a single submission or pause admissions and claims across processes."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rollout = self._rollout_in_db(db)
            if arm and rollout["phase"] == "armed":
                db.commit()
                return rollout
            if arm:
                previous_job = db.execute(
                    "SELECT state FROM cloud_jobs WHERE job_id = ?", (rollout.get("job_id"),)
                ).fetchone()
                active_count = db.execute(
                    f"SELECT COUNT(*) FROM cloud_jobs WHERE state IN ({_ACTIVE_STATES_SQL})",
                    _ACTIVE_DB_STATES,
                ).fetchone()[0]
                previous_state = str(previous_job["state"] or "") if previous_job else ""
                previous_is_active = previous_state in _ACTIVE_DB_STATES
                previous_is_queued = previous_state == "queued"
                busy_previous = previous_is_queued or (
                    previous_job is not None
                    and not self._is_terminal_db_state(previous_state)
                    and not previous_is_active
                )
                # A Telegram lane may be active while Dashboard rollout is paused
                # or references a completed job. Capacity, not previous origin,
                # decides whether another Dashboard permit can be opened.
                may_rearm_while_active = active_count < self._concurrency_limit_in_db(db)
                if busy_previous or (active_count > 0 and not may_rearm_while_active) or self._has_pending_cleanup_in_db(db):
                    raise CloudValidationError(
                        "Hãy hoàn tất job Cloud và đối soát tài nguyên, chi phí trước khi mở lượt mới.",
                        code=CloudErrorCode.ROLLOUT_BUSY.value, http_status=409,
                    )
            value = f"armed:{uuid.uuid4().hex}" if arm else "paused"
            self._write_rollout_in_db(db, value)
            result = self._rollout_in_db(db)
            db.commit()
            return result

    @staticmethod
    def _record_replay_in_db(db: sqlite3.Connection, job_id: str) -> None:
        db.execute(
            "UPDATE cloud_jobs SET last_replayed_at = ? WHERE job_id = ?", (time.time(), job_id)
        )

    def enqueue_or_get(
        self,
        video_path: str,
        video_name: Optional[str] = None,
        gpu_type: str = "T4",
        task_type: str = "full_pipeline",
        cu_estimated: float = 0.5,
        voice: str = "BV562_streaming",
        *,
        cu_buffer: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        deadline_seconds: Optional[int] = None,
        rollout_permit: Optional[str] = None,
        immediate_owner_id: Optional[str] = None,
        enforce_budget: bool = True,
        provider_usage: Optional[Dict[str, Any]] = None,
    ) -> Tuple[CloudJob, bool]:
        """Đưa job mới vào hàng đợi hoặc trả về (job_cũ, True) nếu trùng idempotency_key."""
        name = video_name or Path(video_path).name
        job_id = f"cloud-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now = time.time()
        clean_idem = str(idempotency_key).strip() if idempotency_key else None
        if clean_idem == "":
            clean_idem = None
        options_json_str = json.dumps(options, ensure_ascii=False) if options is not None else None
        breakdown = self.calculate_reservation_breakdown(
            gpu_type=gpu_type,
            cu_estimated=cu_estimated,
            cu_buffer=cu_buffer,
        )

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if clean_idem is not None:
                    existing = db.execute(
                        "SELECT * FROM cloud_jobs WHERE idempotency_key = ? LIMIT 1",
                        (clean_idem,),
                    ).fetchone()
                    if existing is not None:
                        self._record_replay_in_db(db, existing["job_id"])
                        db.execute("COMMIT")
                        return self._row_to_job(existing), True

                if self.rollout_controlled:
                    rollout = self._rollout_in_db(db)
                    if rollout["phase"] != "armed" or rollout_permit != rollout["permit_id"]:
                        raise CloudValidationError(
                            "Cloud cần một lượt được mở thủ công. Hãy kiểm tra mục Phát hành Cloud.",
                            code=CloudErrorCode.ROLLOUT_PAUSED.value, http_status=409,
                        )
                    self._write_rollout_in_db(db, f"admitted:{job_id}")

                cursor = db.execute(
                    """
                    INSERT INTO cloud_jobs (
                        job_id, video_path, video_name, gpu_type, task_type,
                        state, percent, step_name, cu_estimated, created_at, voice,
                        version, attempt, cleanup_status, cleanup_attempts, updated_at,
                        cu_buffer, cu_reserved, hidden_from_ui, idempotency_key, options_json, deadline_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', 0, 'Đang trong hàng đợi', ?, ?, ?, 1, 0, 'none', 0, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        str(video_path),
                        name,
                        gpu_type.upper(),
                        task_type,
                        breakdown["cu_estimated"],
                        now,
                        voice,
                        now,
                        breakdown["cu_buffer"],
                        breakdown["cu_reserved"],
                        clean_idem,
                        options_json_str,
                        now + cloud_deadline_seconds(deadline_seconds),
                    ),
                )
                if immediate_owner_id:
                    job_obj = self._claim_row(
                        db, job_id, immediate_owner_id, DEFAULT_LEASE_SECONDS, now,
                        enforce_budget=enforce_budget, provider_usage=provider_usage,
                    )
                    if job_obj is None:
                        rejection = self.consume_last_claim_rejection(job_id) or {}
                        raise CloudValidationError(
                            rejection.get("reason") or "Không thể cấp lượt chạy Cloud.",
                            code=rejection.get("code") or CloudErrorCode.ROLLOUT_BUSY.value,
                            http_status=rejection.get("http_status") or 409,
                        )
                else:
                    row = db.execute("SELECT * FROM cloud_jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
                    job_obj = self._row_to_job(row)
                db.execute("COMMIT")
                return job_obj, False
            except Exception:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    def enqueue(
        self,
        video_path: str,
        video_name: Optional[str] = None,
        gpu_type: str = "T4",
        task_type: str = "full_pipeline",
        cu_estimated: float = 0.5,
        voice: str = "BV562_streaming",
        *,
        cu_buffer: Optional[float] = None,
        idempotency_key: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        rollout_permit: Optional[str] = None,
    ) -> CloudJob:
        """Đưa job mới vào hàng đợi (hỗ trợ idempotency_key & options snapshot)."""
        job_obj, _ = self.enqueue_or_get(
            video_path=video_path,
            video_name=video_name,
            gpu_type=gpu_type,
            task_type=task_type,
            cu_estimated=cu_estimated,
            voice=voice,
            cu_buffer=cu_buffer,
            idempotency_key=idempotency_key,
            options=options,
            rollout_permit=rollout_permit,
        )
        return job_obj

    def get_job_by_idempotency_key(self, idempotency_key: str) -> Optional[CloudJob]:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM cloud_jobs WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if row:
                self._record_replay_in_db(db, row["job_id"])
            db.commit()
            return self._row_to_job(row) if row else None

    def get_job(self, job_id: str) -> Optional[CloudJob]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def get_active_job(self) -> Optional[CloudJob]:
        """Lấy job đang chạy hoặc đang huỷ."""
        placeholders = ", ".join("?" for _ in _ACTIVE_DB_STATES)
        with self._connect() as db:
            row = db.execute(
                f"""
                SELECT * FROM cloud_jobs
                WHERE state IN ({placeholders})
                ORDER BY id DESC LIMIT 1
                """,
                _ACTIVE_DB_STATES,
            ).fetchone()
            return self._row_to_job(row) if row else None

    def list_active_jobs(self) -> List[CloudJob]:
        """List every leased/running/cancelling job across all Cloud workers."""
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM cloud_jobs WHERE state IN ({_ACTIVE_STATES_SQL}) ORDER BY id ASC",
                _ACTIVE_DB_STATES,
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def list_live_jobs(self) -> List[CloudJob]:
        """Read every running and waiting job in one query, independent of history limits."""
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT * FROM cloud_jobs WHERE state IN ({_ACTIVE_STATES_SQL}, 'queued')
                     ORDER BY state = 'queued', id""",
                _ACTIVE_DB_STATES,
            ).fetchall()
            return [self._row_to_job(row) for row in rows]

    def get_job_by_row_id(self, row_id: int) -> Optional[CloudJob]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE id = ?", (row_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def _claim_capacity_in_db(self, db: sqlite3.Connection) -> tuple[bool, set[str]]:
        fallback, limits = self._concurrency_limits_in_db(db)
        active = db.execute(
            f"SELECT gpu_type, COUNT(*) AS count FROM cloud_jobs WHERE state IN ({_ACTIVE_STATES_SQL}) GROUP BY gpu_type",
            _ACTIVE_DB_STATES,
        ).fetchall()
        total_limit = max([fallback, *limits.values()])
        full_gpus = {row["gpu_type"] for row in active if row["count"] >= limits.get(row["gpu_type"], fallback)}
        return sum(row["count"] for row in active) < total_limit, full_gpus

    def _claim_row(
        self,
        db: sqlite3.Connection,
        job_id: str,
        owner_id: str,
        lease_seconds: float,
        now: float,
        *,
        enforce_budget: bool = True,
        provider_usage: Optional[Dict[str, Any]] = None,
    ) -> Optional[CloudJob]:
        has_capacity, full_gpus = self._claim_capacity_in_db(db)
        if not has_capacity:
            return None
        if self._has_pending_cleanup_in_db(db) or (
            self.rollout_controlled and self._rollout_in_db(db).get("job_id") != job_id
        ):
            return None
        row = db.execute(
            """
            SELECT * FROM cloud_jobs
            WHERE job_id = ?
              AND state = 'queued'
              AND cancel_requested_at IS NULL
            """,
            (job_id,),
        ).fetchone()
        if not row or row["gpu_type"] in full_gpus:
            return None

        has_stored_res = row["cu_reserved"] is not None and float(row["cu_reserved"]) > 0.0
        if has_stored_res:
            cu_est = max(0.0, float(row["cu_estimated"] or 0.0))
            cu_buf = max(0.0, float(row["cu_buffer"] or 0.0))
            cu_res = float(row["cu_reserved"])
        else:
            breakdown = self.calculate_reservation_breakdown(
                gpu_type=row["gpu_type"],
                cu_estimated=float(row["cu_estimated"] or 0.0),
            )
            cu_est = breakdown["cu_estimated"]
            cu_buf = breakdown["cu_buffer"]
            cu_res = breakdown["cu_reserved"]

        snap = self._admission_provider_snapshot_in_db(db, provider_usage, now=now)
        prov_bal_before = snap.get("balance") if snap else None

        if enforce_budget:
            decision = self._evaluate_admission_in_db(
                db,
                cu_needed=cu_res,
                provider_usage=None,
                require_fresh_provider=False,
                now_ts=now,
                record_snapshot=False,
            )
            if not decision.get("allowed", True):
                self._set_claim_rejection(job_id, decision)
                return None

        lease_until = now + max(1.0, float(lease_seconds))
        next_attempt = 1 if int(row["attempt"] or 0) <= 0 else int(row["attempt"]) + 1
        cur = db.execute(
            """
            UPDATE cloud_jobs
            SET state = 'provisioning',
                attempt = ?,
                version = COALESCE(version, 1) + 1,
                owner_id = ?,
                heartbeat_at = ?,
                lease_expires_at = ?,
                started_at = COALESCE(started_at, ?),
                updated_at = ?,
                percent = 5,
                cu_buffer = ?,
                cu_reserved = ?,
                step_name = 'Đang khởi tạo máy ảo GPU Colab...'
            WHERE job_id = ?
              AND state = 'queued'
              AND cancel_requested_at IS NULL
            """,
            (
                next_attempt,
                owner_id,
                now,
                lease_until,
                now,
                now,
                cu_buf,
                cu_res,
                job_id,
            ),
        )
        if cur.rowcount != 1:
            return None

        self._upsert_claimed_ledger_in_db(
            db,
            job_id=job_id,
            attempt=next_attempt,
            gpu_type=row["gpu_type"],
            task_type=row["task_type"],
            cu_estimated=cu_est,
            cu_buffer=cu_buf,
            cu_reserved=cu_res,
            now=now,
            provider_balance_before=prov_bal_before,
        )
        updated_row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(updated_row) if updated_row else None

    def claim_next(
        self,
        owner_id: Optional[str] = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        *,
        enforce_budget: bool = True,
        provider_usage: Optional[Dict[str, Any]] = None,
        job_id: Optional[str] = None,
        exclude_source_channel: Optional[str] = None,
    ) -> Optional[CloudJob]:
        """Lấy job kế tiếp ở trạng thái queued và cấp lease + giữ chỗ ngân sách nguyên tử."""
        claim_owner = owner_id or f"worker-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rollout_job_id = self._rollout_in_db(db).get("job_id") if self.rollout_controlled else None
                if self.rollout_controlled and not rollout_job_id:
                    db.commit()
                    return None
                has_capacity, full_gpus = self._claim_capacity_in_db(db)
                if not has_capacity:
                    db.commit()
                    return None
                rows = db.execute(
                    """
                    SELECT job_id, gpu_type, options_json, idempotency_key FROM cloud_jobs
                    WHERE state = 'queued' AND cancel_requested_at IS NULL
                      AND (? IS NULL OR job_id = ?)
                      AND (? IS NULL OR job_id = ?)
                    ORDER BY id ASC
                    """, (rollout_job_id, rollout_job_id, job_id, job_id),
                ).fetchall()
                row = None
                for candidate in rows:
                    if candidate["gpu_type"] in full_gpus:
                        continue
                    channel = resolve_cloud_source_channel(
                        _parse_optional_json_dict(candidate["options_json"]), candidate["idempotency_key"],
                    )
                    if exclude_source_channel and channel == exclude_source_channel:
                        continue
                    row = candidate
                    break
                if not row:
                    db.execute("COMMIT")
                    return None
                claimed = self._claim_row(
                    db,
                    row["job_id"],
                    claim_owner,
                    lease_seconds,
                    now,
                    enforce_budget=enforce_budget,
                    provider_usage=provider_usage,
                )
                db.execute("COMMIT")
                return claimed
            except Exception:
                db.execute("ROLLBACK")
                raise

    def claim_job(
        self,
        job_id: str,
        owner_id: str,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        *,
        enforce_budget: bool = True,
        provider_usage: Optional[Dict[str, Any]] = None,
    ) -> Optional[CloudJob]:
        """Claim nguyên tử một job cụ thể đang queued cùng giữ chỗ ngân sách."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                claimed = self._claim_row(
                    db,
                    job_id,
                    owner_id,
                    lease_seconds,
                    now,
                    enforce_budget=enforce_budget,
                    provider_usage=provider_usage,
                )
                db.execute("COMMIT")
                return claimed
            except Exception:
                db.execute("ROLLBACK")
                raise

    def heartbeat(
        self,
        job_id: str,
        owner_id: str,
        attempt: int,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        """Gia hạn lease cho đúng owner và attempt hiện tại."""
        now = time.time()
        lease_until = now + max(1.0, float(lease_seconds))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    """
                    UPDATE cloud_jobs
                    SET heartbeat_at = ?,
                        lease_expires_at = ?,
                        updated_at = ?,
                        version = COALESCE(version, 1) + 1
                    WHERE job_id = ?
                      AND owner_id = ?
                      AND attempt = ?
                      AND state NOT IN ('completed', 'cancelled', 'failed', 'error', 'stopped')
                    """,
                    (now, lease_until, now, job_id, owner_id, int(attempt)),
                )
                db.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise

    def register_session(
        self,
        job_id: str,
        attempt: int,
        owner_id: str,
        session_name: str,
    ) -> bool:
        """Gắn quyền sở hữu session Colab theo (job_id, attempt, owner_id) trước khi tạo VM."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return False
                if self._is_terminal_db_state(row["state"]):
                    db.execute("COMMIT")
                    return False
                if row["state"] in ("cancel_requested", "cancelling") or row["cancel_requested_at"] is not None:
                    db.execute("COMMIT")
                    return False
                if row["owner_id"] not in (None, owner_id):
                    db.execute("COMMIT")
                    return False
                if int(row["attempt"] or 0) not in (0, int(attempt)):
                    db.execute("COMMIT")
                    return False

                cur = db.execute(
                    """
                    UPDATE cloud_jobs
                    SET session_name = ?,
                        owner_id = COALESCE(owner_id, ?),
                        attempt = CASE WHEN COALESCE(attempt, 0) = 0 THEN ? ELSE attempt END,
                        cleanup_status = 'pending',
                        updated_at = ?,
                        version = COALESCE(version, 1) + 1
                    WHERE job_id = ? AND version = ?
                    """,
                    (session_name, owner_id, int(attempt), now, job_id, int(row["version"] or 1)),
                )
                if cur.rowcount != 1:
                    db.execute("ROLLBACK")
                    return False

                db.execute(
                    """
                    INSERT INTO cloud_cleanup_events (
                        job_id, attempt, session_name, owner_id, status, attempts, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'pending', 0, NULL, ?, ?)
                    ON CONFLICT(job_id, attempt, session_name) DO UPDATE SET
                        owner_id = excluded.owner_id,
                        status = 'pending',
                        updated_at = excluded.updated_at
                    """,
                    (job_id, int(attempt), session_name, owner_id, now, now),
                )
                db.execute(
                    """
                    UPDATE cloud_cost_ledger
                    SET status = CASE WHEN status = 'reserved' THEN 'active' ELSE status END,
                        usage_started_at = ?,
                        updated_at = ?
                    WHERE job_id = ? AND attempt = ?
                    """,
                    (now, now, job_id, int(row["attempt"] or attempt or 1)),
                )
                db.execute("COMMIT")
                return True
            except Exception:
                db.execute("ROLLBACK")
                raise

    def update_progress(
        self,
        job_id: str,
        state: Optional[str] = None,
        percent: Optional[int] = None,
        step_name: Optional[str] = None,
        session_name: Optional[str] = None,
        cu_actual: Optional[float] = None,
        result_path: Optional[str] = None,
        error: Optional[str] = None,
        *,
        owner_id: Optional[str] = None,
        attempt: Optional[int] = None,
        expected_version: Optional[int] = None,
        error_code: Optional[str] = None,
        cleanup_status: Optional[str] = None,
        allow_from_cancel: bool = False,
        measurement_source: Optional[str] = None,
        output_manifest: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Cập nhật tiến trình/trạng thái có điều kiện theo state, version, owner_id và attempt.
        - Trạng thái cuối (completed/cancelled/failed) không bao giờ nhận cập nhật muộn.
        - cancel_requested/cancelling chặn các cập nhật tiến trình muộn trừ chuyển sang cancelling/cancelled/failed.
        - Chỉ chuyển sang cancelled khi không có session hoặc cleanup_status == 'verified'.
        - Đồng bộ sổ chi phí cloud_cost_ledger cho mọi trạng thái.
        """
        now = time.time()
        target_state: Optional[str] = None
        if state is not None:
            target_state = self._canonical_state_str(state)

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return False

                current_raw = row["state"]
                if self._is_terminal_db_state(current_raw):
                    db.execute("COMMIT")
                    return False

                current_norm = normalize_state(current_raw)
                current_version = int(row["version"] or 1)
                current_attempt = int(row["attempt"] or 0)

                if owner_id is not None and row["owner_id"] not in (None, owner_id):
                    db.execute("COMMIT")
                    return False

                if attempt is not None and current_attempt != int(attempt):
                    db.execute("COMMIT")
                    return False

                if expected_version is not None and current_version != int(expected_version):
                    db.execute("COMMIT")
                    return False

                in_cancel_flow = (
                    current_norm in (CloudJobState.CANCEL_REQUESTED, CloudJobState.CANCELLING)
                    or row["cancel_requested_at"] is not None
                )
                if in_cancel_flow:
                    if not allow_from_cancel and target_state not in (
                        CloudJobState.CANCELLING.value,
                        CloudJobState.CANCELLED.value,
                        CloudJobState.FAILED.value,
                    ):
                        db.execute("COMMIT")
                        return False

                if target_state is not None:
                    target_enum = normalize_state(target_state)
                    probe_finished = (
                        row["task_type"] == GPU_CHECK_TASK
                        and current_norm == CloudJobState.PROVISIONING
                        and target_enum == CloudJobState.COMPLETED
                        and row["cleanup_status"] == CloudCleanupStatus.VERIFIED.value
                    )
                    if target_enum != current_norm and not (
                        probe_finished or can_transition(current_norm, target_enum)
                    ):
                        db.execute("COMMIT")
                        return False

                    if target_enum == CloudJobState.CANCELLED:
                        effective_cleanup = cleanup_status or row["cleanup_status"] or "none"
                        if row["session_name"] and effective_cleanup != CloudCleanupStatus.VERIFIED.value:
                            db.execute("COMMIT")
                            return False

                updates: List[str] = [
                    "version = COALESCE(version, 1) + 1",
                    "updated_at = ?",
                ]
                params: List[Any] = [now]

                if target_state is not None:
                    updates.append("state = ?")
                    params.append(target_state)
                    if normalize_state(target_state) in TERMINAL_STATES:
                        updates.append("completed_at = ?")
                        params.append(now)

                if percent is not None:
                    updates.append("percent = ?")
                    params.append(int(percent))

                if step_name is not None:
                    updates.append("step_name = ?")
                    params.append(redact_secrets(str(step_name)))

                if session_name is not None:
                    updates.append("session_name = ?")
                    params.append(session_name)

                effective_cu_actual: Optional[float] = None
                if cu_actual is not None:
                    effective_cu_actual = max(float(row["cu_actual"] or 0.0), float(cu_actual))
                    updates.append("cu_actual = ?")
                    params.append(effective_cu_actual)

                if result_path is not None:
                    updates.append("result_path = ?")
                    params.append(str(result_path))

                if error is not None:
                    updates.append("error = ?")
                    params.append(redact_secrets(str(error)))

                if error_code is not None:
                    updates.append("error_code = ?")
                    params.append(str(error_code))

                if cleanup_status is not None:
                    updates.append("cleanup_status = ?")
                    params.append(str(cleanup_status))

                if output_manifest is not None:
                    updates.append("output_manifest_json = ?")
                    params.append(redact_secrets(json.dumps(output_manifest, ensure_ascii=False)))

                params.extend([job_id, current_version])
                sql = (
                    f"UPDATE cloud_jobs SET {', '.join(updates)} "
                    "WHERE job_id = ? AND COALESCE(version, 1) = ?"
                )
                cur = db.execute(sql, tuple(params))
                if cur.rowcount == 1:
                    updated_row = db.execute(
                        "SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)
                    ).fetchone()
                    if updated_row and int(updated_row["attempt"] or 0) > 0:
                        self._sync_ledger_on_job_row_in_db(
                            db,
                            updated_row,
                            now=now,
                            cu_observed_override=effective_cu_actual,
                            measurement_source=measurement_source,
                        )
                db.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise

    def list_jobs(self, limit: int = 50, include_hidden: bool = False) -> List[CloudJob]:
        with self._connect() as db:
            if include_hidden:
                rows = db.execute(
                    "SELECT * FROM cloud_jobs ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM cloud_jobs WHERE COALESCE(hidden_from_ui, 0) = 0 ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def has_queued_jobs(self, *, gpu_type: Optional[str] = None) -> bool:
        """Kiểm tra có job tiếp theo đang chờ trong hàng đợi (tùy chọn lọc theo loại GPU)."""
        gpu_norm = str(gpu_type).strip().upper() if gpu_type else None
        with self._connect() as db:
            rollout_job_id = self._rollout_in_db(db).get("job_id") if self.rollout_controlled else None
            if self.rollout_controlled and not rollout_job_id:
                return False
            row = db.execute(
                """
                SELECT 1 FROM cloud_jobs
                WHERE state = 'queued'
                  AND cancel_requested_at IS NULL
                  AND (? IS NULL OR UPPER(gpu_type) = ?)
                  AND (? IS NULL OR job_id = ?)
                LIMIT 1
                """,
                (gpu_norm, gpu_norm, rollout_job_id, rollout_job_id),
            ).fetchone()
            return row is not None

    @staticmethod
    def _warm_idle_interval(row: Any, now_ts: float) -> Tuple[float, float, float]:
        """(start, billed end, CU/hour) of a parked session; billing stops shortly after its expiry."""
        start = float(row["parked_at"] or now_ts)
        limit = max(start, float(row["expires_at"] or start)) + _WARM_IDLE_STOP_GRACE_SECONDS
        rate = float(GPU_CU_RATES.get(str(row["gpu_type"] or "T4").upper(), 1.96))
        return start, max(start, min(float(now_ts), limit)), rate

    def _record_warm_idle_in_db(self, db: sqlite3.Connection, rows: Sequence[Any], now_ts: float) -> None:
        """Keep the idle GPU time of sessions leaving the warm registry in the daily budget."""
        for row in rows:
            start, end, rate = self._warm_idle_interval(row, now_ts)
            if end > start:
                db.execute(
                    "INSERT INTO cloud_warm_idle_usage (session_name, gpu_type, started_at, ended_at, cu) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row["session_name"], str(row["gpu_type"] or "T4").upper(), start, end,
                     (end - start) * rate / 3600.0),
                )

    def _warm_idle_budget_in_db(
        self, db: sqlite3.Connection, now_ts: float, win_start: float, win_end: float
    ) -> Tuple[float, float]:
        """(spent, reserved) CU of warm sessions: recorded idle intervals plus sessions still parked."""
        spent = 0.0
        reserved = 0.0
        for r in db.execute(
            "SELECT started_at, ended_at, cu FROM cloud_warm_idle_usage WHERE ended_at >= ? AND started_at < ?",
            (win_start, win_end),
        ).fetchall():
            spent += _allocate_cu_to_window(float(r["cu"]), float(r["started_at"]), float(r["ended_at"]), win_start, win_end)
        for row in db.execute("SELECT * FROM cloud_warm_sessions").fetchall():
            start, end, rate = self._warm_idle_interval(row, now_ts)
            spent += _allocate_cu_to_window((end - start) * rate / 3600.0, start, end, win_start, win_end)
            reserved += max(0.0, float(row["expires_at"] or 0.0) - max(float(now_ts), start)) * rate / 3600.0
        return spent, reserved

    def park_warm_session(
        self,
        session_name: str,
        gpu_type: str,
        *,
        ttl_seconds: float,
        source_job_id: Optional[str] = None,
        keepalive_mode: bool = False,
        now: Optional[float] = None,
    ) -> List[str]:
        """Ghi nhận bền vững máy ảo giữ ấm vào SQLite (chia sẻ giữa Dashboard & Telegram Bot, chống mồ côi khi crash)."""
        ts = time.time() if now is None else float(now)
        gpu_norm = str(gpu_type or "T4").strip().upper()
        expires_at = ts + max(1.0, float(ttl_seconds))
        evicted: List[str] = []
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old_rows = db.execute(
                    "SELECT * FROM cloud_warm_sessions WHERE UPPER(gpu_type) = ? OR session_name = ?",
                    (gpu_norm, session_name),
                ).fetchall()
                self._record_warm_idle_in_db(db, old_rows, ts)
                evicted = [
                    str(r["session_name"]) for r in old_rows
                    if r["session_name"] and r["session_name"] != session_name
                    and str(r["gpu_type"] or "").upper() == gpu_norm
                ]
                if evicted:
                    db.execute(
                        "DELETE FROM cloud_warm_sessions WHERE UPPER(gpu_type) = ? AND session_name != ?",
                        (gpu_norm, session_name),
                    )
                db.execute(
                    """
                    INSERT INTO cloud_warm_sessions (
                        session_name, gpu_type, source_job_id, parked_at, expires_at, keepalive_mode, owner_pid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        gpu_type = excluded.gpu_type,
                        source_job_id = COALESCE(excluded.source_job_id, cloud_warm_sessions.source_job_id),
                        parked_at = excluded.parked_at,
                        expires_at = excluded.expires_at,
                        keepalive_mode = excluded.keepalive_mode,
                        owner_pid = excluded.owner_pid
                    """,
                    (
                        session_name,
                        gpu_norm,
                        source_job_id,
                        ts,
                        expires_at,
                        1 if keepalive_mode else 0,
                        os.getpid(),
                    ),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return evicted

    def pop_warm_session(
        self,
        gpu_type: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Lấy nguyên tử một máy ảo giữ ấm cùng loại GPU khỏi SQLite để chuyển giao cho job mới."""
        check_now = time.time() if now is None else float(now)
        gpu_norm = str(gpu_type or "T4").strip().upper()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    """
                    SELECT * FROM cloud_warm_sessions
                    WHERE UPPER(gpu_type) = ?
                    ORDER BY parked_at DESC
                    LIMIT 1
                    """,
                    (gpu_norm,),
                ).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return None
                self._record_warm_idle_in_db(db, [row], check_now)
                db.execute(
                    "DELETE FROM cloud_warm_sessions WHERE session_name = ?",
                    (row["session_name"],),
                )
                db.execute("COMMIT")
                info = dict(row)
                info["expired"] = check_now > float(row["expires_at"] or 0.0)
                return info
            except Exception:
                db.execute("ROLLBACK")
                raise

    def remove_warm_session(self, session_name: str, *, expires_at: Optional[float] = None) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                where = "WHERE session_name = ?"
                args: tuple = (session_name,)
                if expires_at is not None:
                    where += " AND expires_at = ?"
                    args = (session_name, expires_at)
                self._record_warm_idle_in_db(
                    db, db.execute(f"SELECT * FROM cloud_warm_sessions {where}", args).fetchall(), time.time()
                )
                cur = db.execute(f"DELETE FROM cloud_warm_sessions {where}", args)
                db.execute("COMMIT")
                return cur.rowcount > 0
            except Exception:
                db.execute("ROLLBACK")
                raise

    def pop_all_warm_sessions(self, gpu_type: Optional[str] = None) -> List[Dict[str, Any]]:
        gpu_norm = str(gpu_type).strip().upper() if gpu_type else None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if gpu_norm:
                    rows = db.execute(
                        "SELECT * FROM cloud_warm_sessions WHERE UPPER(gpu_type) = ?",
                        (gpu_norm,),
                    ).fetchall()
                    db.execute(
                        "DELETE FROM cloud_warm_sessions WHERE UPPER(gpu_type) = ?",
                        (gpu_norm,),
                    )
                else:
                    rows = db.execute("SELECT * FROM cloud_warm_sessions").fetchall()
                    db.execute("DELETE FROM cloud_warm_sessions")
                self._record_warm_idle_in_db(db, rows, time.time())
                db.execute("COMMIT")
                return [dict(r) for r in rows]
            except Exception:
                db.execute("ROLLBACK")
                raise

    def list_warm_sessions(
        self,
        *,
        include_expired: bool = True,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        check_now = time.time() if now is None else float(now)
        with self._connect() as db:
            if include_expired:
                rows = db.execute(
                    "SELECT * FROM cloud_warm_sessions ORDER BY parked_at DESC"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM cloud_warm_sessions WHERE expires_at >= ? ORDER BY parked_at DESC",
                    (check_now,),
                ).fetchall()
            return [dict(r) for r in rows]

    def list_reclaimable_warm_sessions(
        self,
        *,
        only_expired: bool = False,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Trả về các warm session đã hết hạn hoặc thuộc tiến trình cũ đã tắt để thu hồi chống mồ côi."""
        check_now = time.time() if now is None else float(now)
        cur_pid = os.getpid()
        with self._connect() as db:
            if only_expired:
                rows = db.execute(
                    """
                    SELECT * FROM cloud_warm_sessions
                    WHERE expires_at < ?
                       OR (owner_pid IS NOT NULL AND owner_pid != ?)
                    ORDER BY parked_at ASC
                    """,
                    (check_now, cur_pid),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM cloud_warm_sessions ORDER BY parked_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]

    def request_cancel(
        self,
        job_id: str,
        reason: str = "Người dùng đã huỷ công việc",
    ) -> Dict[str, Any]:
        """
        Ghi nhận yêu cầu huỷ bền vững vào DB.
        - Job queued chưa có session chuyển thẳng sang cancelled.
        - Job đang chạy chuyển sang cancel_requested (chưa phải cancelled).
        """
        now = time.time()
        safe_reason = redact_secrets(reason)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return {"cancelled": False, "state": None, "immediate": False, "session_name": None}

                if self._is_terminal_db_state(row["state"]):
                    db.execute("COMMIT")
                    return {
                        "cancelled": False,
                        "state": self._canonical_state_str(row["state"]),
                        "immediate": False,
                        "session_name": row["session_name"],
                    }

                current_version = int(row["version"] or 1)
                current_norm = normalize_state(row["state"])

                if current_norm == CloudJobState.QUEUED and not row["session_name"]:
                    cur = db.execute(
                        """
                        UPDATE cloud_jobs
                        SET state = 'cancelled',
                            cancel_requested_at = ?,
                            completed_at = ?,
                            cleanup_status = 'verified',
                            error_code = ?,
                            error = ?,
                            step_name = 'Đã huỷ trong hàng đợi',
                            updated_at = ?,
                            version = COALESCE(version, 1) + 1
                        WHERE job_id = ? AND COALESCE(version, 1) = ?
                        """,
                        (
                            now,
                            now,
                            CloudErrorCode.CANCELLED_BY_USER.value,
                            safe_reason,
                            now,
                            job_id,
                            current_version,
                        ),
                    )
                    db.execute("COMMIT")
                    return {
                        "cancelled": cur.rowcount == 1,
                        "state": CloudJobState.CANCELLED.value,
                        "immediate": True,
                        "session_name": None,
                    }

                next_state = (
                    CloudJobState.CANCELLING.value
                    if current_norm == CloudJobState.CANCELLING
                    else CloudJobState.CANCEL_REQUESTED.value
                )
                cur = db.execute(
                    """
                    UPDATE cloud_jobs
                    SET state = ?,
                        cancel_requested_at = COALESCE(cancel_requested_at, ?),
                        error_code = ?,
                        error = COALESCE(error, ?),
                        step_name = 'Đang yêu cầu huỷ và thu hồi session GPU...',
                        updated_at = ?,
                        version = COALESCE(version, 1) + 1
                    WHERE job_id = ? AND COALESCE(version, 1) = ?
                    """,
                    (
                        next_state,
                        now,
                        CloudErrorCode.CANCELLED_BY_USER.value,
                        safe_reason,
                        now,
                        job_id,
                        current_version,
                    ),
                )
                if cur.rowcount == 1 and int(row["attempt"] or 0) > 0:
                    ledger_status = (
                        CloudLedgerStatus.PENDING_CLEANUP.value
                        if row["session_name"]
                        else CloudLedgerStatus.ACTIVE.value
                    )
                    db.execute(
                        """
                        UPDATE cloud_cost_ledger
                        SET job_state = ?,
                            status = CASE
                                WHEN status IN ('reconciled', 'released', 'unreconciled') THEN status
                                ELSE ?
                            END,
                            updated_at = ?
                        WHERE job_id = ? AND attempt = ?
                        """,
                        (next_state, ledger_status, now, job_id, int(row["attempt"])),
                    )
                db.execute("COMMIT")
                return {
                    "cancelled": cur.rowcount == 1,
                    "state": next_state,
                    "immediate": False,
                    "session_name": row["session_name"],
                }
            except Exception:
                db.execute("ROLLBACK")
                raise

    def record_cleanup_result(
        self,
        job_id: str,
        attempt: int,
        session_name: str,
        *,
        verified_stopped: bool,
        cleanup_status: str,
        attempts: int,
        error: Optional[str] = None,
    ) -> None:
        """Ghi nhận kết quả kiểm tra dừng session vào cả cloud_jobs, cleanup_events và sổ chi phí."""
        now = time.time()
        safe_err = redact_secrets(error) if error else None
        effective_status = CloudCleanupStatus.VERIFIED.value if verified_stopped else cleanup_status

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """
                    UPDATE cloud_jobs
                    SET cleanup_status = ?,
                        cleanup_attempts = COALESCE(cleanup_attempts, 0) + ?,
                        cleanup_last_error = ?,
                        updated_at = ?
                    WHERE job_id = ?
                    """,
                    (effective_status, int(attempts), safe_err, now, job_id),
                )
                db.execute(
                    """
                    INSERT INTO cloud_cleanup_events (
                        job_id, attempt, session_name, owner_id, status, attempts, last_error, created_at, updated_at
                    )
                    SELECT job_id, ?, ?, owner_id, ?, ?, ?, ?, ?
                    FROM cloud_jobs WHERE job_id = ?
                    ON CONFLICT(job_id, attempt, session_name) DO UPDATE SET
                        status = excluded.status,
                        attempts = cloud_cleanup_events.attempts + excluded.attempts,
                        last_error = excluded.last_error,
                        updated_at = excluded.updated_at
                    """,
                    (
                        int(attempt),
                        session_name,
                        effective_status,
                        int(attempts),
                        safe_err,
                        now,
                        now,
                        job_id,
                    ),
                )
                job_row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if job_row is not None:
                    self._sync_ledger_cleanup_in_db(
                        db,
                        job_row=job_row,
                        attempt=int(attempt),
                        verified_stopped=bool(verified_stopped),
                        safe_err=safe_err,
                        now=now,
                    )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def list_pending_cleanup_events(self) -> List[Dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM cloud_cleanup_events
                WHERE status != 'verified'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _stop_and_verify_with_adapter(adapter: Any, session_name: str) -> Dict[str, Any]:
        if hasattr(adapter, "verify_session_stopped"):
            return adapter.verify_session_stopped(session_name)

        # Fallback cho test adapter tối giản chỉ có stop_session
        ok, msg = adapter.stop_session(session_name)
        return {
            "verified_stopped": bool(ok),
            "cleanup_status": (
                CloudCleanupStatus.VERIFIED.value
                if ok
                else CloudCleanupStatus.FAILED_RETRY_NEEDED.value
            ),
            "attempts": 1,
            "error": None if ok else (msg or "stop_session thất bại"),
        }

    def cleanup_job_session(self, job_id: str, adapter: Any = None) -> Dict[str, Any]:
        """Dừng và xác nhận thu hồi riêng session thuộc job này."""
        job = self.get_job(job_id)
        if not job or not job.session_name:
            return {
                "verified_stopped": True,
                "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                "attempts": 0,
                "error": None,
            }

        if adapter is None:
            from .colab_bridge import ColabProviderAdapter
            adapter = ColabProviderAdapter()

        res = self._stop_and_verify_with_adapter(adapter, job.session_name)
        self.record_cleanup_result(
            job_id,
            max(1, int(job.attempt or 1)),
            job.session_name,
            verified_stopped=bool(res.get("verified_stopped")),
            cleanup_status=str(res.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)),
            attempts=int(res.get("attempts", 1)),
            error=res.get("error"),
        )
        return res

    def cancel_job(self, job_id: str, adapter: Any = None) -> bool:
        """
        Yêu cầu huỷ job; nếu truyền adapter và job có session chưa được worker khác
        thu hồi thì tiến hành cancelling -> verify_session_stopped -> cancelled.
        """
        res = self.request_cancel(job_id)
        if not res["cancelled"]:
            return False
        if res.get("immediate"):
            return True

        session_name = res.get("session_name")
        if adapter is not None:
            self.update_progress(
                job_id,
                state=CloudJobState.CANCELLING.value,
                step_name="Đang dừng và xác minh thu hồi session Colab...",
                allow_from_cancel=True,
            )
            if not session_name:
                return self.update_progress(
                    job_id,
                    state=CloudJobState.CANCELLED.value,
                    step_name="Đã huỷ công việc (không có session đang chạy)",
                    cleanup_status=CloudCleanupStatus.VERIFIED.value,
                    error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                    error="Người dùng đã huỷ công việc",
                    allow_from_cancel=True,
                )

            cleanup = self.cleanup_job_session(job_id, adapter=adapter)
            if cleanup.get("verified_stopped"):
                return self.update_progress(
                    job_id,
                    state=CloudJobState.CANCELLED.value,
                    step_name="Đã huỷ và xác nhận thu hồi session GPU",
                    cleanup_status=CloudCleanupStatus.VERIFIED.value,
                    error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                    error="Người dùng đã huỷ công việc",
                    allow_from_cancel=True,
                )
            self.update_progress(
                job_id,
                state=CloudJobState.CANCELLING.value,
                step_name="Chưa xác nhận dừng được session GPU; đã ghi nhận sự kiện chờ thu hồi",
                cleanup_status=str(cleanup.get("cleanup_status")),
                error=cleanup.get("error") or "Không xác nhận được session đã dừng",
                allow_from_cancel=True,
            )
            return False
        return True

    def is_cancel_or_lease_lost(
        self,
        job_id: str,
        owner_id: Optional[str] = None,
        attempt: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """Kiểm tra job đã bị huỷ, đổi owner/attempt, kết thúc hoặc hết hạn lease chưa."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return True, "missing"

            raw_state = row["state"]
            if row["cancel_requested_at"] is not None or raw_state in _CANCEL_DB_STATES:
                return True, "cancel_requested"

            if self._is_terminal_db_state(raw_state):
                return True, "terminal"

            if owner_id is not None and row["owner_id"] not in (None, owner_id):
                return True, "owner_changed"

            if attempt is not None and int(row["attempt"] or 0) != int(attempt):
                return True, "attempt_superseded"

            check_now = time.time() if now is None else float(now)
            if row["lease_expires_at"] is not None and float(row["lease_expires_at"]) < check_now:
                return True, "lease_expired"

            return False, "ok"

    def release_expired_lease_for_retry(
        self,
        job_id: str,
        adapter: Any = None,
        *,
        reason: str = "Lease hết hạn, trả về hàng đợi cho attempt mới",
        now: Optional[float] = None,
    ) -> bool:
        """
        Thu hồi session của lease hết hạn rồi trả job không ở trạng thái cuối về queued
        để lần claim_next() tiếp theo tăng attempt lên N+1.
        """
        check_now = time.time() if now is None else float(now)
        job = self.get_job(job_id)
        if not job or self._is_terminal_db_state(job.state):
            return False
        if job.state in ("cancel_requested", "cancelling") or job.cancel_requested_at is not None:
            return False
        if job.lease_expires_at is None or float(job.lease_expires_at) >= check_now:
            return False

        if job.session_name:
            cleanup = self.cleanup_job_session(job_id, adapter=adapter)
            if not cleanup.get("verified_stopped"):
                return False

        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cur = db.execute(
                    """
                    UPDATE cloud_jobs
                    SET state = 'queued',
                        owner_id = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        session_name = NULL,
                        cu_actual = 0.0,
                        cleanup_status = 'none',
                        step_name = ?,
                        error = NULL,
                        updated_at = ?,
                        version = COALESCE(version, 1) + 1
                    WHERE job_id = ?
                      AND COALESCE(version, 1) = ?
                      AND cancel_requested_at IS NULL
                      AND state NOT IN ('queued', 'completed', 'cancelled', 'failed', 'error', 'stopped')
                    """,
                    (redact_secrets(reason), check_now, job_id, int(job.version or 1)),
                )
                if cur.rowcount == 1 and int(job.attempt or 0) > 0:
                    db.execute(
                        """
                        UPDATE cloud_cost_ledger
                        SET status = CASE WHEN COALESCE(cu_observed, 0.0) > 0.0 THEN 'reconciled' ELSE 'released' END,
                            cu_reconciled = COALESCE(cu_observed, 0.0),
                            cleanup_verified = 1,
                            job_state = 'requeued',
                            usage_ended_at = COALESCE(usage_ended_at, ?),
                            updated_at = ?
                        WHERE job_id = ? AND attempt = ?
                        """,
                        (check_now, check_now, job_id, int(job.attempt)),
                    )
                db.execute("COMMIT")
                return cur.rowcount == 1
            except Exception:
                db.execute("ROLLBACK")
                raise

    def delete_job(self, job_id: str) -> bool:
        """
        Ẩn job đã kết thúc và sạch session khỏi danh sách UI (soft-hide).
        Không bao giờ xoá bản ghi sổ chi phí cloud_cost_ledger hay cloud_cleanup_events.
        """
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not row:
                    db.execute("COMMIT")
                    return False
                if not self._is_terminal_db_state(row["state"]):
                    db.execute("COMMIT")
                    return False
                if row["session_name"] and (row["cleanup_status"] or "none") != CloudCleanupStatus.VERIFIED.value:
                    db.execute("COMMIT")
                    return False
                if db.execute("""SELECT 1 FROM cloud_cost_ledger WHERE job_id=?
                    AND cleanup_verified=0 AND status IN ('unreconciled', 'pending_cleanup')""", (job_id,)).fetchone():
                    db.execute("COMMIT")
                    return False
                cur = db.execute(
                    "UPDATE cloud_jobs SET hidden_from_ui = 1, updated_at = ? WHERE job_id = ?",
                    (now, job_id),
                )
                db.execute("COMMIT")
                return cur.rowcount > 0
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM cloud_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def list_gpu_evidence(self) -> List[Dict[str, Any]]:
        """Latest actual test or completed production render for each accelerator."""
        placeholders = ", ".join("?" for _ in CHECKABLE_GPUS)
        with self._connect() as db:
            rows = db.execute(
                f"""
                WITH ranked AS (
                    SELECT gpu_type, task_type, state, error_code, cleanup_status,
                           completed_at, cu_actual, job_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY gpu_type
                               ORDER BY completed_at IS NOT NULL, updated_at DESC, id DESC
                           ) AS rank
                    FROM cloud_jobs
                    WHERE gpu_type IN ({placeholders}) AND
                        (task_type = ? OR (task_type = 'full_pipeline' AND state = 'completed'))
                )
                SELECT gpu_type, task_type, state, error_code, cleanup_status,
                       completed_at, cu_actual, job_id
                FROM ranked WHERE rank = 1
                """, (*CHECKABLE_GPUS, GPU_CHECK_TASK),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_setting(self, key: str, value: Any) -> None:
        now = time.time()
        val_str = str(value)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, val_str, now),
                )
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

    def get_cu_budget_daily(self) -> float:
        val = self.get_setting("cu_budget_daily")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return float(DEFAULT_CONFIG.get("cu_budget_daily", 10.0))

    def set_cu_budget_daily(self, budget: float) -> None:
        self.set_setting("cu_budget_daily", round(float(budget), 2))

    def _upsert_claimed_ledger_in_db(
        self,
        db: sqlite3.Connection,
        *,
        job_id: str,
        attempt: int,
        gpu_type: str,
        task_type: str,
        cu_estimated: float,
        cu_buffer: float,
        cu_reserved: float,
        now: float,
        provider_balance_before: Optional[float] = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO cloud_cost_ledger (
                job_id, attempt, gpu_type, task_type,
                cu_estimated, cu_buffer, cu_reserved, cu_observed, cu_reconciled,
                measurement_source, status, job_state, cleanup_verified,
                usage_started_at, usage_ended_at, provider_balance_before,
                provider_balance_after, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, NULL, ?, ?, ?, 0, ?, NULL, ?, NULL, NULL, ?, ?)
            ON CONFLICT(job_id, attempt) DO UPDATE SET
                cu_estimated = excluded.cu_estimated,
                cu_buffer = excluded.cu_buffer,
                cu_reserved = excluded.cu_reserved,
                status = excluded.status,
                job_state = excluded.job_state,
                cleanup_verified = 0,
                usage_started_at = excluded.usage_started_at,
                provider_balance_before = COALESCE(excluded.provider_balance_before, cloud_cost_ledger.provider_balance_before),
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                int(attempt),
                str(gpu_type or "T4").upper(),
                str(task_type or "full_pipeline"),
                float(cu_estimated),
                float(cu_buffer),
                float(cu_reserved),
                CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value,
                CloudLedgerStatus.RESERVED.value,
                CloudJobState.PROVISIONING.value,
                now,
                provider_balance_before,
                now,
                now,
            ),
        )

    def _sync_ledger_on_job_row_in_db(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: float,
        cu_observed_override: Optional[float] = None,
        measurement_source: Optional[str] = None,
    ) -> None:
        job_id = str(row["job_id"])
        attempt = int(row["attempt"] or 0)
        if attempt <= 0:
            return

        ledger_row = db.execute(
            "SELECT * FROM cloud_cost_ledger WHERE job_id = ? AND attempt = ?",
            (job_id, attempt),
        ).fetchone()
        if ledger_row is None:
            breakdown = self.calculate_reservation_breakdown(
                gpu_type=row["gpu_type"],
                cu_estimated=float(row["cu_estimated"] or 0.0),
                cu_buffer=float(row["cu_buffer"]) if row["cu_reserved"] and float(row["cu_reserved"]) > 0.0 else None,
            )
            self._upsert_claimed_ledger_in_db(
                db,
                job_id=job_id,
                attempt=attempt,
                gpu_type=row["gpu_type"],
                task_type=row["task_type"],
                cu_estimated=float(row["cu_estimated"] or 0.0),
                cu_buffer=float(row["cu_buffer"] or breakdown["cu_buffer"]),
                cu_reserved=float(row["cu_reserved"] or breakdown["cu_reserved"]),
                now=float(row["started_at"] or row["created_at"] or now),
            )
            ledger_row = db.execute(
                "SELECT * FROM cloud_cost_ledger WHERE job_id = ? AND attempt = ?",
                (job_id, attempt),
            ).fetchone()

        job_state_norm = self._canonical_state_str(row["state"])
        is_terminal = self._is_terminal_db_state(job_state_norm)
        has_session = bool(row["session_name"])
        effective_cleanup = str(row["cleanup_status"] or "none")
        is_cleaned = (
            effective_cleanup == CloudCleanupStatus.VERIFIED.value
            or (not has_session and effective_cleanup in ("none", CloudCleanupStatus.VERIFIED.value))
        )
        if (float(ledger_row["legacy_hourly_rate"] or 0.) > 0
                and ledger_row["usage_ended_at"] is None
                and effective_cleanup != CloudCleanupStatus.VERIFIED.value):
            is_cleaned = False

        prev_obs = float(ledger_row["cu_observed"] or 0.0) if ledger_row else 0.0
        obs = max(prev_obs, float(row["cu_actual"] or 0.0))
        if cu_observed_override is not None:
            obs = max(obs, float(cu_observed_override))

        prev_src = str(ledger_row["measurement_source"]) if ledger_row else CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value
        next_src = measurement_source or (
            CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value
            if obs > 0.0 and prev_src == CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value
            else prev_src
        )

        prev_status = str(ledger_row["status"]) if ledger_row else CloudLedgerStatus.RESERVED.value
        next_status = prev_status
        cu_reconciled: Optional[float] = ledger_row["cu_reconciled"] if ledger_row else None
        cleanup_verified = 1 if is_cleaned else int(ledger_row["cleanup_verified"] or 0 if ledger_row else 0)
        usage_ended_at: Optional[float] = ledger_row["usage_ended_at"] if ledger_row else None

        if is_terminal:
            cleanup_verified = 1 if is_cleaned else 0
            if usage_ended_at is None and not (float(ledger_row["legacy_hourly_rate"] or 0) > 0 and not is_cleaned):
                usage_ended_at = float(row["completed_at"] or now)
            if is_cleaned:
                if job_state_norm == CloudJobState.CANCELLED.value and obs <= 0.0 and not has_session:
                    next_status = CloudLedgerStatus.RELEASED.value
                    cu_reconciled = 0.0
                else:
                    next_status = CloudLedgerStatus.RECONCILED.value
                    cu_reconciled = round(obs, 6)
            elif effective_cleanup in (
                CloudCleanupStatus.FAILED_RETRY_NEEDED.value,
                CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
            ):
                next_status = CloudLedgerStatus.UNRECONCILED.value
                cu_reconciled = None
            else:
                next_status = CloudLedgerStatus.PENDING_CLEANUP.value
                cu_reconciled = None
        elif job_state_norm in (CloudJobState.CANCEL_REQUESTED.value, CloudJobState.CANCELLING.value):
            if effective_cleanup in (
                CloudCleanupStatus.FAILED_RETRY_NEEDED.value,
                CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
            ):
                next_status = CloudLedgerStatus.UNRECONCILED.value
                cleanup_verified = 0
            elif has_session and not is_cleaned:
                next_status = CloudLedgerStatus.PENDING_CLEANUP.value
                cleanup_verified = 0
        elif job_state_norm in (
            CloudJobState.UPLOADING.value,
            CloudJobState.PREPARING.value,
            CloudJobState.PROCESSING.value,
            CloudJobState.DOWNLOADING.value,
            CloudJobState.VALIDATING.value,
        ):
            if prev_status == CloudLedgerStatus.RESERVED.value:
                next_status = CloudLedgerStatus.ACTIVE.value

        db.execute(
            """
            UPDATE cloud_cost_ledger
            SET cu_observed = ?,
                cu_reconciled = ?,
                measurement_source = ?,
                status = ?,
                job_state = ?,
                cleanup_verified = ?,
                usage_ended_at = ?,
                updated_at = ?
            WHERE job_id = ? AND attempt = ?
            """,
            (
                round(obs, 6),
                cu_reconciled,
                next_src,
                next_status,
                job_state_norm,
                cleanup_verified,
                usage_ended_at,
                now,
                job_id,
                attempt,
            ),
        )

    def _sync_ledger_cleanup_in_db(
        self,
        db: sqlite3.Connection,
        *,
        job_row: sqlite3.Row,
        attempt: int,
        verified_stopped: bool,
        safe_err: Optional[str],
        now: float,
    ) -> None:
        job_id = str(job_row["job_id"])
        ledger_row = db.execute(
            "SELECT * FROM cloud_cost_ledger WHERE job_id = ? AND attempt = ?",
            (job_id, int(attempt)),
        ).fetchone()
        if ledger_row is None:
            return

        job_state_norm = self._canonical_state_str(job_row["state"])
        obs = max(float(ledger_row["cu_observed"] or 0.0), float(job_row["cu_actual"] or 0.0))
        legacy_rate = float(ledger_row["legacy_hourly_rate"] or 0.)
        if legacy_rate > 0 and ledger_row["usage_ended_at"] is None:
            obs = max(obs, max(0., now - float(ledger_row["usage_started_at"])) * legacy_rate / 3600)
            db.execute("UPDATE cloud_cost_ledger SET cu_observed=? WHERE job_id=? AND attempt=?",
                       (obs, job_id, int(attempt)))
            db.execute("UPDATE cloud_jobs SET cu_actual=MAX(COALESCE(cu_actual,0),?) WHERE job_id=?", (obs, job_id))
        if verified_stopped:
            if self._is_terminal_db_state(job_state_norm):
                if job_state_norm == CloudJobState.CANCELLED.value and obs <= 0.0 and not job_row["session_name"]:
                    next_status = CloudLedgerStatus.RELEASED.value
                    reconciled_val = 0.0
                else:
                    next_status = CloudLedgerStatus.RECONCILED.value
                    reconciled_val = round(obs, 6)
            else:
                next_status = str(ledger_row["status"])
                reconciled_val = ledger_row["cu_reconciled"]
            db.execute(
                """
                UPDATE cloud_cost_ledger
                SET cleanup_verified = 1,
                    status = ?,
                    cu_reconciled = ?,
                    usage_ended_at = COALESCE(usage_ended_at, ?),
                    notes = COALESCE(notes, ?),
                    updated_at = ?
                WHERE job_id = ? AND attempt = ?
                """,
                (next_status, reconciled_val, now, safe_err, now, job_id, int(attempt)),
            )
        else:
            db.execute(
                """
                UPDATE cloud_cost_ledger
                SET cleanup_verified = 0,
                    status = ?,
                    cu_reconciled = NULL,
                    notes = COALESCE(?, notes),
                    updated_at = ?
                WHERE job_id = ? AND attempt = ?
                """,
                (CloudLedgerStatus.UNRECONCILED.value, safe_err, now, job_id, int(attempt)),
            )

    def _save_provider_snapshot_in_db(
        self,
        db: sqlite3.Connection,
        snap: Dict[str, Any],
        now: float,
    ) -> None:
        current = self._get_provider_snapshot_in_db(db, now_ts=now)
        if current and current.get("observed_at") is not None:
            previous_time = float(current["observed_at"])
            incoming_time = float(snap.get("observed_at") or 0.0)
            if incoming_time < previous_time:
                return
            if incoming_time == previous_time:
                # Equally dated observations cannot increase spending authority.
                old_balance, new_balance = current.get("balance"), snap.get("balance")
                if (not current.get("ok") and snap.get("ok")) or (
                    current.get("is_stale") and not snap.get("is_stale")
                ) or (
                    old_balance is not None and new_balance is not None
                    and float(new_balance) > float(old_balance)
                ):
                    return
        payload = json.dumps(snap, ensure_ascii=False)
        db.execute(
            "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("provider_usage_snapshot", payload, now),
        )

    def _admission_provider_snapshot_in_db(
        self,
        db: sqlite3.Connection,
        usage: Optional[Dict[str, Any]],
        *,
        now: float,
        record_snapshot: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Resolve claims against the newest observation, never re-date a cached payload."""
        current = self._get_provider_snapshot_in_db(db, now_ts=now)
        if usage is None:
            return current
        incoming = _normalize_provider_usage_payload(usage, now=now)
        has_observed_time = any(usage.get(key) not in (None, "") for key in ("observed_at", "as_of", "timestamp"))
        if current and not has_observed_time:
            # A raw dict may have waited in another lane. It can reduce authority,
            # but cannot raise balance, recover connectivity or refresh cache age.
            old_balance, new_balance = current.get("balance"), incoming.get("balance")
            lowers_balance = (old_balance is not None and new_balance is not None
                              and float(new_balance) < float(old_balance))
            worsens_health = (not incoming.get("ok")) or incoming.get("is_stale")
            if not lowers_balance and not worsens_health:
                return current
            incoming["observed_at"] = current.get("observed_at") or 0.0
            incoming["stale_flag"] = bool(current.get("stale_flag") or incoming.get("stale_flag"))
            incoming["ok"] = bool(current.get("ok") and incoming.get("ok"))
            incoming["age_seconds"] = current.get("age_seconds")
            incoming["is_stale"] = bool(current.get("is_stale") or incoming.get("is_stale"))
            incoming["status"] = ("disconnected" if not incoming["ok"] else
                                  "stale" if incoming["is_stale"] else "ok")
        if current and current.get("observed_at") is not None:
            incoming_time, previous_time = float(incoming.get("observed_at") or 0.0), float(current["observed_at"])
            if incoming_time < previous_time:
                return current
            if incoming_time == previous_time:
                old_balance, new_balance = current.get("balance"), incoming.get("balance")
                if (not current.get("ok") and incoming.get("ok")) or (
                    current.get("is_stale") and not incoming.get("is_stale")
                ) or (
                    old_balance is not None and new_balance is not None
                    and float(new_balance) > float(old_balance)
                ):
                    return current
        if record_snapshot:
            self._save_provider_snapshot_in_db(db, incoming, now)
            return self._get_provider_snapshot_in_db(db, now_ts=now)
        return incoming

    def _get_provider_snapshot_in_db(
        self,
        db: sqlite3.Connection,
        *,
        now_ts: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        row = db.execute(
            "SELECT value FROM cloud_settings WHERE key = ?", ("provider_usage_snapshot",)
        ).fetchone()
        if not row:
            return None
        try:
            raw = json.loads(row["value"])
            if not isinstance(raw, dict):
                raise ValueError("Invalid snapshot object")
        except Exception:
            return {
                "status": "disconnected",
                "ok": False,
                "balance": None,
                "observed_at": None,
                "age_seconds": None,
                "is_stale": True,
                "error": "Dữ liệu snapshot số dư Colab không hợp lệ",
            }
        check_now = time.time() if now_ts is None else float(now_ts)
        stale_limit = float(DEFAULT_CONFIG.get("provider_balance_stale_seconds", 300))
        ok = bool(raw.get("ok"))
        try:
            observed_at = float(raw["observed_at"])
            balance = float(raw["balance"]) if raw.get("balance") is not None else None
            if not math.isfinite(observed_at) or observed_at > check_now + 5.0 or (
                ok and (balance is None or not math.isfinite(balance) or balance < 0.0)
            ):
                raise ValueError("Invalid persisted snapshot")
        except (KeyError, TypeError, ValueError, OverflowError):
            return {
                "status": "disconnected", "ok": False, "balance": None,
                "observed_at": None, "age_seconds": None, "is_stale": True,
                "error": "Snapshot số dư Colab đã lưu không hợp lệ; cần lấy mẫu mới.",
            }
        age_seconds = max(0.0, check_now - observed_at)
        is_stale = bool(raw.get("stale_flag")) or (age_seconds > stale_limit)
        status = "disconnected" if not ok else ("stale" if is_stale else "ok")
        return {
            **raw,
            "observed_at": observed_at,
            "balance": balance,
            "age_seconds": round(age_seconds, 2),
            "is_stale": is_stale,
            "status": status,
        }

    def record_provider_usage_snapshot(
        self,
        usage: Dict[str, Any],
        *,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Ghi nhận snapshot số dư/trạng thái từ provider để phục vụ kiểm soát admission."""
        now_ts = time.time() if now is None else float(now)
        snap = _normalize_provider_usage_payload(usage, now=now_ts)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                self._save_provider_snapshot_in_db(db, snap, now_ts)
                recorded = self._get_provider_snapshot_in_db(db, now_ts=now_ts)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return recorded or snap

    def get_provider_usage_snapshot(self, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        with self._connect() as db:
            return self._get_provider_snapshot_in_db(db, now_ts=now)

    def _compute_budget_summary_in_db(
        self,
        db: sqlite3.Connection,
        *,
        now_ts: Optional[float] = None,
    ) -> Dict[str, Any]:
        check_now = time.time() if now_ts is None else float(now_ts)
        val_row = db.execute(
            "SELECT value FROM cloud_settings WHERE key = ?", ("cu_budget_daily",)
        ).fetchone()
        budget_daily = float(DEFAULT_CONFIG.get("cu_budget_daily", 10.0))
        if val_row is not None:
            try:
                budget_daily = float(val_row["value"])
            except ValueError:
                pass

        tz_name = str(DEFAULT_CONFIG.get("budget_timezone") or "Asia/Ho_Chi_Minh")
        win_start, win_end, window_date = _day_bounds_epoch(check_now, tz_name)

        rows = db.execute("SELECT * FROM cloud_cost_ledger").fetchall()
        spent_raw = 0.0
        reserved_raw = 0.0
        unreconciled_count = 0
        pending_cleanup_count = 0
        sources_used: set[str] = set()

        for r in rows:
            status = str(r["status"] or "")
            observed = max(0.0, float(r["cu_observed"] or 0.0))
            reconciled = r["cu_reconciled"]
            reserved = max(0.0, float(r["cu_reserved"] or 0.0))
            cleanup_verified = int(r["cleanup_verified"] or 0)
            start_ts = float(r["usage_started_at"] or r["created_at"])
            end_ts = float(r["usage_ended_at"]) if r["usage_ended_at"] is not None else check_now
            if r["usage_ended_at"] is None and float(r["legacy_hourly_rate"] or 0.) > 0:
                observed = max(observed, max(0., check_now - start_ts) * float(r["legacy_hourly_rate"]) / 3600)

            effective_usage = (
                max(0.0, float(reconciled))
                if (status == CloudLedgerStatus.RECONCILED.value and reconciled is not None)
                else observed
            )
            allocated_spent = _allocate_cu_to_window(
                effective_usage, start_ts, end_ts, win_start, win_end
            )
            if allocated_spent > 0.0:
                spent_raw += allocated_spent
                sources_used.add(
                    str(r["measurement_source"] or CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value)
                )

            if status in _HOLD_LEDGER_STATUSES and cleanup_verified == 0:
                if status == CloudLedgerStatus.UNRECONCILED.value:
                    unreconciled_count += 1
                elif status == CloudLedgerStatus.PENDING_CLEANUP.value:
                    pending_cleanup_count += 1

                rem_hold = max(0.0, reserved - observed)
                if rem_hold > 0.0 and start_ts < win_end:
                    end_for_hold = float(r["usage_ended_at"]) if r["usage_ended_at"] is not None else check_now
                    if end_for_hold >= win_start or (win_start <= check_now < win_end):
                        reserved_raw += rem_hold

        # Bảo toàn các bản ghi legacy (nếu DB cũ có completed job trước khi tạo cloud_cost_ledger)
        legacy_rows = db.execute(
            """
            SELECT j.* FROM cloud_jobs j
            WHERE COALESCE(j.cu_actual, 0.0) > 0.0
              AND NOT EXISTS (SELECT 1 FROM cloud_cost_ledger l WHERE l.job_id = j.job_id)
            """
        ).fetchall()
        for jr in legacy_rows:
            s_ts = float(jr["started_at"] or jr["created_at"])
            e_ts = float(jr["completed_at"] or jr["updated_at"] or s_ts)
            allocated = _allocate_cu_to_window(float(jr["cu_actual"] or 0.0), s_ts, e_ts, win_start, win_end)
            if allocated > 0.0:
                spent_raw += allocated
                sources_used.add(CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value)

        warm_spent, warm_reserved = self._warm_idle_budget_in_db(db, check_now, win_start, win_end)
        spent_raw += warm_spent
        reserved_raw += warm_reserved
        if warm_spent > 0.0:
            sources_used.add(CloudCostMeasurementSource.UNVERIFIED_ESTIMATE.value)

        spent_today = round(spent_raw, 4)
        reserved_today = round(reserved_raw, 4)
        committed_today = round(spent_raw + reserved_raw, 4)
        remaining_today = max(0.0, round(budget_daily - committed_today, 4))
        percent_used = (
            min(100.0, round((committed_today / budget_daily) * 100.0, 1))
            if budget_daily > 0.0
            else (100.0 if committed_today > 0.0 else 0.0)
        )
        is_exceeded = committed_today >= budget_daily if budget_daily > 0.0 else True

        snap = self._get_provider_snapshot_in_db(db, now_ts=check_now)
        provider_uncertain = bool(snap and snap.get("status") != "ok")
        provider_info = snap or {
            "status": "not_checked",
            "ok": None,
            "balance": None,
            "observed_at": None,
            "age_seconds": None,
            "is_stale": False,
            "error": None,
        }

        return {
            "budget_daily": round(budget_daily, 4),
            "budget_timezone": tz_name,
            "window_date": window_date,
            "spent_today": round(spent_today, 3),
            "reserved_today": round(reserved_today, 3),
            "committed_today": round(committed_today, 3),
            "remaining_today": round(remaining_today, 3),
            "percent_used": percent_used,
            "is_exceeded": is_exceeded,
            "unreconciled_count": unreconciled_count,
            "pending_cleanup_count": pending_cleanup_count,
            "has_uncertain_budget": unreconciled_count > 0 or provider_uncertain,
            "measurement_sources": sorted(sources_used),
            "cost_is_estimated": (
                CloudCostMeasurementSource.PROVIDER_BALANCE_DELTA.value not in sources_used
            ),
            "provider": provider_info,
        }

    def _evaluate_admission_in_db(
        self,
        db: sqlite3.Connection,
        *,
        cu_needed: float,
        provider_usage: Optional[Dict[str, Any]] = None,
        require_fresh_provider: bool = False,
        now_ts: Optional[float] = None,
        record_snapshot: bool = True,
        check_daily_budget: bool = True,
    ) -> Dict[str, Any]:
        check_now = time.time() if now_ts is None else float(now_ts)
        snap = self._admission_provider_snapshot_in_db(
            db, provider_usage, now=check_now, record_snapshot=record_snapshot,
        )
        summary = self._compute_budget_summary_in_db(db, now_ts=check_now)
        if snap is not None:
            summary["provider"] = snap

        if summary["unreconciled_count"] > 0:
            return {
                "allowed": False,
                "code": CloudErrorCode.BUDGET_UNCERTAIN.value,
                "http_status": 503,
                "reason": (
                    f"Còn {summary['unreconciled_count']} phiên GPU chưa xác nhận thu hồi/đối soát chi phí; "
                    "tạm chặn nhận job mới."
                ),
                "budget": summary,
            }

        if snap is None:
            if require_fresh_provider:
                return {
                    "allowed": False,
                    "code": CloudErrorCode.BUDGET_UNCERTAIN.value,
                    "http_status": 503,
                    "reason": "Chưa có dữ liệu số dư Colab mới; không thể nhận job mới.",
                    "budget": summary,
                }
        else:
            if not snap.get("ok"):
                return {
                    "allowed": False,
                    "code": CloudErrorCode.BUDGET_UNCERTAIN.value,
                    "http_status": 503,
                    "reason": f"Không thể kết nối Colab để kiểm tra số dư ({snap.get('error') or 'disconnected'}).",
                    "budget": summary,
                }
            if snap.get("is_stale"):
                return {
                    "allowed": False,
                    "code": CloudErrorCode.BUDGET_UNCERTAIN.value,
                    "http_status": 503,
                    "reason": (
                        f"Số dư Colab đã cũ ({snap.get('age_seconds')}s); cần làm mới trước khi nhận job."
                    ),
                    "budget": summary,
                }
            bal = snap.get("balance")
            min_bal = float(DEFAULT_CONFIG.get("min_provider_balance_cu", 0.0))
            # Provider balance is account-wide: concurrent jobs must not each
            # spend the same available CU before the next usage refresh.
            required_bal = max(min_bal, float(summary["reserved_today"]) + float(cu_needed))
            if bal is not None and float(bal) < required_bal - 1e-6:
                return {
                    "allowed": False,
                    "code": CloudErrorCode.INSUFFICIENT_CU.value,
                    "http_status": 429,
                    "reason": f"Số dư tài khoản Colab không đủ ({float(bal):.2f} < {required_bal:.2f} CU).",
                    "budget": summary,
                }

        if check_daily_budget and cu_needed > 0.0:
            if summary["committed_today"] + float(cu_needed) > summary["budget_daily"] + 1e-6:
                return {
                    "allowed": False,
                    "code": CloudErrorCode.BUDGET_EXCEEDED.value,
                    "http_status": 429,
                    "reason": (
                        f"Vượt hạn mức chi phí ngày ({summary['committed_today']:.2f} + {cu_needed:.2f} "
                        f"> {summary['budget_daily']:.2f} CU)."
                    ),
                    "budget": summary,
                }
        elif check_daily_budget and summary["is_exceeded"]:
            return {
                "allowed": False,
                "code": CloudErrorCode.BUDGET_EXCEEDED.value,
                "http_status": 429,
                "reason": (
                    f"Đã đạt hạn mức chi phí ngày ({summary['committed_today']:.2f}/{summary['budget_daily']:.2f} CU)."
                ),
                "budget": summary,
            }

        return {
            "allowed": True,
            "code": None,
            "http_status": 200,
            "reason": "ok",
            "budget": summary,
        }

    def check_reserved_provider_balance(self) -> Dict[str, Any]:
        """Recheck account-wide holds after claim, without reserving this job twice."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if self._has_pending_cleanup_in_db(db):
                    decision = {
                        "allowed": False,
                        "code": CloudErrorCode.BUDGET_UNCERTAIN.value,
                        "http_status": 503,
                        "reason": "Còn tài nguyên Cloud chưa xác nhận thu hồi; tạm chặn mở session mới.",
                    }
                else:
                    decision = self._evaluate_admission_in_db(
                        db, cu_needed=0.0, require_fresh_provider=True,
                        check_daily_budget=False,
                    )
                db.commit()
                return decision
            except Exception:
                db.rollback()
                raise

    def check_admission_allowed(
        self,
        *,
        cu_estimated: float = 0.0,
        cu_buffer: Optional[float] = None,
        gpu_type: str = "T4",
        enforce_budget: bool = True,
        provider_usage: Optional[Dict[str, Any]] = None,
        require_fresh_provider: bool = False,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Kiểm tra ngân sách, reservation hiện hữu và độ mới số dư provider trước admission."""
        breakdown = self.calculate_reservation_breakdown(
            gpu_type=gpu_type,
            cu_estimated=cu_estimated,
            cu_buffer=cu_buffer,
        )
        now_ts = time.time() if now is None else float(now)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if not enforce_budget:
                    summary = self._compute_budget_summary_in_db(db, now_ts=now_ts)
                    db.execute("COMMIT")
                    return {
                        "allowed": True,
                        "code": None,
                        "http_status": 200,
                        "reason": "budget_check_disabled",
                        "budget": summary,
                        "breakdown": breakdown,
                    }
                decision = self._evaluate_admission_in_db(
                    db,
                    cu_needed=breakdown["cu_reserved"],
                    provider_usage=provider_usage,
                    require_fresh_provider=require_fresh_provider,
                    now_ts=now_ts,
                    record_snapshot=(provider_usage is not None),
                )
                decision["breakdown"] = breakdown
                db.execute("COMMIT")
                return decision
            except Exception:
                db.execute("ROLLBACK")
                raise

    def record_runtime_usage(
        self,
        job_id: str,
        attempt: int,
        cu_observed: float,
        *,
        measurement_source: str = CloudCostMeasurementSource.RUNTIME_RATE_ESTIMATE.value,
        provider_balance: Optional[float] = None,
        now: Optional[float] = None,
        enforce_budget: bool = True,
    ) -> Dict[str, Any]:
        """Ghi nhận usage đang chạy (ước tính theo thời gian/rate hoặc số dư) và kiểm tra trần ngân sách."""
        now_ts = time.time() if now is None else float(now)
        obs = max(0.0, round(float(cu_observed or 0.0), 6))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                job_row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
                if not job_row:
                    summary = self._compute_budget_summary_in_db(db, now_ts=now_ts)
                    db.execute("COMMIT")
                    return {"within_budget": False, "reason": "missing_job", "budget": summary}

                if not self._is_terminal_db_state(job_row["state"]) and int(job_row["attempt"] or 0) == int(attempt):
                    db.execute(
                        "UPDATE cloud_jobs SET cu_actual = MAX(COALESCE(cu_actual, 0.0), ?), updated_at = ? WHERE job_id = ?",
                        (obs, now_ts, job_id),
                    )

                ledger_row = db.execute(
                    "SELECT * FROM cloud_cost_ledger WHERE job_id = ? AND attempt = ?",
                    (job_id, int(attempt)),
                ).fetchone()
                if ledger_row is None:
                    breakdown = self.calculate_reservation_breakdown(
                        gpu_type=job_row["gpu_type"],
                        cu_estimated=float(job_row["cu_estimated"] or 0.0),
                    )
                    self._upsert_claimed_ledger_in_db(
                        db,
                        job_id=job_id,
                        attempt=int(attempt),
                        gpu_type=job_row["gpu_type"],
                        task_type=job_row["task_type"],
                        cu_estimated=float(job_row["cu_estimated"] or 0.0),
                        cu_buffer=float(job_row["cu_buffer"] or breakdown["cu_buffer"]),
                        cu_reserved=float(job_row["cu_reserved"] or breakdown["cu_reserved"]),
                        now=float(job_row["started_at"] or now_ts),
                    )

                db.execute(
                    """
                    UPDATE cloud_cost_ledger
                    SET cu_observed = MAX(COALESCE(cu_observed, 0.0), ?),
                        measurement_source = ?,
                        provider_balance_after = COALESCE(?, provider_balance_after),
                        status = CASE
                            WHEN status = 'reserved' THEN 'active'
                            ELSE status
                        END,
                        updated_at = ?
                    WHERE job_id = ? AND attempt = ?
                    """,
                    (obs, str(measurement_source), provider_balance, now_ts, job_id, int(attempt)),
                )
                summary = self._compute_budget_summary_in_db(db, now_ts=now_ts)
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise

        budget_daily = float(summary["budget_daily"])
        over_daily = enforce_budget and (
            (budget_daily > 0.0 and summary["spent_today"] >= budget_daily - 1e-9)
            or (summary["committed_today"] > budget_daily + 1e-6)
        )
        provider_low = enforce_budget and (
            provider_balance is not None and float(provider_balance) <= float(DEFAULT_CONFIG.get("min_provider_balance_cu", 0.0))
        )
        return {
            "within_budget": not (over_daily or provider_low),
            "over_daily_budget": over_daily,
            "provider_exhausted": provider_low,
            "cu_observed": obs,
            "budget": summary,
        }

    def list_cost_ledger(
        self,
        *,
        job_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Lấy lịch sử sổ chi phí bất biến (bao gồm cả job lỗi, huỷ hoặc đã ẩn khỏi UI)."""
        with self._connect() as db:
            if job_id:
                rows = db.execute(
                    "SELECT * FROM cloud_cost_ledger WHERE job_id = ? ORDER BY attempt ASC, id ASC LIMIT ?",
                    (job_id, int(limit)),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM cloud_cost_ledger ORDER BY id DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        items: List[Dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["is_estimated"] = (
                item.get("measurement_source")
                != CloudCostMeasurementSource.PROVIDER_BALANCE_DELTA.value
            )
            items.append(item)
        return items

    def get_cu_spent_today(self, *, now: Optional[float] = None) -> float:
        with self._connect() as db:
            return round(self._compute_budget_summary_in_db(db, now_ts=now)["spent_today"], 3)

    def get_cu_budget_info(self, *, now: Optional[float] = None) -> Dict[str, Any]:
        with self._connect() as db:
            return self._compute_budget_summary_in_db(db, now_ts=now)

    def _query_sessions_with_status(self, adapter: Any) -> Dict[str, Any]:
        if hasattr(adapter, "get_sessions_status"):
            return adapter.get_sessions_status()
        try:
            sessions = adapter.get_sessions()
            return {"ok": True, "sessions": sessions or [], "error": None}
        except Exception as exc:
            return {"ok": False, "sessions": [], "error": redact_secrets(str(exc))}

    @staticmethod
    def _derive_expected_session_name(job_id: str, attempt: int) -> str:
        clean_id = re.sub(r"[^a-zA-Z0-9]", "", str(job_id))[-8:] or "unknown"
        return f"cjob{clean_id}a{max(1, int(attempt or 1))}"

    def recover(
        self,
        adapter: Any = None,
        *,
        only_expired_leases: bool = False,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Đối soát job dở dang & lease/session thật khi khởi động hoặc định kỳ.
        - Không dùng tuổi created_at rồi bỏ mặc VM đang chạy.
        - Kiểm tra cả session mồ côi nếu crash giữa lúc create_session và register_session.
        - Nếu provider mất kết nối, ghi nhận provider_unreachable thay vì tuyên bố đã dừng.
        """
        current_time = time.time() if now is None else float(now)
        placeholders = ", ".join("?" for _ in _ACTIVE_DB_STATES)

        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM cloud_jobs WHERE state IN ({placeholders}) ORDER BY id ASC",
                _ACTIVE_DB_STATES,
            ).fetchall()
            jobs_to_check = [self._row_to_job(r) for r in rows]

        if only_expired_leases:
            jobs_to_check = [
                j
                for j in jobs_to_check
                if j.lease_expires_at is None or float(j.lease_expires_at) < current_time
            ]

        pending_events = self.list_pending_cleanup_events()
        reclaimable_warm = self.list_reclaimable_warm_sessions(
            only_expired=only_expired_leases,
            now=current_time,
        )
        need_provider = (
            (adapter is not None and bool(jobs_to_check))
            or any(bool(j.session_name) for j in jobs_to_check)
            or bool(pending_events)
            or bool(reclaimable_warm)
        )

        resolved_adapter = adapter
        provider_status: Optional[Dict[str, Any]] = None
        if need_provider:
            if resolved_adapter is None:
                from .colab_bridge import ColabProviderAdapter
                resolved_adapter = ColabProviderAdapter()
            provider_status = self._query_sessions_with_status(resolved_adapter)

        active_provider_names: Optional[set[str]] = None
        if provider_status is not None and provider_status.get("ok"):
            active_provider_names = {
                s.get("name")
                for s in (provider_status.get("sessions") or [])
                if s.get("name") and s.get("is_active", True)
            }

        results: List[Dict[str, Any]] = []
        handled_job_ids: set[str] = set()

        for job in jobs_to_check:
            handled_job_ids.add(job.job_id)
            wants_cancel = bool(job.cancel_requested_at) or job.state in (
                CloudJobState.CANCEL_REQUESTED.value,
                CloudJobState.CANCELLING.value,
            )

            if not job.session_name and active_provider_names is not None and int(job.attempt or 0) >= 1:
                expected_session = self._derive_expected_session_name(job.job_id, int(job.attempt or 1))
                if expected_session in active_provider_names:
                    job.session_name = expected_session
                    with self._connect() as db:
                        db.execute(
                            "UPDATE cloud_jobs SET session_name = COALESCE(session_name, ?), updated_at = ? WHERE job_id = ?",
                            (expected_session, current_time, job.job_id),
                        )

            if not job.session_name:
                with self._connect() as db:
                    unverified_legacy = db.execute("""SELECT 1 FROM cloud_cost_ledger
                        WHERE job_id=? AND legacy_hourly_rate>0 AND usage_ended_at IS NULL
                          AND cleanup_verified=0""", (job.job_id,)).fetchone()
                if unverified_legacy:
                    # A missing legacy session name is not proof the VM stopped.
                    results.append({"job_id": job.job_id, "state": job.state,
                                    "cleanup_status": CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
                                    "error": "Phiên GPU cũ thiếu định danh; cần xác minh thu hồi trước khi đối soát."})
                    continue
                if wants_cancel:
                    self.update_progress(
                        job.job_id,
                        state=CloudJobState.CANCELLED.value,
                        step_name="Đã huỷ sau đối soát khởi động (không có session GPU)",
                        error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                        error=job.error or "Người dùng đã huỷ công việc",
                        cleanup_status=CloudCleanupStatus.VERIFIED.value,
                        allow_from_cancel=True,
                    )
                    final_state = CloudJobState.CANCELLED.value
                else:
                    self.update_progress(
                        job.job_id,
                        state=CloudJobState.FAILED.value,
                        step_name="Tiến trình bị gián đoạn trước khi tạo session GPU",
                        error_code=CloudErrorCode.LEASE_EXPIRED.value,
                        error="Tiến trình bị gián đoạn do restart hoặc mất lease (không có session GPU đang mở)",
                        cleanup_status=CloudCleanupStatus.VERIFIED.value,
                        allow_from_cancel=True,
                    )
                    final_state = CloudJobState.FAILED.value
                results.append(
                    {
                        "job_id": job.job_id,
                        "state": final_state,
                        "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                    }
                )
                continue

            assert provider_status is not None
            if not provider_status.get("ok"):
                err_msg = provider_status.get("error") or "Mất kết nối tới provider Colab khi đối soát session"
                self.record_cleanup_result(
                    job.job_id,
                    max(1, int(job.attempt or 1)),
                    job.session_name,
                    verified_stopped=False,
                    cleanup_status=CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
                    attempts=1,
                    error=err_msg,
                )
                self.update_progress(
                    job.job_id,
                    step_name="Mất kết nối provider khi đối soát session; giữ nguyên chờ thử lại",
                    cleanup_status=CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
                    error=err_msg,
                    allow_from_cancel=True,
                )
                results.append(
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "cleanup_status": CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
                        "error": err_msg,
                    }
                )
                continue

            active_names = {
                s.get("name")
                for s in (provider_status.get("sessions") or [])
                if s.get("name") and s.get("is_active", True)
            }
            if job.session_name not in active_names:
                cleanup_res = {
                    "verified_stopped": True,
                    "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                    "attempts": 0,
                    "error": None,
                }
                self.record_cleanup_result(
                    job.job_id,
                    max(1, int(job.attempt or 1)),
                    job.session_name,
                    verified_stopped=True,
                    cleanup_status=CloudCleanupStatus.VERIFIED.value,
                    attempts=1,
                    error=None,
                )
            else:
                cleanup_res = self._stop_and_verify_with_adapter(resolved_adapter, job.session_name)
                self.record_cleanup_result(
                    job.job_id,
                    max(1, int(job.attempt or 1)),
                    job.session_name,
                    verified_stopped=bool(cleanup_res.get("verified_stopped")),
                    cleanup_status=str(
                        cleanup_res.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)
                    ),
                    attempts=int(cleanup_res.get("attempts", 1)),
                    error=cleanup_res.get("error"),
                )

            if cleanup_res.get("verified_stopped"):
                if wants_cancel:
                    self.update_progress(
                        job.job_id,
                        state=CloudJobState.CANCELLED.value,
                        step_name="Đã huỷ và thu hồi session GPU khi đối soát",
                        error_code=CloudErrorCode.CANCELLED_BY_USER.value,
                        error=job.error or "Người dùng đã huỷ công việc",
                        cleanup_status=CloudCleanupStatus.VERIFIED.value,
                        allow_from_cancel=True,
                    )
                    final_state = CloudJobState.CANCELLED.value
                else:
                    self.update_progress(
                        job.job_id,
                        state=CloudJobState.FAILED.value,
                        step_name="Tiến trình bị gián đoạn; đã đối soát và thu hồi session GPU",
                        error_code=CloudErrorCode.LEASE_EXPIRED.value,
                        error="Tiến trình bị gián đoạn do restart hoặc mất lease; đã đối soát và thu hồi session GPU",
                        cleanup_status=CloudCleanupStatus.VERIFIED.value,
                        allow_from_cancel=True,
                    )
                    final_state = CloudJobState.FAILED.value
                results.append(
                    {
                        "job_id": job.job_id,
                        "state": final_state,
                        "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                    }
                )
            else:
                c_status = str(
                    cleanup_res.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)
                )
                c_err = cleanup_res.get("error") or "Không thể dừng session GPU khi đối soát"
                self.update_progress(
                    job.job_id,
                    step_name="Chưa thu hồi được session GPU; giữ nguyên sự kiện chờ xử lý",
                    cleanup_status=c_status,
                    error=c_err,
                    allow_from_cancel=True,
                )
                results.append(
                    {
                        "job_id": job.job_id,
                        "state": job.state,
                        "cleanup_status": c_status,
                        "error": c_err,
                    }
                )

        # Thử lại các sự kiện cleanup còn tồn đọng không thuộc job vừa kiểm tra
        if provider_status is not None and provider_status.get("ok"):
            active_names = {
                s.get("name")
                for s in (provider_status.get("sessions") or [])
                if s.get("name") and s.get("is_active", True)
            }
            for ev in pending_events:
                if ev["job_id"] in handled_job_ids:
                    continue
                if only_expired_leases:
                    continue
                s_name = ev["session_name"]
                if s_name not in active_names:
                    self.record_cleanup_result(
                        ev["job_id"],
                        int(ev["attempt"]),
                        s_name,
                        verified_stopped=True,
                        cleanup_status=CloudCleanupStatus.VERIFIED.value,
                        attempts=1,
                        error=None,
                    )
                else:
                    cleanup_res = self._stop_and_verify_with_adapter(resolved_adapter, s_name)
                    self.record_cleanup_result(
                        ev["job_id"],
                        int(ev["attempt"]),
                        s_name,
                        verified_stopped=bool(cleanup_res.get("verified_stopped")),
                        cleanup_status=str(
                            cleanup_res.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)
                        ),
                        attempts=int(cleanup_res.get("attempts", 1)),
                        error=cleanup_res.get("error"),
                    )

        if reclaimable_warm and provider_status is not None:
            if provider_status.get("ok"):
                active_names_warm = {
                    s.get("name")
                    for s in (provider_status.get("sessions") or [])
                    if s.get("name") and s.get("is_active", True)
                }
                for w_row in reclaimable_warm:
                    s_name = str(w_row.get("session_name") or "")
                    if not s_name:
                        continue
                    w_job_id = str(w_row.get("source_job_id") or f"warm:{s_name}")
                    if s_name not in active_names_warm:
                        self.remove_warm_session(s_name)
                        results.append({
                            "job_id": w_job_id,
                            "session_name": s_name,
                            "state": "warm_reclaimed",
                            "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                        })
                    else:
                        cleanup_res = self._stop_and_verify_with_adapter(resolved_adapter, s_name)
                        if cleanup_res.get("verified_stopped"):
                            self.remove_warm_session(s_name)
                            results.append({
                                "job_id": w_job_id,
                                "session_name": s_name,
                                "state": "warm_reclaimed",
                                "cleanup_status": CloudCleanupStatus.VERIFIED.value,
                            })
                        else:
                            results.append({
                                "job_id": w_job_id,
                                "session_name": s_name,
                                "state": "warm_reclaim_pending",
                                "cleanup_status": str(
                                    cleanup_res.get("cleanup_status", CloudCleanupStatus.FAILED_RETRY_NEEDED.value)
                                ),
                                "error": cleanup_res.get("error"),
                            })
            else:
                err_msg = provider_status.get("error") or "Mất kết nối provider khi thu hồi warm session"
                for w_row in reclaimable_warm:
                    s_name = str(w_row.get("session_name") or "")
                    if s_name:
                        results.append({
                            "job_id": str(w_row.get("source_job_id") or f"warm:{s_name}"),
                            "session_name": s_name,
                            "state": "warm_reclaim_pending",
                            "cleanup_status": CloudCleanupStatus.PROVIDER_UNREACHABLE.value,
                            "error": err_msg,
                        })

        return results

    def reconcile_expired_leases(
        self,
        adapter: Any = None,
        now: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Đối soát định kỳ các job bị quá hạn lease khi worker đang rảnh."""
        return self.recover(adapter=adapter, only_expired_leases=True, now=now)

    @classmethod
    def _row_to_job(cls, row: sqlite3.Row) -> CloudJob:
        keys = set(row.keys())
        return CloudJob(
            id=row["id"],
            job_id=row["job_id"],
            video_path=row["video_path"],
            video_name=row["video_name"],
            gpu_type=row["gpu_type"],
            task_type=row["task_type"],
            state=row["state"],
            percent=row["percent"],
            step_name=row["step_name"],
            session_name=row["session_name"],
            cu_estimated=row["cu_estimated"],
            cu_actual=row["cu_actual"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result_path=row["result_path"],
            error=row["error"],
            voice=row["voice"] if "voice" in keys else "BV562_streaming",
            version=int(row["version"] or 1) if "version" in keys else 1,
            attempt=int(row["attempt"] or 0) if "attempt" in keys else 0,
            owner_id=row["owner_id"] if "owner_id" in keys else None,
            lease_expires_at=row["lease_expires_at"] if "lease_expires_at" in keys else None,
            heartbeat_at=row["heartbeat_at"] if "heartbeat_at" in keys else None,
            cancel_requested_at=row["cancel_requested_at"] if "cancel_requested_at" in keys else None,
            error_code=row["error_code"] if "error_code" in keys else None,
            cleanup_status=(row["cleanup_status"] or "none") if "cleanup_status" in keys else "none",
            cleanup_attempts=int(row["cleanup_attempts"] or 0) if "cleanup_attempts" in keys else 0,
            cleanup_last_error=row["cleanup_last_error"] if "cleanup_last_error" in keys else None,
            updated_at=row["updated_at"] if "updated_at" in keys else None,
            cu_buffer=float(row["cu_buffer"] or 0.0) if "cu_buffer" in keys else 0.0,
            cu_reserved=float(row["cu_reserved"] or 0.0) if "cu_reserved" in keys else 0.0,
            hidden_from_ui=int(row["hidden_from_ui"] or 0) if "hidden_from_ui" in keys else 0,
            output_manifest=_parse_optional_json_dict(
                row["output_manifest_json"] if "output_manifest_json" in keys else None
            ),
            idempotency_key=row["idempotency_key"] if "idempotency_key" in keys else None,
            options=_parse_optional_json_dict(
                row["options_json"] if "options_json" in keys else None
            ),
            deadline_at=row["deadline_at"] if "deadline_at" in keys else None,
        )

    @staticmethod
    def _parse_optional_json_dict(raw_val: Optional[str]) -> Optional[Dict[str, Any]]:
        return _parse_optional_json_dict(raw_val)
