"""Isolated queue/API endurance exercise; no model, GPU or Telegram delivery.

Default: 24 hours of measured polling; --duration-seconds 60 is a smoke run.
Each segment runs a fresh API process, injects storage/ACK/corruption faults,
then exits abruptly with an active fixture job. The next process must retain
that job for review before reconciling a synthetic receipt. Continuous mode keeps
one process alive and checks resources after warmup and a final idle. Generated
CPU media exercises batch/local-file lifecycles; providers and GPU stay stubbed.
"""
from __future__ import annotations

import argparse
from collections import deque
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import shutil
import threading
import sys
import tempfile
import time
import statistics
import uuid

ROOT = Path(__file__).resolve().parents[1]
CRASH_EXIT = 23
MAX_SAMPLES = 256
MARKER = '.dashboard-soak-fixture'
MAX_POLL_INTERVAL = 10.0
MAX_REQUEST_SECONDS = 5.0
STDERR_LIMIT = 64 * 1024
WARMUP_SECONDS = 1800
IDLE_SECONDS = 120
P95_LIMIT_MS = 500
RESOURCE_WINDOW_TOLERANCE_SECONDS = 20


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def code_inventory():
    files = [Path(__file__), ROOT / 'tools/soak_workload.py', *sorted((ROOT / 'backend/templates').glob('*.html'))]
    files += sorted((ROOT / 'backend').rglob('*.py'))
    files = [path for path in files if path.is_file() and not any(p in ('venv', '__pycache__') for p in path.parts)]
    hashes = {str(path.relative_to(ROOT)): file_hash(path) for path in files}
    return {'sha256': hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), 'files': hashes}


def record_latency(report, elapsed):
    # Fixed 1ms histogram through the 5s request budget, bounded for 24h runs.
    bucket = str(min(5001, max(1, math.ceil(elapsed * 1000))))
    counts = report.setdefault('latency_histogram_ms', {})
    counts[bucket] = counts.get(bucket, 0) + 1


def latency_p95(histogram):
    target = math.ceil(sum(histogram.values()) * .95)
    seen = 0
    for value, count in sorted((int(k), v) for k, v in histogram.items()):
        seen += count
        if seen >= target:
            return value
    return None


class ResourceWindow:
    """Ten-second samples: warmup baseline plus a bounded rolling hour."""
    def __init__(self, warmup=WARMUP_SECONDS):
        self.warmup = warmup
        self.baseline = []
        self.last_hour = deque(maxlen=361)
        self.last_time = -10

    def observe(self, sample):
        elapsed = sample['elapsed_seconds']
        if elapsed - self.last_time < 10:
            return
        self.last_time = elapsed
        self.last_hour.append(dict(sample))
        while self.last_hour and self.last_hour[0]['elapsed_seconds'] < elapsed - 3600:
            self.last_hour.popleft()
        if self.warmup <= elapsed < self.warmup + 300:
            self.baseline.append(dict(sample))

    def evaluate(self, idle=None, idle_seconds=0):
        if not self.baseline:
            return {'status': 'not_observed', 'reason': 'Warmup baseline not yet observed',
                    'warmup_seconds': self.warmup, 'sample_interval_seconds': 10}
        baseline = {k: statistics.median(s[k] for s in self.baseline) for k in ('rss_bytes', 'threads', 'handles')}
        rss = statistics.median(s['rss_bytes'] for s in self.last_hour)
        allowance = max(32 * 1024 ** 2, baseline['rss_bytes'] * .20)
        points = list(self.last_hour)
        span = points[-1]['elapsed_seconds'] - points[0]['elapsed_seconds']
        mean_t = statistics.mean(s['elapsed_seconds'] for s in points)
        mean_rss = statistics.mean(s['rss_bytes'] for s in points)
        variance = sum((s['elapsed_seconds'] - mean_t) ** 2 for s in points)
        slope = (sum((s['elapsed_seconds']-mean_t)*(s['rss_bytes']-mean_rss) for s in points)
                 / variance * 3600) if variance else 0
        gates = {'rss_median': rss <= baseline['rss_bytes'] + allowance,
                 'rss_trend': span < 1800 or slope <= allowance,
                 'idle_threads': idle is not None and idle['threads'] <= baseline['threads'] + 2,
                 'idle_handles': idle is not None and idle['handles'] <= baseline['handles'] + 20}
        # Ten-second sampling follows real polling, so the first sample inside
        # the exact rolling hour can be up to one sampling interval late.
        ready = (idle_seconds >= IDLE_SECONDS and len(self.baseline) >= 25
                 and span >= 3600 - RESOURCE_WINDOW_TOLERANCE_SECONDS)
        return {'status': ('passed' if all(gates.values()) else 'failed') if ready else 'not_observed',
                'baseline': baseline, 'baseline_samples': len(self.baseline),
                'last_hour_median_rss_bytes': rss, 'last_hour_samples': len(points),
                'last_hour_observed_seconds': span, 'rss_allowance_bytes': allowance,
                'window_sampling_tolerance_seconds': RESOURCE_WINDOW_TOLERANCE_SECONDS,
                'rss_slope_bytes_per_hour': slope, 'idle_seconds': idle_seconds, 'idle_sample': idle,
                'checks': gates, 'warmup_seconds': self.warmup, 'sample_interval_seconds': 10}


