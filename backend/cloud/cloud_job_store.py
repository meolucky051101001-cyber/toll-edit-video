"""Fenced cloud claims and recoverable no-clobber publication in SQLite.

Durable publication intent wins against cancellation. An interrupted intent is
reconciled from its staging inode, hash and receipt, never by rerendering.
"""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import json
import math
import datetime
import os
from pathlib import Path
import sqlite3
import time
import uuid
from .cloud_config import DEFAULT_CONFIG
from .cloud_contracts import CloudCapabilityError, CloudOwnershipError, CloudVerificationError, validate_request
from .cloud_artifacts import sha256_file

RUNNING = ("creating_session", "uploading", "processing", "downloading", "validating")
TERMINAL = ("completed", "error", "stopped")
TRANSITIONS = {"creating_session": {"creating_session", "uploading"},
               "uploading": {"uploading", "processing"}, "processing": {"processing", "downloading"},
               "downloading": {"downloading", "validating"}, "validating": {"validating"}}


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
    session_name: str | None
    cu_estimated: float
    cu_actual: float
    created_at: float
    started_at: float | None
    completed_at: float | None
    result_path: str | None
    error: str | None
    voice: str = "BV562_streaming"
    attempt_id: str | None = None
    owner_id: str | None = None
    revision: int = 0
    cancel_requested_at: float | None = None
    external_job_id: str | None = None
    source_sha256: str | None = None
    publication_json: str | None = None
    receipt_json: str | None = None
    session_cleanup: str = "not_created"
    run_started_at: float | None = None

    def to_dict(self):
        result = asdict(self)
        result.pop("publication_json", None)
        result.pop("receipt_json", None)
        # Publication and resource cleanup are separate durable facts. Consumers
        # must not present a fully finished job while its session still holds
        # the admission slot. Keep the stored state available for diagnostics.
        result['publication_state'] = self.state
        result['output_committed'] = bool(self.state == 'completed' and self.publication_json
                                          and self.receipt_json and self.result_path)
        if self.state == 'completed' and self.session_cleanup != 'confirmed':
            result['state'] = 'stop_incomplete'
        return result


