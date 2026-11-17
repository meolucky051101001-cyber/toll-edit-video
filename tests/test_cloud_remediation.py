"""Cloud safety acceptance using isolated SQLite, fixture files and provider fakes."""
import hashlib
import asyncio
import json
import logging
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from backend.cloud import colab_bridge as bridge
from backend.cloud.cloud_artifacts import verify_artifact, sha256_file, stage_for_publication, probe_media
from backend.cloud.cloud_contracts import CloudCapabilityError, CloudOwnershipError, CloudVerificationError
from backend.cloud.cloud_job_store import CloudJobStore
from backend.cloud.cloud_orchestrator import CloudOrchestrator


def media(_):
    return {'streams': [{'codec_type': 'video', 'width': 320, 'height': 240}, {'codec_type': 'audio'}],
            'format': {'duration': '2.0'}}


@pytest.fixture
def store(tmp_path):
    return CloudJobStore(tmp_path / 'jobs.sqlite')


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'nguồn có dấu và space.mp4'
    path.write_bytes(b'source-fixture')
    return path


def claimed(store, source, **kwargs):
    return store.enqueue(str(source), claim_owner='test-owner', **kwargs)


def ready_to_publish(store, job, tmp_path):
    token = dict(attempt_id=job.attempt_id, owner_id=job.owner_id)
    for state in ('uploading', 'processing', 'downloading', 'validating'):
        assert store.update_progress(job.job_id, state=state, source_sha256='source-hash', **token)
    data = tmp_path / ('verified-' + job.attempt_id)
    data.write_bytes(b'verified-media-fixture')
    info = dict(sha256=sha256_file(data), size_bytes=data.stat().st_size, duration_seconds=2., has_video=True, has_audio=True)
    output_dir = tmp_path / 'outputs'
    stage = stage_for_publication(data, output_dir, job.job_id, job.attempt_id, info)
    return stage, output_dir / ('cloud_' + job.video_name), info


class FakeProvider:
    def __init__(self, store, job, *, hook=None, stop_ok=True, invalid=False):
        self.store, self.job, self.hook = store, job, hook
        self.stop_ok, self.invalid = stop_ok, invalid
        self.calls = []
        self.secret_installed = False
        self.payload = b'video-fixture'

    def _call(self, name):
        self.calls.append(name)
        if self.hook:
            self.hook(name)

    def ensure_ready(self):
        self._call('ready')

    def create_session(self, session, **kwargs):
        self._call('create')
        return True, ''

    def upload_file(self, *args):
        self._call('upload')
        return True, ''

    def exec_argv(self, session, argv, **kwargs):
        self._call('exec:' + argv[0] + (':' + argv[1] if len(argv) > 1 else ''))
        assert isinstance(argv, list)
        assert 'sentinel-secret' not in repr(argv) + repr(kwargs)
        return True, ''

    def install_secret(self, session, secret_env, attempt_id):
        self.secret_installed = True
        self._call('secret')
        return '/private/secret-' + attempt_id

    def cleanup_secret(self, session, ref):
        self._call('cleanup_secret')
        self.secret_installed = False

    def stop_session(self, session):
        self._call('stop')
        return self.stop_ok, ''

    def download_file(self, session, remote, target):
        self._call('download:' + Path(remote).name)
        path = Path(target) / Path(remote).name
        if remote.endswith('.json'):
            current = self.store.get_job(self.job.job_id)
            manifest = dict(job_id=self.job.job_id, attempt_id=self.job.attempt_id,
                            source_sha256=current.source_sha256, sha256=hashlib.sha256(self.payload).hexdigest(),
                            size_bytes=len(self.payload), duration_seconds=2.)
            if self.invalid:
                manifest['sha256'] = 'wrong'
            path.write_text(json.dumps(manifest), encoding='utf-8')
        else:
            path.write_bytes(self.payload)
        return True, str(path)


def orchestrator(store, provider, tmp_path):
    return CloudOrchestrator(store, provider=provider, output_dir=tmp_path / 'outputs', probe=media,
                             secret_supplier=lambda: {'GEMINI_API_KEY': 'sentinel-secret'})