def write_report(path, data):
    # The shared atomic writer retries Windows sharing violations and fsyncs.
    backend = str(ROOT / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    from pipeline_v2.atomic_io import atomic_write_json
    atomic_write_json(path, data)


def read_report(path):
    for attempt in range(5):
        try:
            return json.loads(Path(path).read_text(encoding='utf-8'))
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.05 * (attempt + 1))


def append_segment_summary(path, run_started_at, index, result):
    # One compact durable record per segment remains after recent samples roll off.
    summary = {key: value for key, value in result.items() if key not in ('samples', 'stderr_tail')}
    summary.update(run_started_at=run_started_at, segment_index=index,
                   stderr_tail_bytes=len(result.get('stderr_tail', '').encode('utf-8')))
    for attempt in range(5):
        try:
            handle = Path(path).open('a', encoding='utf-8')
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.05 * (attempt + 1))
    with handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def checked_get(client, endpoint, report, expected_status=200):
    report['last_endpoint'] = endpoint
    started = time.monotonic()
    response = client.get(endpoint)
    payload = response.json()
    latency = time.monotonic() - started
    from tools.soak_workload import record_timing
    kind = '/api/queue/operations/<id>' if endpoint.startswith('/api/queue/operations/') else endpoint
    kind = '/api/batches/<id>' if kind.startswith('/api/batches/') else kind
    record_timing(report, 'get:' + kind, latency)
    record_latency(report, latency)
    report['max_request_seconds'] = max(report.get('max_request_seconds', 0), latency)
    _require(latency <= MAX_REQUEST_SECONDS, f'{endpoint}: request exceeded {MAX_REQUEST_SECONDS}s budget ({latency:.3f}s)')
    _require(response.status_code == expected_status, f'{endpoint}: HTTP {response.status_code}')
    return payload


def record_poll_coverage(report, elapsed, gap, gap_budget):
    report['observed_seconds'] = elapsed
    report['covered_seconds'] += min(gap, gap_budget)
    report['max_poll_gap_seconds'] = max(report['max_poll_gap_seconds'], gap)
    report['polling_coverage_ratio'] = min(1, report['covered_seconds'] / elapsed) if elapsed else 0
    _require(gap <= gap_budget, f'Polling gap exceeded {gap_budget}s budget ({gap:.3f}s)')


class PollingObserver:
    """Read all six endpoints between workload phases using one elapsed clock."""
    endpoints = ('/api/status', '/api/queue', '/api/phoi', '/api/banve', '/api/logs', '/health/ready')

    def __init__(self, client, report, interval):
        self.client, self.report, self.interval = client, report, interval
        self.started = self.previous_poll = time.monotonic()

    def poll(self, active_identity=None):
        for endpoint in self.endpoints:
            payload = checked_get(self.client, endpoint, self.report)
            if active_identity is not None and endpoint == '/api/queue':
                _require([item['job_id'] for item in payload['active_jobs']] == [active_identity],
                         'Polling did not observe the active queue identity')
                _require(payload['counters']['running'] == 1 and payload['available'],
                         'Active queue was not available')
            elif active_identity is not None and endpoint == '/api/status':
                _require(payload['active'] and payload['occupies_slot']
                         and payload['execution_id'] == active_identity,
                         'Polling did not observe active tracker lease')
            self.report['requests'] += 1
        self.report['polls'] += 1
        now = time.monotonic()
        self.report['last_poll_phases'] = dict(self.report.get('current_poll_phases', {}))
        record_poll_coverage(self.report, now - self.started, now - self.previous_poll,
                             self.interval + MAX_REQUEST_SECONDS)
        self.report['current_poll_phases'] = {}
        self.previous_poll = now


