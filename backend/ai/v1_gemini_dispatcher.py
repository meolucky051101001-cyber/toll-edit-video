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
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Base paths
BACKEND_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = BACKEND_DIR.parent / "workspace"
DB_DIR = WORKSPACE_DIR / "bot_system"
DB_PATH = DB_DIR / "gemini_circuit_breaker.db"

# Model presets prioritizing fastest, verified active models
DEFAULT_TRANSLATION_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-flash-latest",
]

DEFAULT_CONDENSATION_MODELS = [
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-flash-latest",
]

# Exceptions
class GeminiError(Exception):
    """Base exception for Gemini dispatcher."""

class GeminiConfigError(GeminiError):
    """Raised when GEMINI_API_KEY is missing, invalid, or mismatched."""

class GeminiAuthError(GeminiError):
    """Raised when authentication fails (HTTP 401/403)."""

class GeminiDeadlineError(GeminiError):
    """Raised when overall deadline expires."""

class GeminiCancelledError(GeminiError):
    """Raised when job is stopped or cancelled."""

class GeminiQuotaExhaustedError(GeminiError):
    """Raised when all candidates or account are rate-limited (HTTP 429)."""

class GeminiAllModelsFailedError(GeminiError):
    """Raised when all candidate models fail within budget."""

class GeminiRequestError(GeminiError):
    """Invalid shared request; changing models cannot repair it."""


def mask_secret(text: str) -> str:
    """Mask any API keys in URLs or strings for secure logging."""
    if not text:
        return ""
    text = re.sub(r'(?i)(key|api_key|token)=([^\s&#]+)', r'\1=[REDACTED]', str(text))
    text = re.sub(r'AIza[a-zA-Z0-9_\-]{10,}', 'AIza...', text)
    text = re.sub(r'AQ\.[a-zA-Z0-9_\-]{10,}', 'AQ...[REDACTED]', text)
    return text