def test_live_gate_precedes_secret_access_and_provision(store, source, tmp_path, monkeypatch):
    job = claimed(store, source)
    calls = []
    monkeypatch.setattr(bridge, '_run_wsl_colab', lambda *a, **k: calls.append(a))
    runner = CloudOrchestrator(store, output_dir=tmp_path, secret_supplier=lambda: pytest.fail('secret read'))
    result = runner.process_job(job)
    assert result.status == 'error' and 'cloud_cli_unverified' in result.error
    assert calls == [] and store.get_job(job.job_id).session_name is None


def test_shell_entry_point_is_closed():
    with pytest.raises(ValueError, match='shell_commands_disabled'):
        bridge.exec_cmd('session', 'echo unsafe')


def test_wsl_bridge_executes_without_linux_or_windows_shell(monkeypatch):
    arguments = ['upload', '-s', 'session name', '/mnt/c/video có dấu.mp4',
                 "content/quote'\".mp4", '$(touch forbidden); & echo unsafe',
                 '`echo unsafe`', 'line\nbreak', '$HOME', '*.mp4']
    observed = []

    def run(argv, **kwargs):
        observed.append((argv, kwargs))
        return SimpleNamespace(returncode=7, stdout=b'output', stderr=b'error')

    monkeypatch.setattr(bridge.subprocess, 'run', run)
    assert bridge._run_wsl_colab(arguments, timeout=19) == (7, 'output', 'error')
    assert observed == [([
        'wsl', '-d', bridge.DEFAULT_CONFIG['wsl_distro'], '-u', bridge.DEFAULT_CONFIG['wsl_user'],
        '--exec', 'env', 'PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin',
        'PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring', 'colab', '--auth', 'oauth2',
        *arguments], {'capture_output': True, 'timeout': 19, 'shell': False})]


def test_remote_wrapper_preserves_argv_and_redacts_secret(tmp_path, capsys):
    secret = tmp_path / 'secret.json'
    secret.write_text(json.dumps({'GEMINI_API_KEY': 'sentinel-secret'}))
    argv = [sys.executable, '-c', "import os,sys,json;print(json.dumps(sys.argv[1:]));print(os.environ['GEMINI_API_KEY'])",
            "quote'\"", 'space name', 'Tiếng Việt', 'line\nbreak', '$(touch nope); & echo unsafe']
    wrapper = bridge.build_exec_wrapper(argv, str(tmp_path), 'run123', str(secret))
    assert 'sentinel-secret' not in wrapper
    exec(compile(wrapper, '<fixture-runner>', 'exec'), {})
    output = capsys.readouterr().out
    assert json.loads(output.splitlines()[0]) == argv[3:]
    assert 'sentinel-secret' not in output and '[REDACTED]' in output
    assert '"returncode": 0' in output
    assert not secret.exists() and not (tmp_path / 'nope').exists()


@pytest.mark.parametrize('rc,create,size', [(1, True, 7), (0, False, 0), (0, True, 0)])
def test_download_rejects_failed_partial_missing_or_empty(tmp_path, monkeypatch, rc, create, size):
    monkeypatch.setattr(bridge, 'ensure_live_ready', lambda: None)
    monkeypatch.setattr(bridge, 'to_wsl_path', str)
    stale = tmp_path / 'output_video.mp4'
    stale.write_bytes(b'previous-good-output')
    def transfer(argv, timeout):
        if create:
            Path(argv[-1]).write_bytes(b'x' * size)
        return rc, '', 'sentinel-secret'
    monkeypatch.setattr(bridge, '_run_wsl_colab', transfer)
    ok, error = bridge.download_file('s', 'output_video.mp4', tmp_path)
    assert not ok and error == 'cloud_download_failed'
    assert stale.read_bytes() == b'previous-good-output'
    assert not list(tmp_path.glob('*.part'))


def test_download_uses_unique_new_file(tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, 'ensure_live_ready', lambda: None)
    monkeypatch.setattr(bridge, 'to_wsl_path', str)
    def transfer(argv, timeout):
        Path(argv[-1]).write_bytes(b'fresh')
        return 0, '', ''
    monkeypatch.setattr(bridge, '_run_wsl_colab', transfer)
    a, p1 = bridge.download_file('s', 'output.mp4', tmp_path)
    b, p2 = bridge.download_file('s', 'output.mp4', tmp_path)
    assert a and b and p1 != p2


