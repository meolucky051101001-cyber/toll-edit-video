"""V2-only durable intake. One consumer, protected by the bot singleton lock."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute(
                'CREATE TABLE IF NOT EXISTS jobs ('
                'id INTEGER PRIMARY KEY, '
                'dedupe TEXT UNIQUE NOT NULL, '
                'payload TEXT NOT NULL, '
                'state TEXT NOT NULL, '
                'updated REAL NOT NULL, '
                'error TEXT, '
                'source_fingerprint TEXT, '
                'checkpoint_stage TEXT, '
                'retry_count INTEGER NOT NULL DEFAULT 0'
                ')'
            )
            # Safe schema migration if table already exists
            existing_cols = {r[1] for r in db.execute("PRAGMA table_info(jobs)").fetchall()}
            if 'source_fingerprint' not in existing_cols:
                db.execute('ALTER TABLE jobs ADD COLUMN source_fingerprint TEXT')
            if 'checkpoint_stage' not in existing_cols:
                db.execute('ALTER TABLE jobs ADD COLUMN checkpoint_stage TEXT')
            if 'retry_count' not in existing_cols:
                db.execute('ALTER TABLE jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key: str, payload: dict, source_fingerprint: Optional[str] = None) -> Optional[int]:
        """Enqueue new job by dedupe key; return row id or None if duplicate exists."""
        with self.connect() as db:
            cursor = db.execute(
                'INSERT OR IGNORE INTO jobs(dedupe, payload, state, updated, source_fingerprint, retry_count) '
                'VALUES(?, ?, ?, ?, ?, 0)',
                (key, json.dumps(payload, ensure_ascii=False), 'queued', time.time(), source_fingerprint)
            )
            return cursor.lastrowid if cursor.rowcount else None

    def retry(self, job_id: int, payload_override: Optional[dict] = None) -> int:
        """Retry an existing job with the EXACT same job_id (no new row created)."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(f"Job {job_id} not found")
            payload = json.dumps(payload_override, ensure_ascii=False) if payload_override is not None else row['payload']
            db.execute(
                "UPDATE jobs SET state='queued', payload=?, retry_count=retry_count+1, error=NULL, updated=? WHERE id=?",
                (payload, time.time(), job_id)
            )
            return job_id

    def retry_by_dedupe(self, dedupe: str, payload_override: Optional[dict] = None) -> Optional[int]:
        """Retry existing job identified by dedupe key using the same job id."""
        with self.connect() as db:
            row = db.execute("SELECT id FROM jobs WHERE dedupe=?", (dedupe,)).fetchone()
            if row is None:
                return None
            return self.retry(row['id'], payload_override=payload_override)

    def recover(self):
        """Invoke on bot startup: mark interrupted running jobs as queued for resume."""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='queued', error='interrupted_restart', updated=? WHERE state='running'",
                (time.time(),)
            )

    def claim(self) -> Optional[Tuple[int, dict]]:
        """Claim next queued job. Preserves checkpoint_stage and retry_count in returned payload."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running', updated=? WHERE id=?", (time.time(), row['id']))
            payload = json.loads(row['payload'])
            if row['checkpoint_stage']:
                payload['_checkpoint_stage'] = row['checkpoint_stage']
            payload['_retry_count'] = row['retry_count'] or 0
            if row['source_fingerprint']:
                payload['_source_fingerprint'] = row['source_fingerprint']
            return row['id'], payload

    def checkpoint(self, job_id: int, payload: dict, stage: Optional[str] = None):
        """Save job state checkpoint with optional pipeline stage for seamless resume."""
        with self.connect() as db:
            if stage:
                db.execute(
                    "UPDATE jobs SET payload=?, checkpoint_stage=?, updated=? WHERE id=? AND state='running'",
                    (json.dumps(payload, ensure_ascii=False), str(stage), time.time(), job_id)
                )
            else:
                db.execute(
                    "UPDATE jobs SET payload=?, updated=? WHERE id=? AND state='running'",
                    (json.dumps(payload, ensure_ascii=False), time.time(), job_id)
                )

    def finish(self, job_id: int, state: str, error: Optional[str] = None):
        if state not in {'completed', 'failed', 'cancelled'}:
            raise ValueError(state)
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state=?, error=?, updated=? WHERE id=? AND state='running'",
                (state, error, time.time(), job_id)
            )

    def cancel(self, job_id: Optional[int] = None):
        """Cancel a specific job or all active jobs."""
        with self.connect() as db:
            if job_id is not None:
                db.execute(
                    "UPDATE jobs SET state='cancelled', error='cancelled_by_user', updated=? "
                    "WHERE id=? AND state IN ('queued', 'running')",
                    (time.time(), job_id)
                )
            else:
                db.execute(
                    "UPDATE jobs SET state='cancelled', error='cancelled_all', updated=? "
                    "WHERE state IN ('queued', 'running')",
                    (time.time(),)
                )

    def get_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            return {
                "id": row["id"],
                "dedupe": row["dedupe"],
                "source_fingerprint": row["source_fingerprint"],
                "payload": json.loads(row["payload"]),
                "state": row["state"],
                "checkpoint_stage": row["checkpoint_stage"],
                "retry_count": row["retry_count"],
                "updated": row["updated"],
                "error": row["error"],
            }

    def counts(self):
        with self.connect() as db:
            return {r['state']: r['n'] for r in db.execute('SELECT state, COUNT(*) n FROM jobs GROUP BY state')}
