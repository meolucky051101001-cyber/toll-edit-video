"""
cloud_job_store.py - Quản lý hàng đợi công việc Cloud GPU bằng SQLite (WAL mode).
Lưu trữ trạng thái độc lập, an toàn và hỗ trợ restart/resume.
"""

import json
import math
import datetime
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .cloud_config import DEFAULT_CONFIG


@dataclass
class CloudJob:
    id: int
    job_id: str
    video_path: str
    video_name: str
    gpu_type: str
    task_type: str
    state: str  # queued | creating_session | uploading | processing | downloading | completed | error | stopped
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

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class CloudJobStore:
    def __init__(self, db_path: Optional[str | Path] = None):
        self.db_path = Path(db_path or DEFAULT_CONFIG["db_path"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(str(self.db_path), timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _init_db(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
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
                    voice TEXT DEFAULT 'BV562_streaming'
                )
                """
            )
            # Tự động di trú bảng an toàn nếu thiếu cột voice
            existing_cols = {r[1] for r in db.execute("PRAGMA table_info(cloud_jobs)").fetchall()}
            if "voice" not in existing_cols:
                db.execute("ALTER TABLE cloud_jobs ADD COLUMN voice TEXT DEFAULT 'BV562_streaming'")

            # Bảng cài đặt hệ thống cloud (hạn mức CU, cấu hình động)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            # Billing survives history deletion and includes unsuccessful GPU sessions.
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_cu_ledger (
                job_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
                hourly_rate REAL NOT NULL DEFAULT 0, cu_actual REAL NOT NULL DEFAULT 0,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,
                owner_pid INTEGER, owner_created_at REAL
            )""")
            ledger_columns = {r[1] for r in db.execute("PRAGMA table_info(cloud_cu_ledger)")}
            if "cleanup_pending" not in ledger_columns:
                db.execute("ALTER TABLE cloud_cu_ledger ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0")
            for column, kind in (("owner_pid", "INTEGER"), ("owner_created_at", "REAL")):
                if column not in ledger_columns:
                    db.execute(f"ALTER TABLE cloud_cu_ledger ADD COLUMN {column} {kind}")
            db.execute("""INSERT OR IGNORE INTO cloud_cu_ledger
                (job_id, started_at, ended_at, cu_actual)
                SELECT job_id, COALESCE(completed_at, started_at, created_at),
                       COALESCE(completed_at, started_at, created_at), cu_actual
                FROM cloud_jobs WHERE cu_actual > 0""")

    def enqueue(
        self,
        video_path: str,
        video_name: Optional[str] = None,
        gpu_type: str = "T4",
        task_type: str = "full_pipeline",
        cu_estimated: float = 0.5,
        voice: str = "BV562_streaming",
    ) -> CloudJob:
        """Đưa job mới vào hàng đợi."""
        name = video_name or Path(video_path).name
        job_id = f"cloud-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        now = time.time()

        with self._connect() as db:
            cursor = db.execute(
                """
                INSERT INTO cloud_jobs (
                    job_id, video_path, video_name, gpu_type, task_type,
                    state, percent, step_name, cu_estimated, created_at, voice
                ) VALUES (?, ?, ?, ?, ?, 'queued', 0, 'Đang trong hàng đợi', ?, ?, ?)
                """,
                (job_id, str(video_path), name, gpu_type.upper(), task_type, cu_estimated, now, voice),
            )
            row_id = cursor.lastrowid

        return self.get_job_by_row_id(row_id)

    def get_job(self, job_id: str) -> Optional[CloudJob]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def get_active_job(self) -> Optional[CloudJob]:
        """Lấy job đang chạy (creating_session, uploading, processing, downloading)."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM cloud_jobs
                WHERE state IN ('creating_session', 'uploading', 'processing', 'downloading')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            return self._row_to_job(row) if row else None

    def get_job_by_row_id(self, row_id: int) -> Optional[CloudJob]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE id = ?", (row_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def claim_next(self) -> Optional[CloudJob]:
        """Lấy job kế tiếp ở trạng thái queued và chuyển sang creating_session."""
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM cloud_jobs WHERE state = 'queued' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            job_id = row["job_id"]
            db.execute(
                """
                UPDATE cloud_jobs
                SET state = 'creating_session', started_at = ?, percent = 5,
                    step_name = 'Đang khởi tạo máy ảo GPU Colab...'
                WHERE job_id = ?
                """,
                (now, job_id),
            )
            updated_row = db.execute("SELECT * FROM cloud_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(updated_row)

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
    ) -> bool:
        """Cập nhật tiến trình của job."""
        updates = []
        params = []

        if state is not None:
            updates.append("state = ?")
            params.append(state)
            if state in ("completed", "error", "stopped"):
                updates.append("completed_at = ?")
                params.append(time.time())

        if percent is not None:
            updates.append("percent = ?")
            params.append(percent)

        if step_name is not None:
            updates.append("step_name = ?")
            params.append(step_name)

        if session_name is not None:
            updates.append("session_name = ?")
            params.append(session_name)

        if cu_actual is not None:
            updates.append("cu_actual = ?")
            params.append(cu_actual)

        if result_path is not None:
            updates.append("result_path = ?")
            params.append(str(result_path))

        if error is not None:
            updates.append("error = ?")
            params.append(str(error))

        if not updates:
            return False

        params.append(job_id)
        sql = (f"UPDATE cloud_jobs SET {', '.join(updates)} WHERE job_id = ? "
               "AND state NOT IN ('completed', 'error', 'stopped')")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(sql, tuple(params)).rowcount > 0
            if changed and cu_actual is not None:
                now = time.time()
                db.execute("""INSERT INTO cloud_cu_ledger(job_id, started_at, ended_at, cu_actual)
                    VALUES (?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET
                    cu_actual = MAX(cu_actual, excluded.cu_actual)""",
                    (job_id, now, now, max(0.0, float(cu_actual))))
            return changed

    def is_cancelled(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return job is None or job.state in ("stopped", "error", "completed")

    def publish_if_active(self, job_id: str, result_path: str, publish) -> bool:
        """Serialize publication with cancellation, so acknowledged cancels cannot publish."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["state"] in ("stopped", "error", "completed"):
                return False
            publish()
            db.execute("""UPDATE cloud_jobs SET state='completed', percent=100,
                step_name='Hoàn thành', result_path=?, completed_at=? WHERE job_id=?""",
                (str(result_path), time.time(), job_id))
            return True

    def list_jobs(self, limit: int = 50) -> List[CloudJob]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM cloud_jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def cancel_job(self, job_id: str) -> bool:
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                """
                UPDATE cloud_jobs
                SET state = 'stopped', error = 'Người dùng đã huỷ công việc', completed_at = ?
                WHERE job_id = ? AND state NOT IN ('completed', 'error', 'stopped')
                """,
                (now, job_id),
            )
            return cur.rowcount > 0

    def delete_job(self, job_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("""DELETE FROM cloud_jobs WHERE job_id = ?
                AND state IN ('completed', 'error', 'stopped')
                AND NOT EXISTS (SELECT 1 FROM cloud_cu_ledger
                    WHERE job_id=? AND ended_at IS NULL)""", (job_id, job_id))
            return cur.rowcount > 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute("SELECT value FROM cloud_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        now = time.time()
        val_str = str(value)
        with self._connect() as db:
            db.execute(
                "INSERT INTO cloud_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, val_str, now),
            )

    def get_cu_budget_daily(self) -> float:
        val = self.get_setting("cu_budget_daily")
        if val is not None:
            try:
                return float(val)
            except ValueError:
                pass
        return float(DEFAULT_CONFIG.get("cu_budget_daily", 10.0))

    def set_cu_budget_daily(self, budget: float) -> None:
        if not math.isfinite(float(budget)) or round(float(budget), 2) <= 0:
            raise ValueError("Hạn mức CU phải là số hữu hạn lớn hơn 0.")
        self.set_setting("cu_budget_daily", round(float(budget), 2))

    @staticmethod
    def _spent_today(db, now: float) -> float:
        day = datetime.datetime.fromtimestamp(now)
        midnight = day.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = 0.0
        for row in db.execute("SELECT * FROM cloud_cu_ledger"):
            if row["hourly_rate"] > 0:
                end = row["ended_at"] if row["ended_at"] is not None else now
                total += max(0.0, min(end, now) - max(row["started_at"], midnight)) * row["hourly_rate"] / 3600
            elif row["ended_at"] is not None and midnight <= row["ended_at"] <= now:
                total += row["cu_actual"]
        return total

    def start_billing(self, job_id: str, hourly_rate: float) -> None:
        """Persist allocation intent before the provider call; unknown cleanup blocks new jobs."""
        if not math.isfinite(hourly_rate) or hourly_rate <= 0:
            raise ValueError("Invalid GPU CU rate")
        budget = self.get_cu_budget_daily()
        import psutil
        owner_pid = os.getpid()
        owner_created = psutil.Process(owner_pid).create_time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row["state"] in ("completed", "error", "stopped"):
                raise RuntimeError("Cloud job đã kết thúc hoặc bị hủy.")
            if db.execute("SELECT 1 FROM cloud_cu_ledger WHERE ended_at IS NULL LIMIT 1").fetchone():
                raise RuntimeError("Còn session Cloud chưa xác nhận dừng; chưa thể tạo máy mới.")
            now = time.time()
            if self._spent_today(db, now) >= budget:
                raise RuntimeError("Đã chạm hạn mức CU trong ngày.")
            db.execute("""INSERT INTO cloud_cu_ledger
                (job_id, started_at, hourly_rate, owner_pid, owner_created_at) VALUES (?, ?, ?, ?, ?)""",
                       (job_id, now, hourly_rate, owner_pid, owner_created))

    def finish_billing(self, job_id: str) -> float:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            db.execute("""UPDATE cloud_cu_ledger SET ended_at=?,
                cu_actual=MAX(0, ?-started_at)*hourly_rate/3600
                WHERE job_id=? AND ended_at IS NULL""", (now, now, job_id))
            row = db.execute("SELECT cu_actual FROM cloud_cu_ledger WHERE job_id=?", (job_id,)).fetchone()
            amount = float(row["cu_actual"]) if row else 0.0
            db.execute("UPDATE cloud_jobs SET cu_actual=? WHERE job_id=?", (amount, job_id))
            return amount

    def acknowledge_all_sessions_stopped(self) -> None:
        """Settle failed cleanups after verification; in-flight allocation settles itself."""
        self.recover()
        with self._connect() as db:
            jobs = [r["job_id"] for r in db.execute("""SELECT job_id FROM cloud_cu_ledger
                WHERE ended_at IS NULL AND cleanup_pending=1""")]
        for job_id in jobs:
            self.finish_billing(job_id)

    def note_cleanup_failure(self, job_id: str, message: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE cloud_cu_ledger SET cleanup_pending=1 WHERE job_id=?", (job_id,))
            db.execute("UPDATE cloud_jobs SET error=? WHERE job_id=?",
                       ("Chưa xác nhận dừng GPU: " + str(message)[:500], job_id))

    def cancel_all(self) -> None:
        with self._connect() as db:
            db.execute("""UPDATE cloud_jobs SET state='stopped', completed_at=?,
                error='Người dùng đã hủy công việc' WHERE state NOT IN ('completed','error','stopped')""",
                (time.time(),))

    def get_cu_spent_today(self) -> float:
        with self._connect() as db:
            return round(self._spent_today(db, time.time()), 3)

    def get_cu_budget_info(self) -> Dict[str, Any]:
        budget = self.get_cu_budget_daily()
        spent = self.get_cu_spent_today()
        remaining = max(0.0, round(budget - spent, 3))
        percent = min(100.0, round((spent / budget) * 100, 1)) if budget > 0 else 0.0
        return {
            "budget_daily": budget,
            "spent_today": spent,
            "remaining_today": remaining,
            "percent_used": percent,
            "is_exceeded": spent >= budget,
        }

    def recover(self):
        """Recover only allocations whose owning process is verifiably gone."""
        now = time.time()
        with self._connect() as db:
            pending = db.execute("SELECT * FROM cloud_cu_ledger WHERE ended_at IS NULL").fetchall()
        for row in pending:
            if self._billing_owner_alive(row):
                continue
            with self._connect() as db:
                db.execute("UPDATE cloud_cu_ledger SET cleanup_pending=1 WHERE job_id=? AND ended_at IS NULL",
                           (row["job_id"],))
                db.execute("""UPDATE cloud_jobs SET state='error', completed_at=?,
                    error='Tiến trình Cloud đã dừng; cần xác nhận thu hồi GPU'
                    WHERE job_id=? AND state NOT IN ('completed','error','stopped')""",
                    (now, row["job_id"]))

    @staticmethod
    def _billing_owner_alive(row) -> bool:
        # Missing identity is unknown, never proof that an allocation has finished.
        if not row["owner_pid"] or row["owner_created_at"] is None:
            return True
        import psutil
        try:
            process = psutil.Process(row["owner_pid"])
            return abs(process.create_time() - row["owner_created_at"]) < 0.001
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError):
            return True

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> CloudJob:
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
            voice=row["voice"] if "voice" in row.keys() else "BV562_streaming",
        )