@pytest.mark.parametrize('defect', ['hash', 'identity', 'duration', 'nan', 'audio', 'video', 'zero'])
def test_verify_artifact_rejects_invalid_media(tmp_path, defect):
    path = tmp_path / 'video'
    path.write_bytes(b'video')
    manifest = dict(job_id='j', attempt_id='a', source_sha256='s', size_bytes=5, sha256=sha256_file(path), duration_seconds=2.)
    report = media(path)
    if defect == 'hash': manifest['sha256'] = 'wrong'
    if defect == 'identity': manifest['attempt_id'] = 'old'
    if defect == 'duration': report['format']['duration'] = '20'
    if defect == 'nan': report['format']['duration'] = 'nan'
    if defect in ('audio', 'video'): report['streams'] = [s for s in report['streams'] if s['codec_type'] != defect]
    if defect == 'zero': path.write_bytes(b'')
    with pytest.raises(CloudVerificationError):
        verify_artifact(path, manifest, 'j', 'a', 's', lambda _: report)


def test_probe_timeout_is_verification_error(monkeypatch):
    def timeout(*a, **k): raise subprocess.TimeoutExpired('ffprobe', 30)
    monkeypatch.setattr(subprocess, 'run', timeout)
    with pytest.raises(CloudVerificationError, match='probe_failed'):
        probe_media('fixture')


def test_atomic_external_id_claim_and_worker_do_not_duplicate(store, source):
    first = claimed(store, source, external_job_id='telegram-1')
    replay = store.enqueue(str(source), external_job_id='telegram-1', claim_owner='other-owner')
    assert replay.job_id == first.job_id and replay.owner_id == first.owner_id
    assert store.claim_next('worker') is None
    assert store.claim_job(first.job_id, 'other') is None
    assert store.start_attempt(first.job_id, first.attempt_id, first.owner_id)
    assert not store.start_attempt(first.job_id, first.attempt_id, first.owner_id)


def test_concurrent_claims_have_one_winner(store, source):
    job = store.enqueue(str(source))
    barrier = threading.Barrier(3)
    results = []
    def claim(owner):
        barrier.wait()
        results.append(CloudJobStore(store.db_path).claim_job(job.job_id, owner))
    threads = [threading.Thread(target=claim, args=(str(i),)) for i in range(2)]
    for thread in threads: thread.start()
    barrier.wait()
    for thread in threads: thread.join(5)
    assert len(results) == 2 and sum(r is not None for r in results) == 1


def test_cancel_queued_and_terminal_are_monotonic(store, source):
    job = store.enqueue(str(source))
    assert store.request_cancel(job.job_id)['state'] == 'stopped'
    assert store.claim_next() is None
    assert not store.update_progress(job.job_id, state='completed')
    assert not store.request_cancel(job.job_id)['accepted']


def test_cancel_and_stale_revision_reject_progress(store, source):
    job = claimed(store, source)
    assert not store.update_progress(job.job_id, attempt_id='old', owner_id=job.owner_id, state='uploading')
    assert not store.update_progress(job.job_id, attempt_id=job.attempt_id, owner_id=job.owner_id,
                                     expected_revision=job.revision - 1, state='uploading')
    assert store.request_cancel(job.job_id)['accepted']
    assert not store.update_progress(job.job_id, attempt_id=job.attempt_id, owner_id=job.owner_id, state='uploading')
    store.finish_attempt(job.job_id, job.attempt_id, job.owner_id)
    assert store.get_job(job.job_id).state == 'stopped'
    assert not store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, error='late callback')


def test_verified_pipeline_receipt_and_replay(store, source, tmp_path):
    job = claimed(store, source)
    provider = FakeProvider(store, job)
    runner = orchestrator(store, provider, tmp_path)
    result = runner.process_job(job)
    assert result and result.status == 'completed' and result.receipt == store.get_receipt(job.job_id)
    assert result.receipt['source_sha256'] == sha256_file(source)
    assert provider.calls[-2:] == ['cleanup_secret', 'stop']
    assert not provider.secret_installed
    calls = list(provider.calls)
    assert runner.process_job(job)
    assert provider.calls == calls
    Path(result.result_path).write_bytes(b'tampered')
    assert store.get_receipt(job.job_id) is None
    assert runner.process_job(job).status == 'recovery_required'