def get_gemini_api_keys(explicit_key: Optional[str] = None) -> List[str]:
    """
    Validate and return a list of genuine GEMINI API keys.
    Supports:
    - explicit_key (single key or comma/semicolon/newline-separated string)
    - GEMINI_API_KEYS (comma, semicolon, or newline separated in env or .env)
    - GEMINI_API_KEY (single key in env or .env)
    - GEMINI_API_KEY_1..N
    Strictly filters out FPT_API_KEY, VOICE_API_KEY, and malformed strings.
    """
    fpt_key = os.getenv("FPT_API_KEY", "").strip()
    voice_key = os.getenv("VOICE_API_KEY", "").strip()

    def is_valid_key(k: str) -> bool:
        k = k.strip().strip('"').strip("'")
        if not k or len(k) < 10:
            return False
        if k == fpt_key or k == voice_key or "fpt" in k.lower():
            return False
        return True

    candidates = []

    single_env_key = os.getenv("GEMINI_API_KEY", "").strip()

    # 1. Check explicit_key (only lock to explicit_key if caller passed a custom key/list different from default GEMINI_API_KEY)
    if explicit_key and explicit_key.strip() != single_env_key:
        for raw in re.split(r'[,;\n]+', explicit_key):
            raw = raw.strip()
            if is_valid_key(raw):
                candidates.append(raw)
        if candidates:
            # When caller explicitly passes a custom key/list, honor only explicit keys
            deduped = []
            for k in candidates:
                if k not in deduped:
                    deduped.append(k)
            return deduped

    # 2. Check GEMINI_API_KEYS environment variable
    env_keys = os.getenv("GEMINI_API_KEYS", "").strip()
    if env_keys:
        for raw in re.split(r'[,;\n]+', env_keys):
            raw = raw.strip()
            if is_valid_key(raw):
                candidates.append(raw)

    # 3. Check GEMINI_API_KEY environment variable
    single_key = os.getenv("GEMINI_API_KEY", "").strip()
    if single_key and is_valid_key(single_key):
        candidates.append(single_key)

    # 4. Check .env file
    env_file = BACKEND_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("#"):
                    continue
                if line.startswith("GEMINI_API_KEYS="):
                    val = line.split("=", 1)[1].strip()
                    for raw in re.split(r'[,;\n]+', val):
                        raw = raw.strip().strip('"').strip("'")
                        if is_valid_key(raw):
                            candidates.append(raw)
                elif line.startswith("GEMINI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if is_valid_key(val):
                        candidates.append(val)
                elif re.match(r"^GEMINI_API_KEY_\d+=", line):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if is_valid_key(val):
                        candidates.append(val)
        except Exception:
            pass

    # Deduplicate while preserving order
    deduped = []
    for k in candidates:
        if k not in deduped:
            deduped.append(k)

    if not deduped:
        raise GeminiConfigError("GEMINI_API_KEY or GEMINI_API_KEYS is not configured in environment or .env file.")

    return deduped


def get_gemini_api_key(explicit_key: Optional[str] = None) -> str:
    """
    Validate and return the primary GEMINI_API_KEY.
    Maintains 100% backward compatibility for single-key callers.
    """
    keys = get_gemini_api_keys(explicit_key)
    return keys[0]


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
                        last_error_type TEXT,
                        consecutive_429 INTEGER DEFAULT 0,
                        last_used REAL DEFAULT 0.0
                    );
                """)
                # Backward-compatibility column migration for existing databases
                for col_name, col_type in [("consecutive_429", "INTEGER DEFAULT 0"), ("last_used", "REAL DEFAULT 0.0")]:
                    try:
                        conn.execute(f"ALTER TABLE account_circuit ADD COLUMN {col_name} {col_type};")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[CIRCUIT_BREAKER] Failed to initialize SQLite database: {e}")

    def get_candidate_models(
        self,
        account_hash: str,
        purpose: str,
        candidates: Sequence[str],
        reserve_probe: bool = True,
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

                    if state in ("COOLDOWN", "HALF_OPEN") and cooldown_until <= now:
                        if not reserve_probe:
                            allowed.append((model, "HALF_OPEN"))
                            continue
                        # Try to transition to HALF_OPEN (probe request)
                        # Atomic update ensures only ONE worker probes at a time
                        cur.execute(
                            "UPDATE model_circuit SET state = 'HALF_OPEN', cooldown_until = ? "
                            "WHERE account_hash = ? AND model = ? AND purpose = ? AND cooldown_until <= ?",
                            (now + 40.0, account_hash, model, purpose, now)
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

    def get_min_cooldown_remaining(
        self,
        account_hash: str,
        purpose: str,
        candidates: Sequence[str],
    ) -> Tuple[Optional[str], float]:
        """Return the candidate model with the smallest remaining cooldown in seconds."""
        now = time.time()
        best_model = None
        min_rem = float("inf")
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                for model in candidates:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT cooldown_until FROM model_circuit "
                        "WHERE account_hash = ? AND model = ? AND purpose = ?",
                        (account_hash, model, purpose),
                    )
                    row = cur.fetchone()
                    if not row:
                        return model, 0.0
                    cooldown_until = row[0] or 0.0
                    rem = max(0.0, cooldown_until - now)
                    if rem < min_rem:
                        min_rem = rem
                        best_model = model
        except Exception:
            pass
        return best_model, min_rem if (best_model and min_rem < float("inf")) else 0.0

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

    def is_account_exhausted(self, account_hash: str) -> Tuple[bool, float]:
        """Check if an account_hash is in active 429 quota exhaustion cooldown."""
        now = time.time()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT quota_exhausted_until FROM account_circuit WHERE account_hash = ?", (account_hash,))
                row = cur.fetchone()
                if row and row[0] and row[0] > now:
                    return True, max(0.0, row[0] - now)
        except Exception:
            pass
        return False, 0.0

    def record_account_quota_exhausted(self, account_hash: str, retry_after_seconds: Optional[float] = None):
        """Mark account as 429 quota exhausted with exponential backoff."""
        now = time.time()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute("SELECT consecutive_429 FROM account_circuit WHERE account_hash = ?", (account_hash,))
                row = cur.fetchone()
                consec = (row[0] if row and row[0] is not None else 0) + 1

                if retry_after_seconds and retry_after_seconds > 0:
                    wait_s = min(300.0, retry_after_seconds)
                else:
                    # Exponential cooldown: 60s -> 120s -> 240s -> 300s max
                    wait_s = min(300.0, 60.0 * (2 ** (consec - 1)))

                quota_until = now + wait_s
                conn.execute(
                    "INSERT INTO account_circuit (account_hash, quota_exhausted_until, last_error_type, consecutive_429, last_used) "
                    "VALUES (?, ?, 'HTTP_429', ?, ?) "
                    "ON CONFLICT(account_hash) DO UPDATE SET "
                    "quota_exhausted_until = excluded.quota_exhausted_until, "
                    "last_error_type = 'HTTP_429', "
                    "consecutive_429 = excluded.consecutive_429, "
                    "last_used = excluded.last_used;",
                    (account_hash, quota_until, consec, now)
                )
        except Exception as e:
            logger.debug(f"[CIRCUIT_BREAKER] Failed to record account quota exhausted: {e}")

    def record_account_success(self, account_hash: str):
        now = time.time()
        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                conn.execute(
                    "INSERT INTO account_circuit (account_hash, quota_exhausted_until, last_error_type, consecutive_429, last_used) "
                    "VALUES (?, 0.0, 'SUCCESS', 0, ?) "
                    "ON CONFLICT(account_hash) DO UPDATE SET "
                    "quota_exhausted_until = 0.0, "
                    "consecutive_429 = 0, "
                    "last_used = excluded.last_used;",
                    (account_hash, now)
                )
        except Exception as e:
            logger.debug(f"[CIRCUIT_BREAKER] Failed to record account success: {e}")

    def select_active_key(self, keys: Sequence[str]) -> Tuple[str, str, float]:
        """
        From candidate API keys, select the best active key:
        - Filters keys where quota_exhausted_until <= now.
        - Among active keys, picks the one with the smallest last_used (Round-Robin / LRU load balance).
        - If all keys are in cooldown, returns the one with the smallest remaining cooldown time.
        Returns: (selected_key, account_hash, cooldown_remaining)
        """
        now = time.time()
        if not keys:
            raise GeminiConfigError("No Gemini API keys available.")
        if len(keys) == 1:
            h = _get_account_hash(keys[0])
            exhausted, rem = self.is_account_exhausted(h)
            return keys[0], h, rem if exhausted else 0.0

        key_map = {_get_account_hash(k): k for k in keys}
        hashes = list(key_map.keys())

        try:
            with sqlite3.connect(str(self.db_path), timeout=10.0) as conn:
                cur = conn.cursor()
                placeholders = ",".join("?" for _ in hashes)
                cur.execute(
                    f"SELECT account_hash, quota_exhausted_until, last_used FROM account_circuit WHERE account_hash IN ({placeholders})",
                    hashes
                )
                rows = {r[0]: (r[1] or 0.0, r[2] or 0.0) for r in cur.fetchall()}

                active_keys = []
                exhausted_keys = []
                for h in hashes:
                    quota_until, last_used = rows.get(h, (0.0, 0.0))
                    if quota_until <= now:
                        active_keys.append((h, last_used))
                    else:
                        exhausted_keys.append((h, quota_until - now))

                if active_keys:
                    active_keys.sort(key=lambda x: x[1])
                    best_hash = active_keys[0][0]
                    return key_map[best_hash], best_hash, 0.0

                if exhausted_keys:
                    exhausted_keys.sort(key=lambda x: x[1])
                    best_hash, min_rem = exhausted_keys[0]
                    return key_map[best_hash], best_hash, min_rem
        except Exception as e:
            logger.warning(f"[CIRCUIT_BREAKER] Failed to query account_circuit: {e}")

        return keys[0], hashes[0], 0.0


# Global circuit breaker instance
circuit_breaker = GeminiCircuitBreaker()


def parse_retry_after(header_val: Optional[str]) -> Optional[float]:
    if not header_val:
        return None
    try:
        return float(header_val.strip())
    except ValueError:
        return None


def _record_dispatcher_active_model(model: str, purpose: str):
    if purpose != "translation":
        return
    try:
        import job_tracker
        job_tracker.record_active_translation_model(model)
    except Exception:
        try:
            from backend import job_tracker
            job_tracker.record_active_translation_model(model)
        except Exception:
            pass


def _record_dispatcher_success_model(model: str, purpose: str):
    if purpose != "translation":
        return
    try:
        import job_tracker
        job_tracker.record_translation_model(model)
    except Exception:
        try:
            from backend import job_tracker
            job_tracker.record_translation_model(model)
        except Exception:
            pass


def call_gemini_api(
    payload: dict,
    purpose: str = "translation",
    models: Optional[Sequence[str]] = None,
    overall_deadline: Optional[float] = None,
    stop_checker: Optional[Callable[[], bool]] = None,
    job_id: str = "default",
    api_key: Optional[str] = None,
    timeout_per_request: float = 20.0,
    response_validator: Optional[Callable[[dict], Any]] = None,
) -> Tuple[dict, str]:
    """
    Centralized, resilient dispatcher for all Gemini API calls in Tool V1.
    Strictly follows Codex Plan:
    - Verifies genuine GEMINI_API_KEY / GEMINI_API_KEYS pool.
    - Honors shared circuit breaker and probe mechanics.
    - Automatic multi-key pool rotation & instant 429 failover.
    - Enforces overall monotonic deadline.
    - Single point of retry logic.
    - Masks secrets in logs.
    """
    import requests

    keys_pool = get_gemini_api_keys(api_key)
    curr_key, account_hash, key_cooldown = circuit_breaker.select_active_key(keys_pool)

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

    candidate_statuses = circuit_breaker.get_candidate_models(account_hash, purpose, ordered_models, reserve_probe=False)
    if not candidate_statuses and len(keys_pool) > 1:
        # Check if other keys in the pool have active models
        for alt_k in keys_pool:
            alt_h = _get_account_hash(alt_k)
            alt_statuses = circuit_breaker.get_candidate_models(alt_h, purpose, ordered_models, reserve_probe=False)
            if alt_statuses:
                curr_key = alt_k
                account_hash = alt_h
                candidate_statuses = alt_statuses
                break

    if not candidate_statuses:
        # Check if the shortest cooldown is short enough to wait out within deadline
        best_model, min_wait = circuit_breaker.get_min_cooldown_remaining(account_hash, purpose, ordered_models)
        remaining = overall_deadline - time.monotonic()
        if 0 < min_wait <= min(remaining - 5.0, 15.0):
            logger.info(
                f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Tất cả model trong cooldown, chờ {min_wait:.1f}s cho {best_model} hồi phục..."
            )
            time.sleep(min_wait + 0.5)
            candidate_statuses = circuit_breaker.get_candidate_models(account_hash, purpose, ordered_models, reserve_probe=False)

    if not candidate_statuses:
        # If still in cooldown, allow an immediate probe rescue on the top priority models rather than failing dead
        logger.warning(
            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose}: Cooldown kích hoạt, cho phép probe thử nghiệm trên model ưu tiên hàng đầu: {ordered_models[:2]}"
        )
        candidate_statuses = [(m, "HALF_OPEN") for m in ordered_models[:2]]

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

        connect_timeout = min(5.0, max(0.1, remaining / 2.0))
        read_timeout = min(timeout_per_request, max(0.1, remaining - connect_timeout))
        # Reserve only the model actually about to be tried. Other candidates
        # remain available to subsequent jobs if this request succeeds early.
        admitted = circuit_breaker.get_candidate_models(account_hash, purpose, [model])
        if not admitted:
            continue
        state = admitted[0][1]

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        t_start = time.monotonic()

        state_tag = f"[{state}]" if state != "ACTIVE" else ""
        logger.info(
            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} attempt={attempt+1} {state_tag} "
            f"key=[{account_hash}] timeout=({connect_timeout:.1f}s, {read_timeout:.1f}s) remaining={remaining:.1f}s"
        )

        _record_dispatcher_active_model(model, purpose)

        keys_to_try = [curr_key] + [k for k in keys_pool if k != curr_key]

        for key_idx, active_key in enumerate(keys_to_try):
            active_account_hash = _get_account_hash(active_key)
            if len(keys_to_try) > 1 and key_idx > 0:
                if circuit_breaker.is_account_exhausted(active_account_hash)[0]:
                    continue

            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": active_key,
            }

            try:
                resp = requests.post(url, json=payload, headers=headers, timeout=(connect_timeout, read_timeout))
                resp.encoding = "utf-8"
                elapsed_ms = int((time.monotonic() - t_start) * 1000)

                if resp.status_code == 200:
                    data = resp.json()
                    if stop_checker and stop_checker():
                        raise GeminiCancelledError("Operation cancelled by user stop request.")
                    if response_validator is not None:
                        response_validator(data)
                    circuit_breaker.record_success(active_account_hash, model, purpose)
                    circuit_breaker.record_account_success(active_account_hash)
                    _record_dispatcher_success_model(model, purpose)
                    logger.info(
                        f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} key=[{active_account_hash}] "
                        f"SUCCESS status=200 elapsed={elapsed_ms}ms"
                    )
                    return data, model

                # Error handling
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
                status = resp.status_code
                retry_after = parse_retry_after(resp.headers.get("Retry-After"))

                logger.warning(
                    f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} key=[{active_account_hash}] "
                    f"FAILED status={status} elapsed={elapsed_ms}ms retry_after={retry_after}"
                )

                circuit_breaker.record_failure(
                    active_account_hash, model, purpose, status, f"HTTP_{status}", retry_after
                )

                if status == 429:
                    circuit_breaker.record_account_quota_exhausted(active_account_hash, retry_after)
                    # Check if another key is available in the pool
                    available_alts = [k for k in keys_to_try[key_idx + 1:] if not circuit_breaker.is_account_exhausted(_get_account_hash(k))[0]]
                    if available_alts:
                        logger.warning(
                            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model}: Key [{active_account_hash}] bị HTTP 429 Rate Limit. "
                            f"Lập tức chuyển sang Key [{_get_account_hash(available_alts[0])}] trong Multi-Key Pool ({len(available_alts)} keys khả dụng)..."
                        )
                        continue

                if status in (401, 403):
                    if len(keys_to_try) == 1:
                        raise GeminiAuthError(f"Gemini API key is invalid or lacks permission (HTTP {status}).")
                    else:
                        logger.warning(
                            f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model}: Key [{active_account_hash}] lỗi xác thực (HTTP {status}). Chuyển sang key khác..."
                        )
                        continue

                if status == 400:
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error", {}).get("message", "Bad request")
                    except Exception:
                        err_msg = resp.text[:200]
                    err_msg = mask_secret(err_msg)
                    logger.error(f"[GEMINI_DISPATCHER] HTTP 400 Bad Request: {err_msg}")
                    if any(token in err_msg.lower() for token in ("model", "not supported", "not found")):
                        last_exception = GeminiError(f"HTTP 400: {err_msg}")
                        break
                    raise GeminiRequestError(f"HTTP 400: {err_msg}")

                last_exception = GeminiError(f"HTTP {status} from {model}")

            except requests.Timeout as e:
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
                logger.warning(
                    f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} key=[{active_account_hash}] TIMEOUT after {elapsed_ms}ms"
                )
                circuit_breaker.record_failure(active_account_hash, model, purpose, None, "Timeout")
                last_exception = TimeoutError(mask_secret(str(e)))
                break

            except requests.ConnectionError as e:
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
                logger.warning(
                    f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} key=[{active_account_hash}] CONNECTION_ERROR after {elapsed_ms}ms"
                )
                circuit_breaker.record_failure(active_account_hash, model, purpose, None, "ConnectionError")
                last_exception = ConnectionError(mask_secret(str(e)))
                break

            except Exception as e:
                if isinstance(e, (GeminiAuthError, GeminiRequestError, GeminiDeadlineError, GeminiCancelledError)):
                    raise
                elapsed_ms = int((time.monotonic() - t_start) * 1000)
                logger.warning(
                    f"[GEMINI_DISPATCHER] job_id={job_id} purpose={purpose} model={model} key=[{active_account_hash}] ERROR: {type(e).__name__} after {elapsed_ms}ms"
                )
                circuit_breaker.record_failure(active_account_hash, model, purpose, None, type(e).__name__)
                last_exception = e
                break

    raise GeminiAllModelsFailedError(f"All candidate Gemini models failed for {purpose}: {mask_secret(str(last_exception))}") from last_exception


def get_gemini_pool_health(explicit_key: Optional[str] = None) -> Dict[str, Any]:
    """Return real-time health, cooldowns, and model circuit status across all configured Gemini API keys."""
    now = time.time()
    try:
        keys = get_gemini_api_keys(explicit_key)
    except Exception as exc:
        return {
            "status": "unconfigured",
            "error": mask_secret(str(exc)),
            "total_keys": 0,
            "active_keys": 0,
            "keys": [],
            "default_translation_models": DEFAULT_TRANSLATION_MODELS,
        }

    cb = GeminiCircuitBreaker()
    account_rows: Dict[str, Dict[str, Any]] = {}
    model_rows: Dict[str, List[Dict[str, Any]]] = {}

    try:
        if cb.db_path.is_file():
            with sqlite3.connect(str(cb.db_path), timeout=5.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT account_hash, quota_exhausted_until, last_error_type, consecutive_429, last_used FROM account_circuit"
                )
                for acc_h, q_until, err_t, c_429, l_used in cur.fetchall():
                    account_rows[str(acc_h)] = {
                        "quota_exhausted_until": float(q_until or 0.0),
                        "last_error_type": err_t,
                        "consecutive_429": int(c_429 or 0),
                        "last_used": float(l_used or 0.0),
                    }
                cur.execute(
                    "SELECT account_hash, model, purpose, state, cooldown_until, consecutive_failures, last_error_type, last_error_code, last_success "
                    "FROM model_circuit"
                )
                for acc_h, m_name, purp, st, cd_until, c_fail, err_t, err_c, l_succ in cur.fetchall():
                    rem = max(0.0, float(cd_until or 0.0) - now)
                    eff_state = st if rem > 0 else ("ACTIVE" if st == "COOLDOWN" else st)
                    model_rows.setdefault(str(acc_h), []).append({
                        "model": m_name,
                        "purpose": purp,
                        "state": eff_state,
                        "cooldown_remaining_sec": round(rem, 1),
                        "consecutive_failures": int(c_fail or 0),
                        "last_error_type": err_t,
                        "last_error_code": err_c,
                        "last_success": l_succ,
                    })
    except Exception as db_err:
        logger.debug("get_gemini_pool_health DB query warning: %s", db_err)

    key_items = []
    active_count = 0
    for idx, k in enumerate(keys, 1):
        acc_h = _get_account_hash(k)
        masked = f"{k[:6]}...{k[-4:]}" if len(k) > 12 else "AIza..."
        acc_info = account_rows.get(acc_h, {})
        q_until = float(acc_info.get("quota_exhausted_until", 0.0))
        cd_rem = max(0.0, q_until - now)
        is_exhausted = cd_rem > 0
        if not is_exhausted:
            active_count += 1
        key_items.append({
            "index": idx,
            "account_hash": acc_h,
            "masked_key": masked,
            "status": "COOLDOWN_429" if is_exhausted else "ACTIVE",
            "cooldown_remaining_sec": round(cd_rem, 1),
            "consecutive_429": int(acc_info.get("consecutive_429", 0)),
            "last_error_type": acc_info.get("last_error_type"),
            "last_used": acc_info.get("last_used") or None,
            "models": model_rows.get(acc_h, []),
        })

    return {
        "status": "healthy" if active_count > 0 else "degraded",
        "total_keys": len(keys),
        "active_keys": active_count,
        "keys": key_items,
        "default_translation_models": DEFAULT_TRANSLATION_MODELS,
    }
