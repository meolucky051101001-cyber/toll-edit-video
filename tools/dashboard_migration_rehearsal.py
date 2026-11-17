"""Rehearse migration and guarded rollback using generated temporary state only."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
MARKER = ".dashboard-rehearsal-fixture"


def _owned(root: Path) -> Path:
    root = root.resolve()
    if not (root / MARKER).is_file():
        raise ValueError("This operation only accepts a marked generated rehearsal fixture")
    return root


def _sqlite(path: Path) -> bool:
    with path.open('rb') as stream:
        return stream.read(16) == b'SQLite format 3\0'


def _files(root: Path) -> list[Path]:
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("A fixture path escapes the rehearsal root")
        # Locks are synchronization metadata. Committed WAL contents are read
        # through the owning database; SHM and WAL bytes are not standalone state.
        if not path.is_file() or path.name == MARKER or path.name.endswith('.lock'):
            continue
        if path.name.endswith(('-wal', '-shm')):
            owner = path.with_name(path.name.rsplit('-', 1)[0])
            if owner.is_file() and _sqlite(owner):
                continue
        result.append(path)
    return result


def _readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def _database_state(path: Path) -> str:
    """Stable schema/row identity independent of physical journal headers."""
    digest = hashlib.sha256()
    with closing(_readonly(path)) as db:
        db.execute('BEGIN')
        for pragma in ('user_version', 'application_id'):
            digest.update(f'{pragma}={db.execute("PRAGMA " + pragma).fetchone()[0]}\n'.encode())
        for statement in db.iterdump():
            digest.update(statement.encode('utf-8') + b'\n')
    return digest.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    root = _owned(root)
    return {path.relative_to(root).as_posix():
            _database_state(path) if _sqlite(path) else hashlib.sha256(path.read_bytes()).hexdigest()
            for path in _files(root)}


def _backup_database(source: Path, target: Path) -> None:
    deadline = time.monotonic() + 10

    def bounded_backup(status, remaining, total):
        if time.monotonic() > deadline:
            raise RuntimeError('SQLite backup did not reach a consistent snapshot in time')

    with closing(_readonly(source)) as db, closing(sqlite3.connect(target)) as saved:
        db.backup(saved, pages=128, progress=bounded_backup, sleep=.01)
        saved.execute('PRAGMA journal_mode=DELETE')
        if saved.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite snapshot integrity check failed')


@contextmanager
def _fixture_lock(root: Path):
    from batch_lifecycle_lock import lifecycle_lock
    from queue_control_service import state_lock
    with lifecycle_lock(root / 'control'), state_lock(root / 'telegram_queue.json'):
        yield


def _assert_quiescent(root: Path, *, restoring: bool) -> None:
    from queue_control_service import read_snapshot
    if read_snapshot(root / 'telegram_queue.json')['active_jobs']:
        raise RuntimeError('Drain active work before capturing a snapshot or rollback')
    for path in _files(root):
        if not _sqlite(path) or '.pre-v2-' in path.name:
            continue
        with closing(_readonly(path)) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'batch_plans' in tables:
                states = [json.loads(r[0]).get('state') for r in db.execute('SELECT plan FROM batch_plans')]
                if any(state in {'accepted', 'running', 'recovery_required'} for state in states):
                    raise RuntimeError('Drain or reconcile uncertain batch work before rollback/snapshot')
            if 'cloud_jobs' in tables:
                columns = {r[1] for r in db.execute('PRAGMA table_info(cloud_jobs)')}
                uncertain = " OR session_cleanup='unconfirmed'" if 'session_cleanup' in columns else ''
                if db.execute("SELECT 1 FROM cloud_jobs WHERE state NOT IN ('queued','completed','error','stopped')"
                              + uncertain + ' LIMIT 1').fetchone():
                    raise RuntimeError('Drain or reconcile uncertain cloud work before rollback/snapshot')
    schedule = root / 'control' / 'schedule.json'
    if restoring and schedule.is_file():
        state = json.loads(schedule.read_text(encoding='utf-8-sig')).get('state')
        if state in {'holding', 'dispatching', 'canceling', 'needs_review'}:
            raise RuntimeError('Reconcile uncertain schedule before rollback')


def capture(root: Path, backup: Path) -> dict:
    root = _owned(root)
    backup = backup.resolve()
    if backup.exists() or backup.is_relative_to(root):
        raise ValueError('Backup destination must be new and outside the fixture')
    with _fixture_lock(root):
        _assert_quiescent(root, restoring=False)
        before = inventory(root)
        hashes = {}
        for path in _files(root):
            relative = path.relative_to(root).as_posix()
            target = backup / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if _sqlite(path):
                _backup_database(path, target)
            else:
                shutil.copyfile(path, target)
            hashes[relative] = hashlib.sha256(target.read_bytes()).hexdigest()
        if inventory(root) != before:
            raise RuntimeError('State changed during capture; snapshot is not safe for rollback')
    return {'version': 2, 'files': hashes, 'state': before}


def guarded_restore(root: Path, backup: Path, expected_current: dict, original: dict) -> None:
    """Restore only if no accepted work or state changed after migration."""
    from pipeline_v2.atomic_io import atomic_write_bytes
    root = _owned(root)
    backup = backup.resolve()
    if original.get('version') != 2:
        raise ValueError('Unsupported rehearsal snapshot inventory')
    with _fixture_lock(root):
        current = inventory(root)
        if current != expected_current:
            raise RuntimeError("State changed after migration; reconcile new jobs and receipts before rollback")
        _assert_quiescent(root, restoring=True)
        # Validate every backup byte before the first write.
        saved = {}
        for relative, digest in original['files'].items():
            destination = (root / relative).resolve()
            source = (backup / relative).resolve()
            if not destination.is_relative_to(root) or not source.is_relative_to(backup.resolve()):
                raise ValueError("Invalid backup inventory path")
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise RuntimeError("Backup integrity check failed")
            saved[relative] = data
        # This tool owns generated fixtures with no running service. Reject an
        # open writer, and checkpoint drained DBs before replacing their image.
        databases = [root / relative for relative, data in saved.items() if data.startswith(b'SQLite format 3\0')]
        for path in databases:
            if path.is_file():
                with closing(sqlite3.connect(path, timeout=.05)) as db:
                    row = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if row and row[0]:
                        raise RuntimeError('Drain SQLite writers before rollback')
                    db.execute('BEGIN EXCLUSIVE')
                    db.rollback()
        for relative, data in saved.items():
            atomic_write_bytes(root / relative, data)
        for relative in current.keys() - original['files'].keys():
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Invalid restore target")
            target.unlink()
        for path in databases:
            for suffix in ('-wal', '-shm'):
                path.with_name(path.name + suffix).unlink(missing_ok=True)
        if inventory(root) != original['state']:
            raise RuntimeError('Restored fixture does not match captured schema/rows')


def _legacy_cloud(path: Path, *, active: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute('''CREATE TABLE cloud_jobs (
            id INTEGER PRIMARY KEY,job_id TEXT UNIQUE,video_path TEXT,video_name TEXT,gpu_type TEXT,
            task_type TEXT,state TEXT,percent INTEGER,step_name TEXT,session_name TEXT,
            cu_estimated REAL,cu_actual REAL,created_at REAL,started_at REAL,completed_at REAL,result_path TEXT,error TEXT)''')
        for index, state in enumerate(['queued', 'processing' if active else 'stopped', 'completed'], 1):
            db.execute('INSERT INTO cloud_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (index, f'legacy-{index}', 'fixture.mp4', 'fixture.mp4', 'T4', 'full_pipeline', state,
                        0, '', 'legacy-session' if state == 'processing' else None, 0, 0, 1, None, None, None, None))
        db.commit()


def _persisted_batch(root: Path) -> tuple[dict, dict]:
    from batch_plan_service import canonical_json, fingerprint
    source = root / 'input' / 'A thử.mp4'
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'synthetic-source-no-provider')
    request = {'items': [{'filename': source.name}], 'idempotency_key': 'legacy-batch-operation'}
    payload = {key: value for key, value in request.items() if key != 'idempotency_key'}
    plan = {'schema_version': 1, 'revision': 1, 'batch_id': 'legacy-batch', 'operation_id': request['idempotency_key'],
            'source_kind': 'local', 'state': 'prepared', 'execution_id': None, 'created_at': 1,
            'input_root': str(source.parent), 'output_root': str(root / 'output'), 'video_mode': 'auto',
            'items': [{'job_id': 'legacy-item', 'position': 1, 'filename': source.name,
                       'source_ref': str(source), 'source_fingerprint': fingerprint(source),
                       'output_ref': str(root / 'output' / 'Dubbed_A thử.mp4'), 'state': 'pending',
                       'config_snapshot_id': 'frozen-config', 'config_revision': 1,
                       'config_snapshot': {'effective_config': {'voice': 'fixture', 'speed': 1.0}},
                       'effective_config': {'voice': 'fixture', 'speed': 1.0}}]}
    path = root / 'control' / 'batch_plans.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE batch_plans (batch_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, plan TEXT NOT NULL)')
        db.execute('INSERT INTO batch_plans VALUES (?,?,?,?)', (plan['batch_id'], plan['operation_id'],
                   hashlib.sha256(canonical_json(payload).encode()).hexdigest(), canonical_json(plan)))
        db.commit()
    return plan, request


def _refuses_restore(root: Path, backup: Path, original: dict, reason: str) -> None:
    before = inventory(root)
    try:
        guarded_restore(root, backup, before, original)
    except RuntimeError as exc:
        assert reason in str(exc), str(exc)
    else:
        raise AssertionError('Rollback discarded uncertain state: ' + reason)
    assert inventory(root) == before


def rehearse() -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    from pipeline_v2.atomic_io import atomic_write_json
    from queue_control_service import migrate_snapshot, state_lock, read_snapshot
    from batch_plan_service import BatchPlanStore
    from cloud.cloud_job_store import CloudJobStore
    from night_schedule_service import ScheduleStore
    started = time.monotonic()
    checks = []
    with tempfile.TemporaryDirectory(prefix="dashboard_migration_") as temporary:
        runtime = Path(temporary)
        fixture = runtime / "workspace"
        fixture.mkdir()
        (fixture / MARKER).write_text("generated-fixture-only", encoding="utf-8")
        snapshot = fixture / "telegram_queue.json"
        original_bytes = json.dumps({"items": [
            {"url": "https://example.invalid/same", "filename": "A thử.mp4", "type": "url"},
            {"url": "https://example.invalid/same", "filename": "B.mp4", "type": "url"},
        ], "active_job": None}, ensure_ascii=False).encode("utf-8")
        snapshot.write_bytes(original_bytes)
        schedule = fixture / 'control' / 'schedule.json'
        atomic_write_json(schedule, {'active': False, 'time': '02:00', 'last_fired_date': '2020-01-01'})
        schedule_bytes = schedule.read_bytes()
        plan, request = _persisted_batch(fixture)
        cloud_path = fixture / 'cloud' / 'cloud_jobs.sqlite'
        _legacy_cloud(cloud_path)
        atomic_write_json(fixture / "configs" / "frozen.json", {"voice": "fixture", "speed": 1.0})
        backup = runtime / "backup"
        original = capture(fixture, backup)
        with state_lock(snapshot):
            first = migrate_snapshot(snapshot)
            second = migrate_snapshot(snapshot)
        ids = [item["job_id"] for item in first["items"]]
        assert len(ids) == len(set(ids)) == 2
        assert first == second
        assert list(fixture.glob("*.legacy-*.bak"))[0].read_bytes() == original_bytes
        checks.extend(["legacy migration preserves distinct requests with the same URL",
                       "migration is idempotent and retains byte-exact legacy backup"])
        schedules = ScheduleStore(schedule)
        first_schedule = schedules.get()
        assert first_schedule['state'] == 'fired' and schedules.get() == first_schedule
        schedule_backups = list(schedule.parent.glob('schedule.json.legacy-*.bak'))
        assert len(schedule_backups) == 1 and schedule_backups[0].read_bytes() == schedule_bytes
        checks.append('legacy fired schedule migrates once, retains exact backup and never dispatches')
        batches = BatchPlanStore(fixture)

        def no_refreeze(**kwargs):
            raise AssertionError('Persisted plan must not be re-frozen on replay')

        assert batches.prepare(request, fixture / 'input', fixture / 'output', no_refreeze) == plan
        checks.append('persisted batch SQLite retains IDs, frozen source/config and idempotent intent')
        cloud = CloudJobStore(cloud_path)
        first_cloud = [job.to_dict() for job in cloud.list_jobs()]
        assert [job.to_dict() for job in CloudJobStore(cloud_path).list_jobs()] == first_cloud
        assert len(first_cloud) == 3 and cloud.get_receipt('legacy-3') is None
        cloud_backups = list(cloud_path.parent.glob(cloud_path.name + '.pre-v2-*.sqlite'))
        assert len(cloud_backups) == 1
        assert _database_state(cloud_backups[0]) == _database_state(backup / 'cloud' / 'cloud_jobs.sqlite')
        checks.append('cloud upgrade preserves rows/backup, replays idempotently and invents no receipt')
        migrated = inventory(fixture)
        guarded_restore(fixture, backup, migrated, original)
        assert inventory(fixture) == original['state']
        assert snapshot.read_bytes() == original_bytes
        checks.append('quiescent rollback restores JSON bytes and every SQLite schema/row')

        atomic_write_json(schedule, {'active': True, 'time': '02:00'})
        uncertain = ScheduleStore(schedule).get()
        assert uncertain['state'] == 'needs_review' and not uncertain['active']
        assert ScheduleStore(schedule).claim(now=time.time() + 86400) is None
        _refuses_restore(fixture, backup, original, 'uncertain schedule')
        checks.append('clock-only legacy schedule stays held without dispatch and blocks uncertain rollback')
        batches.update(plan['batch_id'], state='accepted', execution_id='interrupted-execution')
        assert batches.reconcile_interrupted() == [plan['batch_id']]
        recovered = batches.get(plan['batch_id'])
        assert recovered['state'] == 'recovery_required' and recovered['items'] == plan['items']
        assert batches.reconcile_interrupted() == []
        _refuses_restore(fixture, backup, original, 'uncertain batch')
        checks.append('interrupted batch acceptance preserves reservation/config across repeated reconciliation')
        # Reconcile the synthetic batch and schedule before checking cloud alone.
        batches.update(plan['batch_id'], state='abandoned')
        ScheduleStore(schedule).cancel()
        with closing(sqlite3.connect(cloud_path)) as db:
            db.execute("UPDATE cloud_jobs SET state='processing',session_name='legacy-session' WHERE job_id='legacy-2'")
            db.commit()
        cloud = CloudJobStore(cloud_path)
        assert cloud.get_job('legacy-2').state == 'recovery_required'
        assert cloud.get_job('legacy-2').session_cleanup == 'unconfirmed'
        assert cloud.claim_next() is None
        assert CloudJobStore(cloud_path).get_job('legacy-2').revision == 1
        _refuses_restore(fixture, backup, original, 'uncertain cloud')
        checks.append('legacy active cloud owner is preserved as uncertain and blocks claims/rollback')
        with state_lock(snapshot):
            migrate_snapshot(snapshot)
        migrated = inventory(fixture)
        current = read_snapshot(snapshot)
        current["items"].append({"job_id": "accepted_after_upgrade", "type": "url"})
        current["revision"] += 1
        atomic_write_json(snapshot, current)
        before_refusal = inventory(fixture)
        try:
            guarded_restore(fixture, backup, migrated, original)
        except RuntimeError as exc:
            assert "State changed" in str(exc)
        else:
            raise AssertionError("Rollback overwrote work accepted after upgrade")
        assert inventory(fixture) == before_refusal
        checks.append("rollback refuses changed post-upgrade state without changing any bytes")
    return {"isolated": True, "production_modified": False, "passed": checks,
            "duration_seconds": round(time.monotonic() - started, 3),
            "scope": "generated queue/schedule/batch/cloud migration and rollback; live services not exercised"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "test_evidence/dashboard/migration.json")
    args = parser.parse_args()
    result = rehearse()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