@pytest.mark.parametrize('phase', ['create', 'upload', 'exec:python3:full_pipeline.py', 'download:output_video.mp4', 'download:output_manifest.json'])
def test_cancel_at_each_provider_phase_never_publishes(store, source, tmp_path, phase):
    job = claimed(store, source)
    def hook(name):
        if name == phase: store.request_cancel(job.job_id)
    provider = FakeProvider(store, job, hook=hook)
    result = orchestrator(store, provider, tmp_path).process_job(job)
    assert result.status == 'stopped' and not result
    assert provider.calls[-1] == 'stop'
    assert not (tmp_path / 'outputs' / ('cloud_' + source.name)).exists()
    assert not provider.secret_installed


def test_cancel_during_long_operation_stops_and_drains(store, source, tmp_path):
    job = claimed(store, source)
    entered, stopped, released = threading.Event(), threading.Event(), threading.Event()
    def hook(name):
        if name == 'exec:python3:full_pipeline.py':
            entered.set()
            assert stopped.wait(5)
            assert released.wait(5)
        if name == 'stop': stopped.set()
    provider = FakeProvider(store, job, hook=hook)
    result = []
    thread = threading.Thread(target=lambda: result.append(orchestrator(store, provider, tmp_path).process_job(job)))
    thread.start()
    assert entered.wait(5)
    store.request_cancel(job.job_id)
    assert stopped.wait(5)
    assert thread.is_alive() and store.get_job(job.job_id).state == 'stopping'
    released.set()
    thread.join(5)
    assert result[0].status == 'stopped'


def test_stop_failure_keeps_ownership_and_blocks_next_claim(store, source, tmp_path):
    job = claimed(store, source)
    provider = FakeProvider(store, job, hook=lambda phase: store.request_cancel(job.job_id) if phase == 'create' else None, stop_ok=False)
    assert orchestrator(store, provider, tmp_path).process_job(job).status == 'stop_incomplete'
    store.enqueue(str(source))
    assert store.claim_next() is None
    assert not store.delete_job(job.job_id)


def test_invalid_download_manifest_does_not_replace_old_output(store, source, tmp_path):
    job = claimed(store, source)
    output = tmp_path / 'outputs' / ('cloud_' + source.name)
    output.parent.mkdir()
    output.write_bytes(b'old-good-output')
    result = orchestrator(store, FakeProvider(store, job, invalid=True), tmp_path).process_job(job)
    assert result.status == 'error' and result.error == 'cloud_manifest_hash_mismatch'
    assert output.read_bytes() == b'old-good-output' and store.get_receipt(job.job_id) is None


def test_output_conflict_never_clobbers(store, source, tmp_path):
    job = claimed(store, source)
    stage, output, info = ready_to_publish(store, job, tmp_path)
    output.write_bytes(b'other-owner')
    with pytest.raises(CloudVerificationError, match='output_conflict'):
        store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info)
    assert output.read_bytes() == b'other-owner'


def test_cancel_wins_before_publication(store, source, tmp_path):
    job = claimed(store, source)
    stage, output, info = ready_to_publish(store, job, tmp_path)
    assert store.request_cancel(job.job_id)['accepted']
    with pytest.raises(CloudOwnershipError):
        store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info)
    assert not output.exists()


def test_publication_intent_wins_racing_cancel(store, source, tmp_path):
    job = claimed(store, source)
    stage, output, info = ready_to_publish(store, job, tmp_path)
    intent, release = threading.Event(), threading.Event()
    receipts = []
    def hook(phase):
        if phase == 'after_intent':
            intent.set()
            assert release.wait(5)
    thread = threading.Thread(target=lambda: receipts.append(store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info, crash_hook=hook)))
    thread.start()
    assert intent.wait(5)
    outcome = CloudJobStore(store.db_path).request_cancel(job.job_id)
    assert not outcome['accepted'] and outcome['state'] == 'publishing'
    release.set()
    thread.join(5)
    assert receipts[0] and output.exists()


@pytest.mark.parametrize('phase', ['after_intent', 'after_rename', 'after_receipt'])
def test_crash_recovery_finishes_exact_publication(store, source, tmp_path, phase):
    job = claimed(store, source)
    stage, output, info = ready_to_publish(store, job, tmp_path)
    class Crash(BaseException): pass
    def hook(at):
        if at == phase: raise Crash()
    with pytest.raises(Crash):
        store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info, crash_hook=hook)
    restarted = CloudJobStore(store.db_path)
    restarted.recover()
    restarted.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
    receipt = restarted.get_receipt(job.job_id)
    assert receipt and receipt['sha256'] == info['sha256']
    assert output.read_bytes() == stage.read_bytes()
    assert restarted.recover_publication(job.job_id) == receipt