def isolated_environment(runtime):
    runtime = Path(runtime).resolve()
    if not (runtime / MARKER).is_file():
        raise ValueError('Soak runtime must be a marked, generated fixture')
    env = dict(os.environ)
    for key, folder in {'AUTODUB_WORKSPACE': 'workspace', 'AUTODUB_INPUT_DIR': 'input',
                        'AUTODUB_OUTPUT_DIR': 'output', 'AUTODUB_DATA_DIR': 'data',
                        'AUTODUB_MODEL_CACHE': 'models', 'AUTODUB_SHARED_ASSETS_DIR': 'assets'}.items():
        path = runtime / folder
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    (runtime / 'workspace' / 'bot_system').mkdir(exist_ok=True)
    log = runtime / 'fixture.log'
    if not log.exists():
        log.write_text('Isolated dashboard soak fixture\n', encoding='utf-8')
    env.update(AUTODUB_TESTING='1', AUTODUB_LOCAL_TOKEN='isolated-soak-fixture-token',
               AUTODUB_LOG_FILE=str(log), PIPELINE_MODE='legacy', PYTHONDONTWRITEBYTECODE='1',
               PYTHONIOENCODING='utf-8', AUTODUB_EXPERIMENTAL_DUAL_WORKERS='0',
               AUTODUB_CONTROL_DIR=str(runtime / 'workspace' / 'bot_system' / 'control'),
               TOOL_V1_CONTROL_DIR=str(runtime / 'workspace' / 'bot_system' / 'control'))
    return env


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def memory_sample(process, elapsed):
    return {'elapsed_seconds': round(elapsed, 3), 'rss_bytes': process.memory_info().rss,
            'threads': process.num_threads(),
            'handles': process.num_handles() if hasattr(process, 'num_handles') else process.num_fds()}


