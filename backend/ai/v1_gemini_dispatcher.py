"""
Unified Gemini Dispatcher & Circuit Breaker for Tool V1.
Strictly implements the Codex Plan for Gemini Stability & Speed:
1. Key Isolation: validates GEMINI_API_KEY, rejects FPT/VOICE keys, masks keys in logs.
2. Single Point of Retry & Error Classification: 400, 401/403, 404, 429, 500-504, timeout, invalid payload.
3. Multi-Process Circuit Breaker via SQLite WAL:
   - States: ACTIVE, COOLDOWN, HALF_OPEN (single probe request).
   - Exponential backoff: 60s -> 120s -> 300s max.
   - Honors Retry-After header.
4. Total Deadline Budgeting: Monotonic clock countdown, aborts immediately on cancellation.
5. Structured Logging: Purpose, model, attempt, timing, status, cooldown, cue counts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Base paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BACKEND_DIR.parent / "workspace"
DB_DIR = WORKSPACE_DIR / "bot_system"
DB_PATH = DB_DIR / "gemini_circuit_breaker.db"

# Model presets as specified in Codex Plan
DEFAULT_TRANSLATION_MODELS = [
    "gemini-3.7-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
]

DEFAULT_CONDENSATION_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
]

# Exceptions
class GeminiError(Exception):
    """Base exception for Gemini dispatcher."""
    pass

class GeminiConfigError(GeminiError):
    """Raised when GEMINI_API_KEY is missing, invalid, or mismatched."""
    pass

class GeminiAuthError(GeminiError):
    """Raised when authentication fails (HTTP 401/403)."""
    pass

class GeminiDeadlineError(GeminiError):
    """Raised when overall deadline expires."""
    pass

class GeminiCancelledError(GeminiError):
    """Raised when job is stopped or cancelled."""
    pass

class GeminiQuotaExhaustedError(GeminiError):
    """Raised when all candidates or account are rate-limited (HTTP 429)."""
    pass

class GeminiAllModelsFailedError(GeminiError):
    """Raised when all candidate models fail within budget."""
    pass


def mask_secret(text: str) -> str:
    """Mask any API keys in URLs or strings for secure logging."""
    if not text:
        return ""
    text = re.sub(r'key=([a-zA-Z0-9_\-\.]+)', r'key=\1'[:8] + '...', str(text))
    text = re.sub(r'AIza[a-zA-Z0-9_\-]{10,}', 'AIza...', text)
    return text


def get_gemini_api_key(explicit_key: Optional[str] = None) -> str:
    """
    Validate and return the genuine GEMINI_API_KEY.
    Strictly prevents taking FPT_API_KEY, VOICE_API_KEY, or empty credentials.
    """
    fpt_key = os.getenv("FPT_API_KEY", "").strip()
    voice_key = os.getenv("VOICE_API_KEY", "").strip()

    candidate = (explicit_key or "").strip()
    # Reject if candidate is actually an FPT or voice key
    if candidate and (candidate == fpt_key or candidate == voice_key or "fpt" in candidate.lower()):
        candidate = ""

    if not candidate:
        candidate = os.getenv("GEMINI_API_KEY", "").strip()

    if not candidate:
        env_file = BACKEND_DIR / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = line.strip()
                    if line.startswith("GEMINI_API_KEY=") and not line.startswith("#"):
                        candidate = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except Exception:
                pass

    if not candidate:
        raise GeminiConfigError("GEMINI_API_KEY is not configured in environment or .env file.")

    if len(candidate) < 10:
        raise GeminiConfigError("GEMINI_API_KEY appears malformed or too short.")

    return candidate


def _get_account_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


class GeminiCircuitBreaker:
    """
    SQLite-backed circuit breaker with WAL mode for safe multi-worker concurrency.
    Tracks state per (account_hash, model, purpose).
    """
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self._ensure_db()

    def _ensure_db(self):
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(self.db_path), timeout=15.0) as conn:
                conn.execute("PRAGMA journal_mode=WAL;")
                conn.execute("PRAGMA synchronous=NORMAL;")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS model_circuit (
                        account_hash TEXT,
                        model TEXT,
                        purpose TEXT,
                        state TEXT DEFAULT 'ACTIVE',
                        cooldown_until REAL DEFAULT 0.0,
                        consecutive_failures INTEGER DEFAULT 0,
                        last_error_type TEXT,
                        last_error_code INTEGER,
                        last_used REAL,
                        last_success REAL,
                        PRIMARY KEY (account_hash, model, purpose)
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS account_circuit (
                        account_hash TEXT PRIMARY KEY,
                        quota_exhausted_until REAL DEFAULT 0.0,
                        last_error_type TEXT
                    );
                """)
        except Exception as e:
            logger.warning(f"[CIRCUIT_BREAKER] Failed to initialize SQLite database: {e}")

    def get_candidate_models(
        self,
        account_hash: str,
        purpose: str,
        candidates: Sequence[str]
    ) -> List[Tuple[str, str]]:
        """
        Check circuit status for each candidate model.
        Returns list of tuples: (model_name, state) where state in ('ACTIVE', 'HALF_OPEN').
        Models in active COOLDOWN are excluded.
        """
        now = time.time()
        allowed = []
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                for model in candidates:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT state, cooldown_until, consecutive_failures FROM model_circuit "
                        "WHERE account_hash = ? AND model = ? AND purpose = ?",
                        (account_hash, model, purpose)
                    )
                    row = cur.fetchone()
                    if not row:
                        allowed.append((model, "ACTIVE"))
                        continue

                    state, cooldown_until, consec = row
                    if cooldown_until > now:
                        # In cooldown: skip without network request
                        continue

                    if state == "COOLDOWN" and cooldown_until <= now:
                        # Try to transition to HALF_OPEN (probe request)
                        # Atomic update ensures only ONE worker probes at a time
                        cur.execute(
                            "UPDATE model_circuit SET state = 'HALF_OPEN', cooldown_until = ? "
                            "WHERE account_hash = ? AND model = ? AND purpose = ? AND state != 'HALF_OPEN'",
                            (now + 40.0, account_hash, model, purpose)
                        )
                        conn.commit()
                        if cur.rowcount > 0:
                            allowed.append((model, "HALF_OPEN"))
                    else:
                        allowed.append((model, "ACTIVE"))
        except Exception as e:
            logger.warning(f"[CIRCUIT_BREAKER] DB read error: {e}. Falling back to default list.")
            return [(m, "ACTIVE") for m in candidates]

        return allowed

    def record_success(self, account_hash: str, model: str, purpose: str):
        now = time.time()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute(
                    "INSERT INTO model_circuit (account_hash, model, purpose, state, cooldown_until, consecutive_failures, last_used, last_success) "
                    "VALUES (?, ?, ?, 'ACTIVE', 0.0, 0, ?, ?) "
                    "ON CONFLICT(account_hash, model, purpose) DO UPDATE SET "
                    "state = 'ACTIVE', cooldown_until = 0.0, consecutive_failures = 0, last_used = excluded.last_used, last_success = excluded.last_success;",
                    (account_hash, model, purpose, now, now)
                )
        except Exception as e:
            logger.debug(f"[CIRCUIT_BREAKER] Failed to record success: {e}")

    def record_failure(
        self,
        account_hash: str,
        model: str,
        purpose: str,
        status_code: Optional[int],
        error_type: str,
        retry_after_seconds: Optional[float] = None
    ):
        now = time.time()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT consecutive_failures FROM model_circuit WHERE account_hash = ? AND model = ? AND purpose = ?",
                    (account_hash, model, purpose)
                )
                row = cur.fetchone()
                consec = (row[0] if row else 0) + 1

                # Determine cooldown duration based on error category
                if status_code == 404:
                    # Model not supported/found -> 24 hour disable
                    wait_s = 86400.0
                elif status_code == 429:
                    if retry_after_seconds and retry_after_seconds > 0:
                        wait_s = min(300.0, retry_after_seconds)
                    else:
                        # Exponential cooldown: 60s -> 120s -> 240s -> 300s max
                        wait_s = min(300.0, 60.0 * (2 ** (consec - 1)))
                elif status_code in (500, 502, 503, 504):
                    wait_s = min(180.0, 45.0 * consec)
                elif error_type in ("Timeout", "ConnectionError"):
                    wait_s = min(90.0, 20.0 * consec)
                else:
                    wait_s = min(120.0, 30.0 * consec)

                cooldown_until = now + wait_s
                conn.execute(
                    "INSERT INTO model_circuit (account_hash, model, purpose, state, cooldown_until, consecutive_failures, last_error_type, last_error_code, last_used) "
                    "VALUES (?, ?, ?, 'COOLDOWN', ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_hash, model, purpose) DO UPDATE SET "
                    "state = 'COOLDOWN', cooldown_until = excluded.cooldown_until, consecutive_failures = excluded.consecutive_failures, "
                    "last_error_type = excluded.last_error_type, last_error_code = excluded.last_error_code, last_used = excluded.last_used;",
                    (account_hash, model, purpose, cooldown_until, consec, error_type, status_code, now)
                )
        except Exception as e:
            logger.debug(f"[CIRCUIT_BREAKER] Failed to record failure: {e}")


