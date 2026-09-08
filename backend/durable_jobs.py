"""V2-only durable intake. One consumer, protected by the bot singleton lock."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL, error TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key, payload):
        with self.connect() as db:
            cursor = db.execute('INSERT OR IGNORE INTO jobs(dedupe,payload,state,updated) VALUES(?,?,?,?)',
                                (key, json.dumps(payload, ensure_ascii=False), 'queued', time.time()))
            return cursor.lastrowid if cursor.rowcount else None

    def recover(self):
        # Invoke only after acquiring the process singleton, never on /stop.
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='queued',updated=? WHERE state='running'", (time.time(),))

    def claim(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), row['id']))
            return row['id'], json.loads(row['payload'])

    def checkpoint(self, job_id, payload):
        with self.connect() as db:
            db.execute("UPDATE jobs SET payload=?,updated=? WHERE id=? AND state='running'",
                       (json.dumps(payload, ensure_ascii=False), time.time(), job_id))

    def finish(self, job_id, state, error=None):
        if state not in {'completed', 'failed', 'cancelled'}:
            raise ValueError(state)
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=?,error=?,updated=? WHERE id=? AND state='running'",
                       (state, error, time.time(), job_id))

    def cancel(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET state='cancelled',updated=? WHERE state IN ('queued','running')", (time.time(),))

    def counts(self):
        with self.connect() as db:
            return {r['state']:r['n'] for r in db.execute('SELECT state,COUNT(*) n FROM jobs GROUP BY state')}