def child_run(runtime, report_path, seconds, interval, verify_only=False, mode='restart', idle_seconds=0, workload_every=10):
    # Imports follow isolation so no backend module can bind a live workspace.
    os.environ.update(isolated_environment(runtime))
    sys.path.insert(0, str(ROOT / 'backend'))
    sys.path.insert(0, str(ROOT))
    instance = uuid.uuid4().hex
    report = {'passed': False, 'polls': 0, 'requests': 0, 'queue_cycles': 0,
              'fault_checks': 0, 'recovery_checks': 0, 'observed_seconds': 0,
              'samples': [], 'error': None, 'covered_seconds': 0, 'max_request_seconds': 0,
              'max_poll_gap_seconds': 0, 'polling_coverage_ratio': 0,
              'request_budget_seconds': MAX_REQUEST_SECONDS, 'poll_gap_budget_seconds': interval + MAX_REQUEST_SECONDS,
              'process_id': os.getpid(), 'instance_id': instance, 'execution_mode': mode,
              'latency_histogram_ms': {}, 'workload_checks': 0, 'handoff_checks': 0, 'pause_checks': 0, 'queue_command_checks': 0,
              'expected_faults': ['queue_storage_failure', 'corrupt_snapshot', 'lost_command_ack']}
    started = time.monotonic()
    try:
        import psutil
        report['host'] = {'platform': platform.platform(), 'architecture': platform.machine(),
                          'python': sys.version, 'cpu_count': os.cpu_count(),
                          'physical_memory_bytes': psutil.virtual_memory().total}
        from unittest.mock import patch
        from tools.soak_workload import (FixtureWorkload, persistence_inventory, verify_persistence,
                                        inventory_summary, isolate_control_paths, verify_control_isolation,
                                        timed, record_timing)
        # Install the boundary before stop_service/video_pause bind their imports.
        isolate_control_paths(runtime)
        import main as api
        import job_tracker
        import telegram_queue_monitor as monitor
        from fastapi.testclient import TestClient
        from pipeline_v2.atomic_io import atomic_write_bytes, atomic_write_json
        from queue_control_service import QueueStateError, read_operation, read_snapshot, submit_command
        report['control_isolation'] = verify_control_isolation(runtime)
        queue = monitor.MonitoredQueue()
        ledger_path = Path(runtime) / 'expected-persistence.json'
        before_restore = persistence_inventory(Path(runtime), queue)
        if ledger_path.exists():
            prior = read_report(ledger_path)
            verify_persistence(prior, before_restore)
            report['previous_segment_ledger_sha256'] = prior['identity_ledger_sha256']
        report['persistence_before'] = inventory_summary(before_restore)
        recovered = queue.restore()
        if queue.active_jobs:
            _require(recovered['recovery_state'] == 'needs_review', 'Interrupted job was not held for review')
            before = queue.snapshot.read_bytes()
            try:
                queue.get_nowait()
            except QueueStateError:
                pass
            else:
                raise AssertionError('Interrupted active job was claimable')
            _require(queue.snapshot.read_bytes() == before, 'Blocked claim changed interrupted state')
            active_ids = {job['job_id'] for job in queue.active_jobs}
            _require(all(jid.startswith('crash_fixture_') for jid in active_ids), 'Unexpected interrupted identity')
            for job in queue.active_jobs:
                _require(job['type'] == 'local_file' and Path(job['video_path']).is_file(), 'Interrupted local_file source was lost')
            # Explicit synthetic receipt: this is a fixture reconciliation, not delivery.
            queue.restore(delivered=lambda job: job['job_id'] in active_ids)
            _require(not queue.active_jobs and queue.recovery_state == 'ready', 'Receipt reconciliation failed')
            report['recovery_checks'] += 1
        _require(queue.qsize() == 0, 'Unexpected pending job after restart')
        process = psutil.Process()
        samples = deque(maxlen=MAX_SAMPLES)
        resource = ResourceWindow()
        workload = FixtureWorkload(Path(runtime), queue, report)
        before_inventory = persistence_inventory(Path(runtime), queue)
        verify_persistence(before_restore, before_inventory)
        report['persistence_restored'] = inventory_summary(before_inventory)
        with TestClient(api.app, headers={'X-Local-Control-Token': api.LOCAL_TOKEN}) as client:
            workload.bind(client, api, checked_get)
            faults_started = time.monotonic()
            # Exercise persistence-before-memory on a real queue writer failure.
            before = queue.snapshot.read_bytes()
            with patch.object(monitor, 'atomic_write_json', side_effect=OSError('injected fixture storage failure')):
                try:
                    queue.put_nowait({'job_id': 'write_failure_' + instance, 'type': 'url', 'url': 'https://example.invalid/fail'})
                except OSError:
                    pass
                else:
                    raise AssertionError('Injected storage failure was accepted')
            _require(queue.qsize() == 0 and queue.snapshot.read_bytes() == before, 'Failed write published membership')
            queue.restore()
            report['fault_checks'] += 1

            # The real API must fail closed on unreadable queue bytes and recover.
            good = queue.snapshot.read_bytes()
            atomic_write_bytes(queue.snapshot, b'{invalid-fixture')
            checked_get(client, '/api/queue', report, 503)
            checked_get(client, '/health/ready', report, 503)
            _require(queue.snapshot.read_bytes() == b'{invalid-fixture', 'Poll overwrote corrupt snapshot')
            atomic_write_bytes(queue.snapshot, good)
            queue.restore()
            report['fault_checks'] += 1

            # Crash between snapshot commit and command ACK; receipt prevents replay.
            ack_id, operation_id = 'ack_' + instance, 'delete_' + instance
            queue.put_nowait({'job_id': ack_id, 'type': 'url', 'url': 'https://example.invalid/ack'})
            prior = read_operation(queue.snapshot, operation_id)
            if prior is None:
                submit_command(queue.snapshot, 'delete', [ack_id], operation_id=operation_id)
                actual_write = monitor.atomic_write_json
                def fail_ack(path, data):
                    if Path(path).parent.name == 'queue_commands':
                        raise OSError('injected command ACK failure')
                    return actual_write(path, data)
                with patch.object(monitor, 'atomic_write_json', side_effect=fail_ack):
                    try:
                        queue.apply_commands()
                    except OSError:
                        pass
                    else:
                        raise AssertionError('ACK fault was not injected')
                _require(read_operation(queue.snapshot, operation_id)['state'] == 'applied', 'Committed receipt lost')
                queue.restore()
                _require(queue.qsize() == 0, 'ACK replay resurrected deleted job')
                report['fault_checks'] += 1
            else:
                # The same deletion identity remains tombstoned across process restarts.
                raise AssertionError('Unexpected reusable ACK fixture')

            report['intentional_fault_seconds'] = time.monotonic() - faults_started
            if not verify_only:
                workload.handoff()

            observer = PollingObserver(client, report, interval)
            workload.poll_checkpoint = observer.poll
            report['setup_seconds'] = observer.started - started
            # A final successful poll follows the requested duration; unobserved
            # trailing sleeps or request stalls cannot satisfy the elapsed gate.
            while not verify_only:
                cycle_id = f'cycle_{instance}_{report["queue_cycles"]}'
                if report['queue_cycles'] % workload_every == 0:
                    observer.poll()
                    with timed(report, 'workload:total', include_in_poll_gap=False):
                        workload.cycle(cycle_id)
                queue_started = time.monotonic()
                queue.put_nowait({'job_id': cycle_id, 'type': 'url', 'url': 'https://example.invalid/cycle',
                                  'filename': 'Thử nghiệm Unicode.mp4'})
                job = queue.get_nowait()
                _require(job['job_id'] == cycle_id, 'Claim identity changed')
                queue.task_done(job['job_id'], retry={**job, 'retry_count': 1})
                retried = queue.get_nowait()
                _require(retried['job_id'] == job['job_id'], 'Retry identity changed')
                job_tracker.start_batch(1, job_id=job['job_id'])
                job_tracker.start_video(job['filename'])
                job_tracker.update_step(1, 'Synthetic active fixture', percent=10)
                queue.publish()
                record_timing(report, 'queue:claim_retry_tracker', time.monotonic() - queue_started)
                observer.poll(cycle_id)
                elapsed = report['observed_seconds']
                cleanup_started = time.monotonic()
                sample = memory_sample(process, elapsed)
                report.setdefault('first_sample', sample)
                report['last_sample'] = sample
                samples.append(sample)
                resource.observe(sample)
                report['samples'] = list(samples)
                report['resource_gate'] = resource.evaluate()
                for metric in ('rss_bytes', 'threads', 'handles'):
                    report[metric + '_peak'] = max(report.get(metric + '_peak', 0), sample[metric])
                # Only after observing both active states may the fixture drain.
                queue.task_done(retried['job_id'])
                job_tracker.mark_stopped('Synthetic fixture observed; no media render')
                report['queue_cycles'] += 1
                record_timing(report, 'queue:cleanup_resource_sample', time.monotonic() - cleanup_started)
                with timed(report, 'report:write'):
                    write_report(report_path, report)
                # Include final cleanup/report writes in observed coverage too;
                # otherwise their stalls could be mislabeled as teardown.
                observer.poll()
                elapsed = report['observed_seconds']
                if elapsed >= seconds:
                    break
                with timed(report, 'loop:sleep'):
                    time.sleep(min(interval, max(0, seconds - elapsed)))
            report['polling_validated'] = verify_only or (report['polls'] > 0 and report['polling_coverage_ratio'] >= .999999)
            if not verify_only and mode == 'continuous' and idle_seconds:
                # Idle is measured separately and never counts toward workload duration.
                idle_started = time.monotonic()
                while time.monotonic() - idle_started < idle_seconds:
                    report['idle_observed_seconds'] = time.monotonic() - idle_started
                    write_report(report_path, report)
                    time.sleep(min(1, max(0, idle_seconds - report['idle_observed_seconds'])))
                report['idle_observed_seconds'] = time.monotonic() - idle_started
            report['resource_gate'] = resource.evaluate(memory_sample(process, report['observed_seconds']),
                                                        report.get('idle_observed_seconds', 0))
        _require(not queue.active_jobs and queue.qsize() == 0, 'Queue did not drain')
        report['final_drain_verified'] = True
        if not verify_only and mode == 'restart':
            crash_id = 'crash_fixture_' + instance
            crash_job = {'job_id': crash_id, 'type': 'local_file',
                         'video_path': str(workload.seed), 'filename': workload.seed.name}
            from telegram_local_source import capture_source
            capture_source(crash_job)
            queue.put_nowait(crash_job)
            _require(queue.get_nowait()['job_id'] == crash_id, 'Crash fixture was not active')
        after_inventory = persistence_inventory(Path(runtime), queue)
        verify_persistence(before_inventory, after_inventory, report['workload_checks'] * 2)
        report['persistence_after'] = inventory_summary(after_inventory)
        for metric in ('receipt_count', 'tombstone_count', 'command_count', 'batch_count'):
            _require(report['persistence_after'][metric] >= before_inventory[metric], 'Durable history was reset: ' + metric)
        report['api_read_p95_ms'] = latency_p95(report['latency_histogram_ms'])
        _require(report['api_read_p95_ms'] < P95_LIMIT_MS, 'API read P95 exceeded 500ms')
        report.update(passed=True, wall_seconds=time.monotonic() - started)
        report['teardown_seconds'] = max(0, report['wall_seconds'] - report['setup_seconds']
                                        - report['observed_seconds'] - report.get('idle_observed_seconds', 0))
        report['planned_downtime_seconds'] = report['setup_seconds'] + report['teardown_seconds'] + report.get('idle_observed_seconds', 0)
        write_report(ledger_path, after_inventory)
        write_report(report_path, report)
        if not verify_only and mode == 'restart':
            os._exit(CRASH_EXIT)
        return 0
    except BaseException as exc:
        report.update(error=f'{type(exc).__name__}: {exc}', wall_seconds=time.monotonic() - started)
        write_report(report_path, report)
        return 1


