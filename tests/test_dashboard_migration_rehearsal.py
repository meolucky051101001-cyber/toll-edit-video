import json
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from tools.dashboard_migration_rehearsal import (
    MARKER, _database_state, _legacy_cloud, capture, guarded_restore, inventory, rehearse,
)


def test_migration_and_guarded_rollback_rehearsal():
    result = rehearse()
    assert result["isolated"] and not result["production_modified"]
    assert len(result["passed"]) == 10
    assert all(term in result['scope'] for term in ('queue', 'schedule', 'batch', 'cloud'))


def test_unmarked_workspace_cannot_be_backed_up_or_restored(tmp_path):
    with pytest.raises(ValueError, match="marked"):
        capture(tmp_path, tmp_path / "backup")
    with pytest.raises(ValueError, match="marked"):
        guarded_restore(tmp_path, tmp_path / "backup", {}, {})


def test_backup_corruption_is_detected_before_any_restore_write(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / MARKER).write_text("fixture")
    (root / "telegram_queue.json").write_text(json.dumps({"items": []}))
    (root / "config.json").write_text('{"voice":"original"}')
    backup = tmp_path / "backup"
    original = capture(root, backup)
    expected = inventory(root)
    (backup / "config.json").write_text("corrupt")
    with pytest.raises(RuntimeError, match="integrity"):
        guarded_restore(root, backup, expected, original)
    assert inventory(root) == expected


def test_active_job_blocks_quiescent_backup(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / MARKER).write_text("fixture")
    (root / "telegram_queue.json").write_text(json.dumps({"items": [], "active_job": {"job_id": "active"}}))
    with pytest.raises(RuntimeError, match="Drain"):
        capture(root, tmp_path / "backup")


def fixture_root(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / MARKER).write_text('generated-fixture-only')
    (root / 'telegram_queue.json').write_text('{"items": []}')
    return root


def test_sqlite_backup_includes_committed_wal_and_is_standalone(tmp_path):
    root = fixture_root(tmp_path)
    path = root / 'events.sqlite'
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE events (name TEXT)')
        writer.execute('INSERT INTO events VALUES (?)', ('committed in WAL – thử',))
        writer.commit()
        assert path.with_name(path.name + '-wal').stat().st_size > 0
        main_before = path.read_bytes()
        backup = tmp_path / 'backup'
        manifest = capture(root, backup)
        assert path.read_bytes() == main_before
        assert not any(relative.endswith(('-wal', '-shm')) for relative in manifest['files'])
        saved = backup / path.name
        with closing(sqlite3.connect(saved)) as db:
            assert db.execute('SELECT name FROM events').fetchall() == [('committed in WAL – thử',)]
            assert db.execute('PRAGMA quick_check').fetchone() == ('ok',)
            assert db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        assert _database_state(saved) == _database_state(path)


def test_committed_wal_after_upgrade_blocks_rollback_without_file_writes(tmp_path):
    root = fixture_root(tmp_path)
    path = root / 'events.sqlite'
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute('CREATE TABLE events (name TEXT)')
        writer.commit()
        backup = tmp_path / 'backup'
        original = capture(root, backup)
        expected = inventory(root)
        writer.execute("INSERT INTO events VALUES ('new accepted work')")
        writer.commit()
        state = inventory(root)
        raw_files = {p.name: p.read_bytes() for p in root.iterdir()
                     if p.is_file() and p.name != path.name + '-shm' and not p.name.endswith('.lock')}
        with pytest.raises(RuntimeError, match='State changed'):
            guarded_restore(root, backup, expected, original)
        assert inventory(root) == state
        assert all((root / name).read_bytes() == value for name, value in raw_files.items())


def test_sqlite_rollback_removes_migrated_schema_and_keeps_all_original_rows(tmp_path):
    root = fixture_root(tmp_path)
    path = root / 'events.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE events (name TEXT)')
        db.execute("INSERT INTO events VALUES ('before upgrade')")
        db.execute('PRAGMA user_version=1')
        db.commit()
        original = capture(root, tmp_path / 'backup')
    with closing(sqlite3.connect(path)) as db:
        db.execute('ALTER TABLE events ADD COLUMN receipt TEXT')
        db.execute('PRAGMA user_version=2')
        db.commit()
    expected = inventory(root)
    guarded_restore(root, tmp_path / 'backup', expected, original)
    assert inventory(root) == original['state']
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert [r[1] for r in db.execute('PRAGMA table_info(events)')] == ['name']
        assert db.execute('SELECT * FROM events').fetchall() == [('before upgrade',)]


def test_corrupt_sqlite_backup_is_rejected_before_any_application_file_changes(tmp_path):
    root = fixture_root(tmp_path)
    path = root / 'events.sqlite'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE events (name TEXT)')
        db.commit()
    backup = tmp_path / 'backup'
    original = capture(root, backup)
    before = inventory(root)
    (backup / path.name).write_bytes(b'broken sqlite snapshot')
    with pytest.raises(RuntimeError, match='integrity'):
        guarded_restore(root, backup, before, original)
    assert inventory(root) == before


def test_cloud_legacy_automatic_backup_keeps_committed_wal_before_migration(tmp_path):
    from cloud.cloud_job_store import CloudJobStore
    path = tmp_path / 'cloud.sqlite'
    _legacy_cloud(path, active=True)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('PRAGMA wal_autocheckpoint=0')
        writer.execute("UPDATE cloud_jobs SET step_name='last committed progress' WHERE job_id='legacy-2'")
        writer.commit()
        assert path.with_name(path.name + '-wal').stat().st_size > 0
        before = _database_state(path)
        store = CloudJobStore(path)
        backups = list(tmp_path.glob('cloud.sqlite.pre-v2-*.sqlite'))
        assert len(backups) == 1 and _database_state(backups[0]) == before
        assert store.get_job('legacy-2').state == 'recovery_required'
        assert store.get_job('legacy-2').step_name == 'last committed progress'
        assert store.claim_next() is None
        assert len(CloudJobStore(path).list_jobs()) == 3
        assert len(list(tmp_path.glob('cloud.sqlite.pre-v2-*.sqlite'))) == 1


def test_backup_cannot_be_created_recursively_inside_fixture(tmp_path):
    root = fixture_root(tmp_path)
    with pytest.raises(ValueError, match='outside'):
        capture(root, root / 'nested-backup')


def test_restore_inventory_cannot_escape_fixture(tmp_path):
    root = fixture_root(tmp_path)
    original = capture(root, tmp_path / 'backup')
    original['files']['../outside.json'] = 'not-important'
    before = inventory(root)
    with pytest.raises(ValueError, match='path'):
        guarded_restore(root, tmp_path / 'backup', before, original)
    assert inventory(root) == before