def test_recovery_rejects_same_bytes_from_different_owner(store, source, tmp_path):
    job = claimed(store, source)
    stage, output, info = ready_to_publish(store, job, tmp_path)
    class Crash(BaseException): pass
    def crash(_): raise Crash()
    with pytest.raises(Crash):
        store.publish(job.job_id, job.attempt_id, job.owner_id, stage, output, info, crash_hook=crash)
    output.write_bytes(stage.read_bytes())
    assert store.recover_publication(job.job_id) is None
    assert store.get_job(job.job_id).state == 'recovery_required'
    assert store.get_receipt(job.job_id) is None


def test_legacy_migration_retains_rows_and_holds_active_job(tmp_path):
    path = tmp_path / 'legacy.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE cloud_jobs (id INTEGER PRIMARY KEY,job_id TEXT UNIQUE,video_path TEXT,video_name TEXT,gpu_type TEXT,task_type TEXT,state TEXT,percent INTEGER,step_name TEXT,session_name TEXT,cu_estimated REAL,cu_actual REAL,created_at REAL,started_at REAL,completed_at REAL,result_path TEXT,error TEXT)')
        for i, state in enumerate(['queued', 'processing', 'completed'], 1):
            db.execute('INSERT INTO cloud_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (i, str(i), 'v.mp4', 'v.mp4', 'T4', 'full_pipeline', state, 0, '', None, 0, 0, 1, None, None, None, None))
    migrated = CloudJobStore(path)
    backups = list(tmp_path.glob('legacy.sqlite.pre-v2-*.sqlite'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT state FROM cloud_jobs WHERE job_id='2'").fetchone()[0] == 'processing'
    assert len(migrated.list_jobs()) == 3
    assert migrated.get_job('2').state == 'recovery_required'
    assert migrated.get_job('3').state == 'completed' and migrated.get_receipt('3') is None
    assert migrated.claim_next() is None
    assert len(CloudJobStore(path).list_jobs()) == 3
    assert len(list(tmp_path.glob('legacy.sqlite.pre-v2-*.sqlite'))) == 1


def test_schema_migration_releases_source_and_backup_connections(tmp_path, monkeypatch):
    path = tmp_path / 'legacy.sqlite'
    legacy = sqlite3.connect(path)
    try:
        legacy.execute('CREATE TABLE cloud_schema (version INTEGER NOT NULL)')
        legacy.execute('INSERT INTO cloud_schema VALUES (1)')
        legacy.commit()
    finally:
        legacy.close()
    connect = sqlite3.connect
    opened = []

    def tracked_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', tracked_connect)
    CloudJobStore(path)
    assert len(opened) >= 3
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
    # Windows refuses replacement while a leaked SQLite handle is still open.
    moved = path.with_name('replaceable.sqlite')
    path.replace(moved)
    moved.replace(path)


def _claim_process(db_path, job_id, owner, barrier, results):
    child_store = CloudJobStore(db_path)
    barrier.wait(10)
    result = child_store.claim_job(job_id, owner)
    results.put(result.owner_id if result else None)


def test_worker_and_telegram_claim_compete_across_processes(store, source):
    job = store.enqueue(str(source))
    ctx = multiprocessing.get_context('spawn')
    barrier, results = ctx.Barrier(3), ctx.Queue()
    children = [ctx.Process(target=_claim_process, args=(str(store.db_path), job.job_id, owner, barrier, results))
                for owner in ('worker', 'telegram')]
    for child in children: child.start()
    try:
        barrier.wait(10)
        outcomes = [results.get(timeout=10) for _ in children]
        assert sum(o is not None for o in outcomes) == 1
        assert store.get_job(job.job_id).owner_id in outcomes
    finally:
        for child in children:
            child.join(10)
            if child.is_alive(): child.terminate()
        results.close()
    assert all(child.exitcode == 0 for child in children)


def test_cancel_while_provision_pending_waits_for_create_before_stop(store, source, tmp_path):
    job = claimed(store, source)
    entered, released = threading.Event(), threading.Event()
    def hook(name):
        if name == 'create':
            entered.set()
            assert released.wait(5)
    provider = FakeProvider(store, job, hook=hook)
    results = []
    thread = threading.Thread(target=lambda: results.append(orchestrator(store, provider, tmp_path).process_job(job)))
    thread.start()
    assert entered.wait(5)
    store.request_cancel(job.job_id)
    assert 'stop' not in provider.calls
    released.set()
    thread.join(5)
    assert results[0].status == 'stopped' and provider.calls[-1] == 'stop'


def test_cancel_during_verification_never_publishes(store, source, tmp_path):
    job = claimed(store, source)
    provider = FakeProvider(store, job)
    runner = orchestrator(store, provider, tmp_path)
    def cancel_probe(path):
        store.request_cancel(job.job_id)
        return media(path)
    runner.probe = cancel_probe
    assert runner.process_job(job).status == 'stopped'
    assert not (tmp_path / 'outputs' / ('cloud_' + source.name)).exists()


def test_secret_in_typed_provider_error_is_redacted_and_cleaned(store, source, tmp_path, caplog):
    job = claimed(store, source)
    class FailingProvider(FakeProvider):
        def exec_argv(self, session, argv, **kwargs):
            if 'full_pipeline.py' in argv:
                raise CloudCapabilityError('provider rejected sentinel-secret')
            return super().exec_argv(session, argv, **kwargs)
    provider = FailingProvider(store, job)
    with caplog.at_level(logging.INFO):
        result = orchestrator(store, provider, tmp_path).process_job(job)
    assert result.status == 'error'
    assert 'sentinel-secret' not in repr(result) + caplog.text + store.get_job(job.job_id).error
    assert not provider.secret_installed and provider.calls[-2:] == ['cleanup_secret', 'stop']


def test_same_claim_cannot_execute_twice(store, source, tmp_path):
    job = claimed(store, source)
    assert store.start_attempt(job.job_id, job.attempt_id, job.owner_id)
    provider = FakeProvider(store, job)
    assert orchestrator(store, provider, tmp_path).process_job(job).status == 'busy'
    assert provider.calls == []


def test_receipt_identity_cannot_be_swapped(store, source, tmp_path):
    job = claimed(store, source)
    result = orchestrator(store, FakeProvider(store, job), tmp_path).process_job(job)
    assert result
    changed = dict(result.receipt, attempt_id='other-attempt')
    Path(str(result.result_path) + '.cloud-receipt.json').write_text(json.dumps(changed), encoding='utf-8')
    with store._connect() as db:
        db.execute('UPDATE cloud_jobs SET receipt_json=? WHERE job_id=?', (json.dumps(changed), job.job_id))
    assert store.get_receipt(job.job_id) is None


def test_output_conflict_cleans_unpublished_staging(store, source, tmp_path):
    job = claimed(store, source)
    output = tmp_path / 'outputs' / ('cloud_' + source.name)
    output.parent.mkdir()
    output.write_bytes(b'owned-before')
    result = orchestrator(store, FakeProvider(store, job), tmp_path).process_job(job)
    assert result.status == 'error' and result.error == 'output_conflict'
    assert output.read_bytes() == b'owned-before'
    assert not list((output.parent / '.cloud-staging').rglob('*.part'))


def test_real_media_manifest_and_verification(tmp_path):
    video = tmp_path / 'output_video.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x120:r=10:d=1', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=1', '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(video)],
                   check=True, capture_output=True, timeout=30)
    digest = sha256_file(video)
    script = Path(__file__).resolve().parents[1] / 'backend/cloud/gpu_scripts/write_output_manifest.py'
    subprocess.run([sys.executable, '-B', str(script), '--job-id', 'job', '--attempt-id', 'attempt',
                    '--source-sha256', digest, '--source-kind', 'file', '--input', str(video)],
                   cwd=tmp_path, check=True, capture_output=True, timeout=30)
    manifest = json.loads((tmp_path / 'output_manifest.json').read_text())
    info = verify_artifact(video, manifest, 'job', 'attempt', digest)
    assert info['has_video'] and info['has_audio'] and info['sha256'] == digest
    wrong = subprocess.run([sys.executable, '-B', str(script), '--job-id', 'job', '--attempt-id', 'attempt',
                    '--source-sha256', 'wrong', '--source-kind', 'file', '--input', str(video)],
                   cwd=tmp_path, capture_output=True, timeout=30)
    assert wrong.returncode != 0


def test_newer_schema_is_not_downgraded(tmp_path):
    path = tmp_path / 'future.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE cloud_schema(version INTEGER NOT NULL)')
        db.execute('INSERT INTO cloud_schema VALUES (999)')
    with pytest.raises(CloudOwnershipError, match='newer_than_application'):
        CloudJobStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version FROM cloud_schema').fetchone()[0] == 999