def completion_fields(report):
    completed = (not report['errors'] and report['observed_seconds'] >= report['requested_seconds']
                 and report['segments'] > 0 and report.get('final_recovery_verified', False)
                 and report.get('polling_validated', False) and report.get('polling_coverage_ratio', 0) >= .999999)
    if report.get('schema_version', 0) >= 3:
        completed = completed and report.get('workload_checks', 0) > 0 and report.get('code_unchanged', False)
        completed = completed and (report.get('api_read_p95_ms') or 5001) < P95_LIMIT_MS
        completed = completed and report.get('resource_gate', {}).get('status') != 'failed'
        if report['requested_seconds'] >= 86400 and report.get('execution_mode') == 'continuous':
            completed = completed and report.get('resource_gate', {}).get('status') == 'passed'
    return {'passed': bool(completed), 'soak_completed': bool(completed and report['observed_seconds'] >= 86400),
            'mode': 'soak_24h' if report['requested_seconds'] >= 86400 else 'smoke'}


def supervise_child(command, env, seconds, child_report, output, report, started):
    process = None
    reader = None
    chunks = deque(maxlen=STDERR_LIMIT // 1024)
    runner_error = None
    returncode = None
    def read_stderr(pipe):
        try:
            while True:
                chunk = pipe.read(1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            pipe.close()
    try:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if getattr(process, 'stderr', None) is not None:
            reader = threading.Thread(target=read_stderr, args=(process.stderr,), daemon=True)
            reader.start()
        deadline = time.monotonic() + seconds + 120
        last_saved = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Soak child exceeded its segment deadline')
            try:
                returncode = process.wait(timeout=min(1, remaining))
                break
            except subprocess.TimeoutExpired:
                if child_report.exists() and time.monotonic() - last_saved >= 10:
                    report['current_segment'] = read_report(child_report)
                    report['wall_seconds'] = time.monotonic() - started
                    write_report(output, report)
                    last_saved = time.monotonic()
    except BaseException as exc:
        runner_error = f'{type(exc).__name__}: {exc}'
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            returncode = process.wait(timeout=15)
        if reader is not None:
            reader.join(timeout=5)
    result = read_report(child_report) if child_report.exists() else {
        'passed': False, 'error': f'Child exited {returncode} without evidence'}
    result['returncode'] = returncode
    tail = b''.join(chunks)[-STDERR_LIMIT:].decode('utf-8', errors='replace')
    result['stderr_tail'] = tail.encode('utf-8')[-STDERR_LIMIT:].decode('utf-8', errors='ignore')
    if runner_error:
        result.update(passed=False, error=runner_error)
    return result


def remove_generated_runtime(runtime):
    # This is the only recursive removal, and only after successful acceptance.
    runtime = runtime.resolve()
    if (runtime.parent != Path(tempfile.gettempdir()).resolve() or not runtime.name.startswith('dashboard_soak_')
            or not (runtime / MARKER).is_file()):
        raise ValueError('Refusing to remove an unverified fixture directory')
    shutil.rmtree(runtime)


def run(duration_seconds, restart_seconds, poll_interval, output, *, mode='restart', idle_seconds=0, workload_every=10):
    for name, value in (('duration', duration_seconds), ('restart', restart_seconds), ('poll', poll_interval)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{name} must be a positive finite number')
    if poll_interval > MAX_POLL_INTERVAL:
        raise ValueError(f'poll interval must be at most {MAX_POLL_INTERVAL} seconds')
    if mode not in ('restart', 'continuous') or not math.isfinite(idle_seconds) or idle_seconds < 0:
        raise ValueError('Invalid execution mode or idle duration')
    if type(workload_every) is not int or workload_every < 1:
        raise ValueError('workload_every must be a positive integer')
    output = Path(output)
    summary_path = output.with_suffix('.segments.jsonl')
    started = time.monotonic()
    report = {'schema_version': 3, 'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'requested_seconds': duration_seconds, 'observed_seconds': 0, 'wall_seconds': 0,
              'covered_seconds': 0, 'polling_validated': True, 'polling_coverage_ratio': 0,
              'max_request_seconds': 0, 'max_poll_gap_seconds': 0,
              'request_budget_seconds': MAX_REQUEST_SECONDS, 'poll_gap_budget_seconds': poll_interval + MAX_REQUEST_SECONDS,
              'segments': 0, 'forced_exits': 0, 'polls': 0, 'requests': 0, 'queue_cycles': 0,
              'fault_checks': 0, 'recovery_checks': 0, 'errors': [], 'recent_segments': [],
              'workload_checks': 0, 'handoff_checks': 0, 'pause_checks': 0, 'queue_command_checks': 0, 'mutation_requests': 0,
              'planned_downtime_seconds': 0, 'child_wall_seconds': 0,
              'execution_mode': mode, 'latency_histogram_ms': {}, 'code_inventory': code_inventory(),
              'idle_seconds_requested': idle_seconds, 'warmup_seconds': WARMUP_SECONDS,
              'passed': False, 'soak_completed': False, 'isolated': True,
              'production_render': False, 'real_telegram_delivery': False, 'gpu_exercised': False,
              'segment_summaries': str(summary_path.resolve()),
              'limitations': ['Synthetic CPU media and stub provider; no real model/GPU/Telegram delivery.',
                             'Warmup/hour/idle resource gates need a sufficiently long continuous run.'],
              'scope': 'Real queue/API/batch lifecycle, local_file dispatch, cross-process ownership, durable fault recovery and generated media listings'}
    write_report(output, report)
    runtime = None
    try:
        runtime = Path(tempfile.mkdtemp(prefix='dashboard_soak_'))
        (runtime / MARKER).write_text('generated-only', encoding='utf-8')
        env = isolated_environment(runtime)
        child_report = runtime / 'child-report.json'
        attempt = 0
        while report['observed_seconds'] < duration_seconds or not report.get('final_recovery_verified'):
            final = report['observed_seconds'] >= duration_seconds
            seconds = min(restart_seconds if mode == 'restart' else duration_seconds,
                          duration_seconds - report['observed_seconds']) if not final else 0
            child_report.unlink(missing_ok=True)
            command = [sys.executable, '-B', str(Path(__file__).resolve()), '--child-runtime', str(runtime),
                       '--output', str(child_report), '--duration-seconds', str(seconds),
                       '--poll-interval', str(poll_interval), '--execution-mode', mode,
                       '--idle-seconds', str(idle_seconds), '--workload-every', str(workload_every)]
            if final:
                command.append('--verify-only')
            result = supervise_child(command, env, seconds + (idle_seconds if mode == 'continuous' else 0),
                                     child_report, output, report, started)
            attempt += 1
            append_segment_summary(summary_path, report['started_at'], attempt, result)
            report.pop('current_segment', None)
            report['recent_segments'] = (report['recent_segments'] + [result])[-16:]
            for key in ('max_request_seconds', 'max_poll_gap_seconds'):
                report[key] = max(report[key], result.get(key, 0))
            expected = 0 if final or mode == 'continuous' else CRASH_EXIT
            if result['returncode'] != expected or not result['passed']:
                raise RuntimeError(result.get('error') or f"Unexpected child exit {result['returncode']}")
            _require(result.get('polling_validated'), 'Child did not validate polling coverage')
            report['observed_seconds'] += result['observed_seconds']
            report['covered_seconds'] += result['covered_seconds']
            report['polling_coverage_ratio'] = min(1, report['covered_seconds'] / report['observed_seconds'])
            for bucket, count in result.get('latency_histogram_ms', {}).items():
                report['latency_histogram_ms'][bucket] = report['latency_histogram_ms'].get(bucket, 0) + count
            report['api_read_p95_ms'] = latency_p95(report['latency_histogram_ms'])
            report['corpus'] = result.get('corpus', {})
            report['host'] = result.get('host', {})
            if report.get('persistence_after'):
                _require(result.get('previous_segment_ledger_sha256') == report['persistence_after']['identity_ledger_sha256'],
                         'Previous segment persistence ledger was not reconciled')
            report['persistence_after'] = result.get('persistence_after', {})
            report['resource_gate'] = result.get('resource_gate', {})
            report['planned_downtime_seconds'] += result.get('planned_downtime_seconds', 0)
            report['child_wall_seconds'] += result.get('wall_seconds', 0)
            if final:
                _require(result['recovery_checks'] == 1, 'Final interrupted job was not reconciled')
                report['final_recovery_verified'] = True
            else:
                report['segments'] += 1
                report['forced_exits'] += int(mode == 'restart')
                if mode == 'continuous':
                    _require(result.get('final_drain_verified'), 'Continuous child did not drain')
                    report['final_recovery_verified'] = True
            for key in ('polls', 'requests', 'queue_cycles', 'fault_checks', 'recovery_checks',
                        'workload_checks', 'handoff_checks', 'pause_checks', 'queue_command_checks', 'mutation_requests'):
                report[key] += result.get(key, 0)
            for metric in ('rss_bytes', 'threads', 'handles'):
                key = metric + '_peak'
                report[key] = max(report.get(key, 0), result.get(key, 0))
            report['wall_seconds'] = time.monotonic() - started
            write_report(output, report)
            if final or mode == 'continuous':
                break
    except BaseException as exc:
        report['errors'].append(f'{type(exc).__name__}: {exc}')
    report['wall_seconds'] = time.monotonic() - started
    report['supervisor_overhead_seconds'] = max(0, report['wall_seconds'] - report['child_wall_seconds'])
    report['code_unchanged'] = code_inventory()['sha256'] == report['code_inventory']['sha256']
    if not report['code_unchanged']:
        report['errors'].append('Source code changed during the measured run')
    report.update(completion_fields(report))
    if runtime is not None:
        if report['passed']:
            try:
                remove_generated_runtime(runtime)
            except OSError as exc:
                report['retained_runtime'] = str(runtime)
                report['cleanup_warning'] = f'{type(exc).__name__}: {exc}'
        else:
            report['retained_runtime'] = str(runtime)
            tail = report['recent_segments'][-1].get('stderr_tail', '') if report['recent_segments'] else ''
            from pipeline_v2.atomic_io import atomic_write_bytes
            atomic_write_bytes(runtime / 'child-stderr-tail.log', tail.encode('utf-8')[-STDERR_LIMIT:])
    write_report(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration-seconds', type=float, default=86400)
    parser.add_argument('--restart-seconds', type=float, default=300)
    parser.add_argument('--poll-interval', type=float, default=1)
    parser.add_argument('--execution-mode', choices=('restart', 'continuous'), default='restart')
    parser.add_argument('--idle-seconds', type=float)
    parser.add_argument('--workload-every', type=int, default=10)
    parser.add_argument('--output', type=Path, default=ROOT / 'test_evidence/dashboard/soak.json')
    parser.add_argument('--child-runtime', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--verify-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    idle = args.idle_seconds if args.idle_seconds is not None else (IDLE_SECONDS if args.duration_seconds >= 120 else 0)
    if args.child_runtime:
        return child_run(args.child_runtime, args.output, args.duration_seconds, args.poll_interval, args.verify_only,
                         args.execution_mode, idle, args.workload_every)
    try:
        result = run(args.duration_seconds, args.restart_seconds, args.poll_interval, args.output,
                     mode=args.execution_mode, idle_seconds=idle, workload_every=args.workload_every)
    except ValueError as exc:
        parser.error(str(exc))
    # Windows redirected stdout can use cp1252 even for Unicode fixture paths.
    print(json.dumps({k: v for k, v in result.items() if k not in ('recent_segments', 'code_inventory')}, ensure_ascii=True))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