# Global circuit breaker instance
circuit_breaker = GeminiCircuitBreaker()


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    if not header_val:
        return None
    try:
        return float(header_val.strip())
    except ValueError:
        return None


def call_gemini_api(
    payload: dict,
    purpose: str = "translation",
    models: Optional[Sequence[str]] = None,
    overall_deadline: Optional[float] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
    job_id: str = "default",
    api_key: Optional[str] = None,
    timeout_per_request: float = 20.0,
) -> Tuple[dict, str]:
    """
    Centralized, resilient dispatcher for all Gemini API calls in Tool V1.
    Strictly follows Codex Plan:
    - Verifies genuine GEMINI_API_KEY.
    - Honors shared circuit breaker and probe mechanics.
    - Enforces overall monotonic deadline.
    - Single point of retry logic.
    - Masks secrets in logs.
    """
    import requests

    real_key = get_gemini_api_key(api_key)
    account_hash = _get_account_hash(real_key)

    if models is None:
        if purpose == "condensation":
            pref = os.getenv("GEMINI_CONDENSE_MODEL", "").strip()
            models = ([pref] if pref else []) + DEFAULT_CONDENSATION_MODELS
        else:
            pref = os.getenv("GEMINI_MODEL", "").strip()
            models = ([pref] if pref else []) + DEFAULT_TRANSLATION_MODELS

    # Deduplicate while preserving order
    ordered_models = []
    for m in models:
        if m and m not in ordered_models:
            ordered_models.append(m)

    # Monotonic deadline enforcement
    if overall_deadline is None:
        budget = 90.0 if purpose == "translation" else 45.0
        overall_deadline = time.monotonic() + budget

    if stop_checker and stop_checker():
        logger.info(f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Hủy theo yêu cầu /stop.")
        raise GeminiCancelledError("Operation cancelled by user stop request.")

    candidate_statuses = circuit_breaker.get_candidate_models(account_hash, purpose, ordered_models)
    if not candidate_statuses:
        # All models in circuit cooldown
        logger.warning(
            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Tất cả các model đều đang trong thời gian nghỉ (COOLDOWN)!"
        )
        raise GeminiQuotaExhaustedError("All Gemini models are currently in cooldown due to previous rate limits.")

    headers = {"Content-Type": "application/json"}
    last_exception = None

    for attempt, (model, state) in enumerate(candidate_statuses):
        # 1. Check cancellation token
        if stop_checker and stop_checker():
            logger.info(f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Hủy theo yêu cầu /stop.")
            raise GeminiCancelledError("Operation cancelled by user stop request.")

        # 2. Check remaining time budget
        remaining = overall_deadline - time.monotonic()
        if remaining <= 1.0:
            logger.warning(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Đã hết ngân sách thời gian tổng ({remaining:.1f}s còn lại)."
            )
            raise GeminiDeadlineError("Gemini call exceeded overall deadline budget.")

        connect_timeout = min(5.0, max(1.0, remaining / 2.0))
        read_timeout = min(timeout_per_request, max(2.0, remaining - connect_timeout))

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={real_key}"
        masked_url = mask_secret(url)
        t_start = time.monotonic()

        state_tag = f"[{state}]" if state != "ACTIVE" else ""
        logger.info(
            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} attempt={attempt+1} {state_tag} "
            f"timeout=({connect_timeout:.1f}s, {read_timeout:.1f}s) remaining={remaining:.1f}s"
        )

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=(connect_timeout, read_timeout))
            resp.encoding = "utf-8"
            elapsed_ms = int((time.monotonic() - t_start) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                circuit_breaker.record_success(account_hash, model, purpose)
                logger.info(
                    f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} "
                    f"SUCCESS status=200 elapsed={elapsed_ms}ms"
                )
                return data, model

            # Error handling
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            status = resp.status_code
            retry_after = parse_retry_after(resp.headers.get("Retry-After"))

            logger.warning(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} "
                f"FAILED status={status} elapsed={elapsed_ms}ms retry_after={retry_after}"
            )

            circuit_breaker.record_failure(
                account_hash, model, purpose, status, f"HTTP_{status}", retry_after
            )

            if status in (401, 403):
                # Authentication/Permission error: stop chain immediately
                raise GeminiAuthError(f"Gemini API key is invalid or lacks permission (HTTP {status}).")

            if status == 400:
                # Malformed request: parse structured error
                try:
                    err_json = resp.json()
                    err_msg = err_json.get("error", {}).get("message", "Bad request")
                except Exception:
                    err_msg = resp.text[:200]
                logger.error(f"[GEMINI_DISPATCHER] HTTP 400 Bad Request: {err_msg}")
                # Don't keep hammering if payload itself is fundamentally broken
                last_exception = GeminiError(f"HTTP 400: {err_msg}")
                continue

            last_exception = GeminiError(f"HTTP {status} from {model}")

        except requests.Timeout as e:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            logger.warning(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} TIMEOUT after {elapsed_ms}ms"
            )
            circuit_breaker.record_failure(account_hash, model, purpose, None, "Timeout")
            last_exception = e

        except requests.ConnectionError as e:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            logger.warning(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} CONNECTION_ERROR after {elapsed_ms}ms"
            )
            circuit_breaker.record_failure(account_hash, model, purpose, None, "ConnectionError")
            last_exception = e

        except Exception as e:
            if isinstance(e, (GeminiAuthError, GeminiDeadlineError, GeminiCancelledError)):
                raise
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            logger.warning(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} ERROR: {type(e).__name__} after {elapsed_ms}ms"
            )
            circuit_breaker.record_failure(account_hash, model, purpose, None, type(e).__name__)
            last_exception = e

    raise GeminiAllModelsFailedError(f"All candidate Gemini models failed for {purpose}: {last_exception}")