def test_failed_cleanup_after_publication_blocks_receipt_and_admission(store, source, tmp_path):
    job = claimed(store, source)
    provider = FakeProvider(store, job, stop_ok=False)
    result = orchestrator(store, provider, tmp_path).process_job(job)
    assert result.status == 'stop_incomplete' and not result
    assert store.get_job(job.job_id).state == 'completed'
    assert store.get_receipt(job.job_id) is None
    store.enqueue(str(source))
    assert store.claim_next() is None


def test_source_changed_during_upload_does_not_publish(store, source, tmp_path):
    job = claimed(store, source)
    def change_source(phase):
        if phase == 'upload': source.write_bytes(b'changed-source')
    provider = FakeProvider(store, job, hook=change_source)
    result = orchestrator(store, provider, tmp_path).process_job(job)
    assert result.status == 'error' and result.error == 'cloud_source_changed'
    assert not (tmp_path / 'outputs' / ('cloud_' + source.name)).exists()


def test_cancel_route_persists_intent_without_claiming_cleanup(store, source, monkeypatch):
    from backend.cloud import cloud_routes
    job = claimed(store, source)
    monkeypatch.setattr(cloud_routes, 'store', store)
    outcome = asyncio.run(cloud_routes.api_cloud_job_cancel({'job_id': job.job_id}))
    assert outcome['accepted'] and not outcome['cancelled'] and outcome['state'] == 'cancel_requested'
    assert store.get_job(job.job_id).cancel_requested_at is not None