class CloudJobStore:
    def __init__(self, db_path=None):
        self.db_path = Path(db_path or DEFAULT_CONFIG["db_path"])
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_legacy_schema()
        self._init_db()

    def _backup_legacy_schema(self):
        if not self.db_path.is_file() or not self.db_path.stat().st_size:
            return
        with closing(sqlite3.connect(str(self.db_path), timeout=20)) as source:
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_schema'").fetchone()
            version = source.execute('SELECT version FROM cloud_schema').fetchone() if exists else None
            if version and version[0] > 2:
                raise CloudOwnershipError('cloud_schema_newer_than_application')
            if version and version[0] == 2:
                return
            backup = self.db_path.with_name(self.db_path.name + '.pre-v2-' + uuid.uuid4().hex + '.sqlite')
            # SQLite backup includes WAL contents consistently; never copy only
            # the main database file while writers may still have a WAL.
            with closing(sqlite3.connect(str(backup))) as target:
                source.backup(target)

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
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT UNIQUE NOT NULL,
                video_path TEXT NOT NULL, video_name TEXT NOT NULL, gpu_type TEXT NOT NULL,
                task_type TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                percent INTEGER DEFAULT 0, step_name TEXT DEFAULT '', session_name TEXT,
                cu_estimated REAL DEFAULT 0, cu_actual REAL DEFAULT 0,
                created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                result_path TEXT, error TEXT)""")
            columns = {r[1] for r in db.execute("PRAGMA table_info(cloud_jobs)")}
            additions = {"voice": "TEXT DEFAULT 'BV562_streaming'", "attempt_id": "TEXT",
                         "owner_id": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 0",
                         "cancel_requested_at": "REAL", "external_job_id": "TEXT",
                         "source_sha256": "TEXT", "publication_json": "TEXT", "receipt_json": "TEXT",
                         "session_cleanup": "TEXT NOT NULL DEFAULT 'not_created'", "run_started_at": "REAL"}
            for name, kind in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE cloud_jobs ADD COLUMN {name} {kind}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS cloud_external_id ON cloud_jobs(external_job_id) WHERE external_job_id IS NOT NULL")
            db.execute("""UPDATE cloud_jobs SET state='recovery_required',
                       error='legacy_active_job_requires_reconciliation', revision=revision+1,
                       session_cleanup='unconfirmed'
                       WHERE attempt_id IS NULL AND state NOT IN ('queued','completed','error','stopped','recovery_required')""")
            db.execute("CREATE TABLE IF NOT EXISTS cloud_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)")
            # Billing is independent of mutable job history and remains open
            # until scoped session cleanup is confirmed, including failures.
            db.execute("""CREATE TABLE IF NOT EXISTS cloud_cu_ledger (
                job_id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL,
                hourly_rate REAL NOT NULL DEFAULT 0, cu_actual REAL NOT NULL DEFAULT 0,
                cleanup_pending INTEGER NOT NULL DEFAULT 0,
                owner_pid INTEGER, owner_created_at REAL)""")
            ledger_columns = {r[1] for r in db.execute("PRAGMA table_info(cloud_cu_ledger)")}
            for name, kind in (("cleanup_pending", "INTEGER NOT NULL DEFAULT 0"),
                               ("owner_pid", "INTEGER"), ("owner_created_at", "REAL")):
                if name not in ledger_columns:
                    db.execute(f"ALTER TABLE cloud_cu_ledger ADD COLUMN {name} {kind}")
            db.execute("""INSERT OR IGNORE INTO cloud_cu_ledger
                (job_id, started_at, ended_at, cu_actual)
                SELECT job_id, COALESCE(completed_at, started_at, created_at),
                       COALESCE(completed_at, started_at, created_at), cu_actual
                FROM cloud_jobs WHERE cu_actual > 0""")
            db.execute("CREATE TABLE IF NOT EXISTS cloud_schema (version INTEGER NOT NULL)")
            version = db.execute("SELECT version FROM cloud_schema").fetchone()
            if not version:
                db.execute("INSERT INTO cloud_schema VALUES (2)")
            elif version[0] != 2:
                db.execute("UPDATE cloud_schema SET version=2")

    @staticmethod
    def _row_to_job(row):
        return CloudJob(**{field: row[field] for field in CloudJob.__dataclass_fields__}) if row else None

    def get_job(self, job_id):
        with self._connect() as db:
            return self._row_to_job(db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone())

    def get_job_by_row_id(self, row_id):
        with self._connect() as db:
            return self._row_to_job(db.execute("SELECT * FROM cloud_jobs WHERE id=?", (row_id,)).fetchone())

    def list_jobs(self, limit=50):
        with self._connect() as db:
            return [self._row_to_job(r) for r in db.execute("SELECT * FROM cloud_jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def get_active_job(self):
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE state NOT IN ('queued','completed','error','stopped') OR session_cleanup='unconfirmed' ORDER BY id LIMIT 1").fetchone()
            return self._row_to_job(row)

    def enqueue(self, video_path, video_name=None, gpu_type="T4", task_type="full_pipeline",
                cu_estimated=0.5, voice="BV562_streaming", external_job_id=None, claim_owner=None):
        validate_request(str(video_path), gpu_type, task_type, voice)
        name = video_name or Path(video_path).name
        if not name or Path(name).name != name or any(c in name for c in ('/', '\\', '\0')) or name in ('.', '..'):
            raise ValueError("invalid_cloud_video_name")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE external_job_id=?", (external_job_id,)).fetchone() if external_job_id else None
            if row:
                if (row['video_path'], row['gpu_type'], row['task_type'], row['voice']) != (str(video_path), gpu_type.upper(), task_type, voice):
                    raise ValueError("cloud_idempotency_conflict")
                if claim_owner and row['state'] == 'queued':
                    self._claim(db, row['job_id'], claim_owner)
                    row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (row['job_id'],)).fetchone()
                return self._row_to_job(row)
            job_id = "cloud-" + uuid.uuid4().hex
            db.execute("""INSERT INTO cloud_jobs (job_id,video_path,video_name,gpu_type,task_type,voice,
                       cu_estimated,created_at,external_job_id) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (job_id, str(video_path), name, gpu_type.upper(), task_type, voice, cu_estimated, time.time(), external_job_id))
            if claim_owner:
                self._claim(db, job_id, claim_owner)
            return self._row_to_job(db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _claim(db, job_id, owner_id):
        if db.execute("SELECT 1 FROM cloud_jobs WHERE state NOT IN ('queued','completed','error','stopped') OR session_cleanup='unconfirmed' LIMIT 1").fetchone():
            return None
        if db.execute("SELECT 1 FROM cloud_cu_ledger WHERE ended_at IS NULL LIMIT 1").fetchone():
            return None
        token = uuid.uuid4().hex
        cur = db.execute("""UPDATE cloud_jobs SET state='creating_session', attempt_id=?,owner_id=?,
                         started_at=?,revision=revision+1,percent=5,step_name='Claimed'
                         WHERE job_id=? AND state='queued' AND attempt_id IS NULL""",
                         (token, owner_id, time.time(), job_id))
        return token if cur.rowcount else None

    def claim_job(self, job_id, owner_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._claim(db, job_id, owner_id):
                return None
            return self._row_to_job(db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone())

    def claim_next(self, owner_id=None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT job_id FROM cloud_jobs WHERE state='queued' AND external_job_id IS NULL ORDER BY id LIMIT 1").fetchone()
            if not row or not self._claim(db, row['job_id'], owner_id or uuid.uuid4().hex):
                return None
            return self._row_to_job(db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (row['job_id'],)).fetchone())

    def assert_owned(self, job_id, attempt_id, owner_id):
        job = self.get_job(job_id)
        if not job or not attempt_id or (job.attempt_id, job.owner_id) != (attempt_id, owner_id):
            raise CloudOwnershipError("stale_cloud_attempt")
        return job

    def start_attempt(self, job_id, attempt_id, owner_id):
        with self._connect() as db:
            return bool(db.execute("""UPDATE cloud_jobs SET run_started_at=?,revision=revision+1
                WHERE job_id=? AND attempt_id=? AND owner_id=? AND run_started_at IS NULL
                AND state IN ('creating_session','cancel_requested','stopping')""",
                (time.time(), job_id, attempt_id, owner_id)).rowcount)

    def update_progress(self, job_id, *, attempt_id=None, owner_id=None, expected_revision=None,
                        state=None, percent=None, step_name=None, session_name=None, source_sha256=None, **forbidden):
        if forbidden or not attempt_id or not owner_id:
            return False
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if (not row or row['attempt_id'] != attempt_id or row['owner_id'] != owner_id
                    or row['cancel_requested_at'] is not None or row['state'] not in RUNNING
                    or (expected_revision is not None and row['revision'] != expected_revision)
                    or (state and state not in TRANSITIONS[row['state']])):
                return False
            values = {k: v for k, v in dict(state=state, percent=percent, step_name=step_name,
                       session_name=session_name, source_sha256=source_sha256).items() if v is not None}
            if session_name:
                if row['session_name'] and row['session_name'] != session_name:
                    return False
                values['session_cleanup'] = 'unconfirmed'
            values['revision'] = row['revision'] + 1
            db.execute("UPDATE cloud_jobs SET " + ','.join(k+'=?' for k in values) + " WHERE job_id=?", (*values.values(), job_id))
            return True

    def request_cancel(self, job_id):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return {"accepted": False, "state": "missing", "job_id": job_id}
            if row['state'] in TERMINAL or row['publication_json']:
                return {"accepted": False, "state": row['state'], "job_id": job_id}
            state = 'stopped' if row['state'] == 'queued' else 'cancel_requested'
            if row['cancel_requested_at'] is not None:
                state = row['state']
            db.execute("""UPDATE cloud_jobs SET cancel_requested_at=COALESCE(cancel_requested_at,?),
                         state=?,revision=revision+1,completed_at=CASE WHEN ?='stopped' THEN ? ELSE completed_at END
                         WHERE job_id=?""", (time.time(), state, state, time.time(), job_id))
            return {"accepted": True, "state": state, "job_id": job_id}

    def cancel_job(self, job_id):
        return self.request_cancel(job_id)['accepted']

    def begin_stopping(self, job_id, attempt_id, owner_id):
        with self._connect() as db:
            return bool(db.execute("""UPDATE cloud_jobs SET state='stopping',revision=revision+1
                    WHERE job_id=? AND attempt_id=? AND owner_id=? AND cancel_requested_at IS NOT NULL
                    AND publication_json IS NULL AND state NOT IN ('completed','stopped')""", (job_id, attempt_id, owner_id)).rowcount)

    def finish_attempt(self, job_id, attempt_id, owner_id, *, error=None, cleanup_ok=True):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or (row['attempt_id'], row['owner_id']) != (attempt_id, owner_id):
                return False
            cleanup = 'confirmed' if cleanup_ok else 'unconfirmed'
            if row['state'] == 'completed':
                db.execute("UPDATE cloud_jobs SET session_cleanup=?,revision=revision+1 WHERE job_id=?", (cleanup, job_id))
                return True
            if row['state'] in ('error', 'stopped'):
                return False
            state = ('recovery_required' if row['publication_json'] else 'stop_incomplete' if not cleanup_ok
                     else 'stopped' if row['cancel_requested_at'] is not None else 'error')
            db.execute("""UPDATE cloud_jobs SET state=?,error=?,session_cleanup=?,completed_at=?,
                       revision=revision+1 WHERE job_id=?""", (state, error, cleanup, time.time(), job_id))
            return True

    def publish(self, job_id, attempt_id, owner_id, stage_path, output_path, verification, cu_actual=0, crash_hook=None):
        stage, output = Path(stage_path).resolve(), Path(output_path).resolve()
        if stage.stat().st_dev != output.parent.stat().st_dev:
            raise CloudVerificationError("cloud_publication_cross_volume")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or (row['attempt_id'], row['owner_id']) != (attempt_id, owner_id):
                raise CloudOwnershipError("stale_cloud_attempt")
            if row['state'] == 'completed':
                return self._receipt(row)
            if row['state'] != 'validating' or row['cancel_requested_at'] is not None:
                raise CloudOwnershipError("cloud_publication_not_allowed")
            receipt_path = Path(str(output) + '.cloud-receipt.json')
            if output.exists() or receipt_path.exists():
                raise CloudVerificationError("output_conflict")
            if stage.is_symlink() or sha256_file(stage) != verification['sha256']:
                raise CloudVerificationError("cloud_staging_hash_mismatch")
            intent = {"version": 1, "job_id": job_id, "attempt_id": attempt_id,
                      "source_sha256": row['source_sha256'], "output_path": str(output),
                      "stage_path": str(stage), "receipt_path": str(receipt_path),
                      "verification": verification, "cu_actual": cu_actual}
            db.execute("UPDATE cloud_jobs SET state='publishing',publication_json=?,revision=revision+1 WHERE job_id=?",
                       (json.dumps(intent, sort_keys=True), job_id))
        if crash_hook:
            crash_hook('after_intent')
        return self.recover_publication(job_id, crash_hook=crash_hook)

    @staticmethod
    def _receipt(row):
        if not row['receipt_json']:
            return None
        receipt = json.loads(row['receipt_json'])
        intent = json.loads(row['publication_json'] or '{}')
        if (receipt.get('job_id'), receipt.get('attempt_id'), receipt.get('source_sha256'), receipt.get('result_path')) != (
                row['job_id'], row['attempt_id'], row['source_sha256'], row['result_path']):
            return None
        if receipt.get('result_path') != intent.get('output_path') or receipt.get('sha256') != intent.get('verification', {}).get('sha256'):
            return None
        path = Path(receipt['result_path'])
        sidecar = Path(str(path) + '.cloud-receipt.json')
        if not path.is_absolute() or not path.is_file() or path.is_symlink() or not sidecar.is_file() or sidecar.is_symlink():
            return None
        if sha256_file(path) != receipt['sha256'] or json.loads(sidecar.read_text(encoding='utf-8')) != receipt:
            return None
        return receipt

    def get_receipt(self, job_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or row['state'] != 'completed' or row['session_cleanup'] != 'confirmed':
                return None
            try:
                return self._receipt(row)
            except (OSError, ValueError, KeyError):
                return None

    def recover_publication(self, job_id, crash_hook=None):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cloud_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row or not row['publication_json']:
                return None
            if row['state'] == 'completed':
                return self._receipt(row)
            intent = json.loads(row['publication_json'])
            stage, output = Path(intent['stage_path']), Path(intent['output_path'])
            try:
                expected_stage_dir = output.parent / '.cloud-staging' / row['job_id'] / row['attempt_id']
                if (not stage.is_absolute() or not output.is_absolute()
                        or stage.parent.resolve() != expected_stage_dir.resolve()
                        or intent.get('job_id') != row['job_id'] or intent.get('attempt_id') != row['attempt_id']
                        or intent.get('source_sha256') != row['source_sha256']
                        or Path(intent['receipt_path']) != Path(str(output) + '.cloud-receipt.json')):
                    raise CloudVerificationError('cloud_publication_identity_mismatch')
                if not stage.is_file() or stage.is_symlink() or sha256_file(stage) != intent['verification']['sha256']:
                    raise CloudVerificationError('cloud_publication_stage_changed')
                if output.exists():
                    if output.is_symlink() or not os.path.samefile(stage, output):
                        raise CloudVerificationError('output_conflict')
                else:
                    # Atomic no-clobber installation on the same volume. The
                    # staging hardlink remains as crash recovery ownership proof.
                    os.link(stage, output)
                if crash_hook:
                    crash_hook('after_rename')
                receipt = {"version": 1, "job_id": job_id, "attempt_id": row['attempt_id'],
                           "source_sha256": row['source_sha256'], "result_path": str(output),
                           **intent['verification']}
                sidecar = Path(intent['receipt_path'])
                encoded = json.dumps(receipt, sort_keys=True)
                if sidecar.exists():
                    if json.loads(sidecar.read_text(encoding='utf-8')) != receipt:
                        raise CloudVerificationError('cloud_receipt_conflict')
                else:
                    receipt_stage = stage.with_suffix('.receipt')
                    with open(receipt_stage, 'w', encoding='utf-8') as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(receipt_stage, sidecar)
                if crash_hook:
                    crash_hook('after_receipt')
                db.execute("""UPDATE cloud_jobs SET state='completed',percent=100,result_path=?,
                           receipt_json=?,cu_actual=?,completed_at=?,revision=revision+1 WHERE job_id=?""",
                           (str(output), encoded, intent['cu_actual'], time.time(), job_id))
                return receipt
            except (OSError, ValueError, KeyError) as exc:
                db.execute("UPDATE cloud_jobs SET state='recovery_required',error=?,revision=revision+1 WHERE job_id=?",
                           (str(exc) if isinstance(exc, CloudVerificationError) else 'cloud_publication_recovery_required', job_id))
                return None

    def recover(self):
        # No age-based stealing of an active worker. Only committed publication
        # intents are recoverable automatically; other attempts remain held.
        with self._connect() as db:
            ids = [r[0] for r in db.execute("SELECT job_id FROM cloud_jobs WHERE publication_json IS NOT NULL AND state IN ('publishing','recovery_required')")]
        for job_id in ids:
            self.recover_publication(job_id)
        with self._connect() as db:
            pending = db.execute("SELECT * FROM cloud_cu_ledger WHERE ended_at IS NULL").fetchall()
        for row in pending:
            if self._billing_owner_alive(row):
                continue
            with self._connect() as db:
                db.execute("UPDATE cloud_cu_ledger SET cleanup_pending=1 WHERE job_id=? AND ended_at IS NULL", (row['job_id'],))
                db.execute("""UPDATE cloud_jobs SET state=CASE WHEN publication_json IS NOT NULL
                    THEN 'recovery_required' ELSE 'stop_incomplete' END,
                    session_cleanup='unconfirmed', error='cloud_billing_owner_gone_cleanup_unconfirmed', revision=revision+1
                    WHERE job_id=? AND session_cleanup!='confirmed'""", (row['job_id'],))

    def delete_job(self, job_id):
        with self._connect() as db:
            return bool(db.execute("""DELETE FROM cloud_jobs WHERE job_id=?
                AND state IN ('completed','error','stopped') AND session_cleanup IN ('confirmed','not_created')
                AND NOT EXISTS (SELECT 1 FROM cloud_cu_ledger WHERE job_id=? AND ended_at IS NULL)""", (job_id, job_id)).rowcount)

    def get_setting(self, key, default=None):
        with self._connect() as db:
            row = db.execute("SELECT value FROM cloud_settings WHERE key=?", (key,)).fetchone()
            return row['value'] if row else default

    def set_setting(self, key, value):
        with self._connect() as db:
            db.execute("INSERT INTO cloud_settings VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, str(value), time.time()))

    def get_cu_budget_daily(self):
        return float(self.get_setting('cu_budget_daily', DEFAULT_CONFIG.get('cu_budget_daily', 10)))

    def set_cu_budget_daily(self, budget):
        if not math.isfinite(float(budget)) or round(float(budget), 2) <= 0:
            raise ValueError('Hạn mức CU phải là số hữu hạn lớn hơn 0.')
        self.set_setting('cu_budget_daily', round(float(budget), 2))

    @staticmethod
    def _spent_today(db, now):
        midnight = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        total = 0.0
        for row in db.execute('SELECT * FROM cloud_cu_ledger'):
            if row['hourly_rate'] > 0:
                end = row['ended_at'] if row['ended_at'] is not None else now
                total += max(0.0, min(end, now) - max(row['started_at'], midnight)) * row['hourly_rate'] / 3600
            elif row['ended_at'] is not None and midnight <= row['ended_at'] <= now:
                total += row['cu_actual']
        return total

    def start_billing(self, job_id, hourly_rate, *, attempt_id=None, owner_id=None):
        """Reserve budget before provisioning; an unknown allocation keeps admission closed."""
        if not math.isfinite(hourly_rate) or hourly_rate <= 0:
            raise ValueError('Invalid GPU CU rate')
        budget = self.get_cu_budget_daily()
        import psutil
        pid = os.getpid()
        created = psutil.Process(pid).create_time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM cloud_jobs WHERE job_id=?', (job_id,)).fetchone()
            if (not row or row['state'] in TERMINAL or row['cancel_requested_at'] is not None
                    or not attempt_id or (row['attempt_id'], row['owner_id']) != (attempt_id, owner_id)):
                raise CloudOwnershipError('cloud_billing_attempt_not_owned')
            if db.execute('SELECT 1 FROM cloud_cu_ledger WHERE ended_at IS NULL LIMIT 1').fetchone():
                raise CloudCapabilityError('Còn session Cloud chưa xác nhận dừng; chưa thể tạo máy mới.')
            now = time.time()
            if self._spent_today(db, now) >= budget:
                raise CloudCapabilityError('cloud_daily_budget_exceeded')
            db.execute('''INSERT INTO cloud_cu_ledger
                (job_id, started_at, hourly_rate, owner_pid, owner_created_at) VALUES (?,?,?,?,?)''',
                (job_id, now, hourly_rate, pid, created))

    def finish_billing(self, job_id, *, attempt_id=None, owner_id=None):
        self.assert_owned(job_id, attempt_id, owner_id)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute('''UPDATE cloud_cu_ledger SET ended_at=?,cleanup_pending=0,
                cu_actual=MAX(0,?-started_at)*hourly_rate/3600
                WHERE job_id=? AND ended_at IS NULL''', (now, now, job_id))
            row = db.execute('SELECT cu_actual FROM cloud_cu_ledger WHERE job_id=?', (job_id,)).fetchone()
            amount = float(row['cu_actual']) if row else 0.0
            db.execute('UPDATE cloud_jobs SET cu_actual=? WHERE job_id=?', (amount, job_id))
            db.execute("""UPDATE cloud_cu_ledger SET cleanup_pending=0
                WHERE job_id=? AND ended_at IS NOT NULL""", (job_id,))
            return amount

    def note_cleanup_failure(self, job_id, message, *, attempt_id=None, owner_id=None):
        self.assert_owned(job_id, attempt_id, owner_id)
        with self._connect() as db:
            db.execute('UPDATE cloud_cu_ledger SET cleanup_pending=1 WHERE job_id=? AND ended_at IS NULL', (job_id,))
            db.execute('UPDATE cloud_jobs SET error=? WHERE job_id=?', ('cloud_cleanup_unconfirmed: ' + str(message)[:500], job_id))

    def acknowledge_all_sessions_stopped(self):
        # Account-wide inventory cannot prove scoped attempt/secret cleanup.
        raise CloudOwnershipError('cloud_global_cleanup_cannot_release_owned_attempts')

    def cancel_all(self):
        return [self.request_cancel(job.job_id) for job in self.list_jobs(10000) if job.state not in TERMINAL]

    @staticmethod
    def _billing_owner_alive(row):
        if not row['owner_pid'] or row['owner_created_at'] is None:
            return True
        import psutil
        try:
            return abs(psutil.Process(row['owner_pid']).create_time() - row['owner_created_at']) < 0.001
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError):
            return True

    def get_cu_spent_today(self):
        with self._connect() as db:
            return round(self._spent_today(db, time.time()), 3)

    def get_cu_budget_info(self):
        budget, spent = self.get_cu_budget_daily(), self.get_cu_spent_today()
        return dict(budget_daily=budget, spent_today=spent, remaining_today=max(0, budget-spent),
                    percent_used=min(100, spent/budget*100) if budget > 0 else 0, is_exceeded=spent >= budget)