def test_stop_route_only_requests_owned_jobs(store, source, monkeypatch):
    from backend.cloud import cloud_routes
    job = claimed(store, source)
    monkeypatch.setattr(cloud_routes, 'store', store)
    monkeypatch.setattr(cloud_routes, 'get_sessions', lambda: pytest.fail('must not enumerate provider sessions'))
    outcome = asyncio.run(cloud_routes.api_cloud_stop())
    assert outcome['jobs'] == [{'accepted': True, 'state': 'cancel_requested', 'job_id': job.job_id}]


def test_worker_stop_route_does_not_report_drained_worker_early(monkeypatch):
    from backend.cloud import cloud_routes
    worker = SimpleNamespace(stop=lambda: False, is_running=lambda: True)
    monkeypatch.setattr(cloud_routes, 'get_cloud_worker', lambda: worker)
    outcome = asyncio.run(cloud_routes.api_cloud_worker_stop())
    assert outcome == {'status': 'stopping', 'worker_running': True}


def test_cloud_api_exposes_incomplete_cleanup_without_hiding_committed_output(store, source, tmp_path, monkeypatch):
    from backend.cloud import cloud_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    job = claimed(store, source)
    result = orchestrator(store, FakeProvider(store, job, stop_ok=False), tmp_path).process_job(job)
    assert result.status == 'stop_incomplete'
    monkeypatch.setattr(cloud_routes, 'store', store)
    monkeypatch.setattr(cloud_routes, 'get_cloud_worker', lambda: SimpleNamespace(
        get_current_job=lambda: None, is_running=lambda: False))
    app = FastAPI()
    app.include_router(cloud_routes.router)
    with TestClient(app) as client:
        jobs = client.get('/api/cloud/jobs').json()['jobs']
        active = client.get('/api/cloud/status').json()['active_job']
        assert len(jobs) == 1
        for public in (jobs[0], active):
            assert public['state'] == 'stop_incomplete'
            assert public['publication_state'] == 'completed'
            assert public['output_committed'] is True
            assert public['session_cleanup'] == 'unconfirmed'
            assert 'publication_json' not in public and 'receipt_json' not in public
        assert store.get_job(job.job_id).state == 'completed'
        assert store.get_receipt(job.job_id) is None
        store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
        confirmed = client.get('/api/cloud/jobs').json()['jobs'][0]
        assert confirmed['state'] == 'completed' and confirmed['output_committed'] is True
        assert store.get_receipt(job.job_id)
