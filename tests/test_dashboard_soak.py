import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import soak_dashboard as soak


def test_short_isolated_soak_exercises_crash_recovery_and_faults(tmp_path):
    output = tmp_path / 'smoke.json'
    result = soak.run(.6, .3, .05, output)
    assert result['passed'], result['errors']
    assert result['mode'] == 'smoke'
    assert result['soak_completed'] is False
    assert result['forced_exits'] >= 1
    assert result['recovery_checks'] == result['forced_exits']
    assert result['final_recovery_verified'] is True
    assert result['fault_checks'] == (result['segments'] + 1) * 3
    assert result['requests'] == result['polls'] * 6 > 0
    assert result['queue_cycles'] > 0
    assert result['polling_validated'] and result['polling_coverage_ratio'] == 1
    assert result['max_request_seconds'] <= soak.MAX_REQUEST_SECONDS
    assert result['max_poll_gap_seconds'] <= result['poll_gap_budget_seconds']
    summaries = [json.loads(line) for line in Path(result['segment_summaries']).read_text(encoding='utf-8').splitlines()]
    assert len(summaries) == result['segments'] + 1
    assert all('samples' not in item and 'stderr_tail' not in item for item in summaries)
    assert result['rss_bytes_peak'] > 0
    assert result['threads_peak'] > 0
    assert result['handles_peak'] > 0
    assert all(len(segment['samples']) <= soak.MAX_SAMPLES for segment in result['recent_segments'])
    assert all(segment['control_isolation']['verified'] for segment in result['recent_segments'])
    assert result['workload_checks'] == result['pause_checks'] == result['queue_command_checks'] > 0
    assert result['handoff_checks'] == result['segments']
    assert result['code_unchanged'] and result['corpus']['media_count'] >= 6
    assert result['api_read_p95_ms'] < 500
    assert result['planned_downtime_seconds'] > 0
    assert len({item['instance_id'] for item in summaries}) == len(summaries)
    for previous, current in zip(summaries, summaries[1:]):
        assert current['previous_segment_ledger_sha256'] == previous['persistence_after']['identity_ledger_sha256']
        assert current['persistence_before']['identity_ledger_sha256'] == previous['persistence_after']['identity_ledger_sha256']
        for metric in ('receipt_count', 'tombstone_count', 'command_count', 'batch_count'):
            assert current['persistence_before'][metric] >= previous['persistence_after'][metric]
    assert summaries[-1]['persistence_after']['receipt_count'] == len(summaries) + result['queue_command_checks']
    assert json.loads(output.read_text(encoding='utf-8')) == result


def test_continuous_soak_keeps_one_process_and_drains(tmp_path):
    result = soak.run(.5, .1, .05, tmp_path / 'continuous.json', mode='continuous')
    assert result['passed'], result['errors']
    assert result['segments'] == 1 and result['forced_exits'] == result['recovery_checks'] == 0
    assert result['final_recovery_verified'] and result['recent_segments'][0]['final_drain_verified']
    assert result['workload_checks'] == result['pause_checks'] == result['queue_command_checks'] > 0
    assert result['persistence_after']['receipt_count'] == 1 + result['queue_command_checks']
    assert result['persistence_after']['tombstone_count'] == 1 + result['queue_command_checks']
    assert result['handoff_checks'] == 1
    assert result['resource_gate']['status'] == 'not_observed'
    assert result['soak_completed'] is False


def test_fixture_boundary_rebinds_preloaded_control_aliases_and_preserves_external_stop(tmp_path, monkeypatch):
    import service_control
    from tools.soak_workload import isolate_control_paths
    runtime = tmp_path / 'fixture'
    runtime.mkdir()
    (runtime / soak.MARKER).write_text('generated-only', encoding='utf-8')
    control = runtime / 'workspace/bot_system/control'
    external = tmp_path / 'external-control'
    external.mkdir()
    sentinel = external / 'video.stop'
    sentinel.write_text('keep user stop intent', encoding='utf-8')
    original = lambda *args, **kwargs: [external]
    stop_alias = SimpleNamespace(control_directories=original)
    pause_alias = SimpleNamespace(control_directories=original)
    monkeypatch.setattr(service_control, 'control_directories', original)
    monkeypatch.setitem(sys.modules, 'stop_service', stop_alias)
    monkeypatch.setitem(sys.modules, 'video_pause', pause_alias)
    monkeypatch.setenv('AUTODUB_WORKSPACE', str(runtime / 'workspace'))
    monkeypatch.setenv('TOOL_V1_CONTROL_DIR', str(control))
    isolate_control_paths(runtime)
    for module in (service_control, stop_alias, pause_alias):
        assert module.control_directories(soak.ROOT) == [control]
        for directory in module.control_directories(soak.ROOT):
            (directory / 'video.stop').unlink(missing_ok=True)
    assert sentinel.read_text(encoding='utf-8') == 'keep user stop intent'


def test_cli_unicode_corpus_succeeds_on_windows_redirected_stdout(monkeypatch):
    payload = {'passed': True, 'corpus': {'Nguồn 中文.mp4': 'hash'}}
    monkeypatch.setattr(soak, 'run', lambda *a, **k: payload)
    monkeypatch.setattr(sys, 'argv', ['soak_dashboard.py', '--duration-seconds', '1'])
    raw = io.BytesIO()
    stdout = io.TextIOWrapper(raw, encoding='cp1252')
    monkeypatch.setattr(sys, 'stdout', stdout)
    assert soak.main() == 0
    stdout.flush()
    assert json.loads(raw.getvalue().decode('cp1252')) == payload


def test_latency_histogram_uses_all_requests_and_is_bounded():
    report = {}
    for _ in range(95):
        soak.record_latency(report, .0192)
    for _ in range(5):
        soak.record_latency(report, 4)
    assert soak.latency_p95(report['latency_histogram_ms']) == 20
    soak.record_latency(report, 60)
    assert soak.latency_p95(report['latency_histogram_ms']) == 4000
    assert sum(report['latency_histogram_ms'].values()) == 101
    assert max(map(int, report['latency_histogram_ms'])) == 5001


def test_slow_workload_is_attributable_without_loosening_polling_gate():
    from tools.soak_workload import record_timing
    report = {'observed_seconds': 0, 'covered_seconds': 0, 'max_poll_gap_seconds': 0}
    for _ in range(10000):
        record_timing(report, 'workload:local_file', 2)
    record_timing(report, 'workload:batch', 4)
    with pytest.raises(AssertionError, match='Polling gap'):
        soak.record_poll_coverage(report, 8, 8, 6)
    assert len(report['timings']) == 2
    assert report['timings']['workload:local_file']['count'] == 10000
    assert report['current_poll_phases'] == {'workload:local_file': 2, 'workload:batch': 4}


def test_polling_between_workload_phases_keeps_coverage_but_rejects_single_stall(monkeypatch):
    from tools.soak_workload import checkpointed_step, record_timing
    elapsed = [0]
    requests = []
    class Response:
        status_code = 200
        def json(self):
            return {}
    class Client:
        def get(self, endpoint):
            requests.append(endpoint)
            return Response()
    monkeypatch.setattr(soak.time, 'monotonic', lambda: elapsed[0])
    report = {'observed_seconds': 0, 'covered_seconds': 0, 'max_poll_gap_seconds': 0,
              'polls': 0, 'requests': 0}
    observer = soak.PollingObserver(Client(), report, 1)
    observer.poll()
    for phase_seconds in (2, 4, 3, 2):
        with checkpointed_step(report, 'batch:read_prepared', observer.poll):
            elapsed[0] += phase_seconds
    assert report['observed_seconds'] == 11 and report['polling_coverage_ratio'] == 1
    assert report['max_poll_gap_seconds'] == 4
    assert report['polls'] == 5 and report['requests'] == len(requests) == 30
    record_timing(report, 'workload:batch', 11, include_in_poll_gap=False)
    assert report['timings']['workload:batch']['last_seconds'] == 11
    assert report['current_poll_phases'] == {}
    with pytest.raises(AssertionError, match='Polling gap'):
        with checkpointed_step(report, 'batch:read_prepared', observer.poll):
            elapsed[0] += 7
    assert report['max_poll_gap_seconds'] == 7 and report['polling_coverage_ratio'] < 1
    assert report['last_poll_phases']['batch:read_prepared'] == 7
    assert 'workload:batch' not in report['last_poll_phases']


def test_active_identity_assertions_remain_separate_from_idle_polling(monkeypatch):
    monkeypatch.setattr(soak, 'checked_get', lambda *a: {})
    report = {'observed_seconds': 0, 'covered_seconds': 0, 'max_poll_gap_seconds': 0,
              'polls': 0, 'requests': 0}
    observer = soak.PollingObserver(None, report, 1)
    observer.poll()
    with pytest.raises((AssertionError, KeyError)):
        observer.poll('expected-active-job')


@pytest.mark.parametrize('stall_phase', ['cleanup', 'report_write'])
def test_final_cycle_cleanup_and_report_stalls_cannot_be_hidden_as_teardown(tmp_path, stall_phase):
    runtime = tmp_path / ('final_stall_' + stall_phase)
    runtime.mkdir()
    (runtime / soak.MARKER).write_text('generated-only', encoding='utf-8')
    output = runtime / 'report.json'
    script = r'''
import sys
from pathlib import Path
from tools import soak_dashboard as soak
from tools.soak_workload import isolate_control_paths

runtime, output, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(soak.ROOT / 'backend'))
isolate_control_paths(runtime)
import job_tracker

monotonic = soak.time.monotonic
offset = [0]
injected = [False]
soak.time.monotonic = lambda: monotonic() + offset[0]
original_write = soak.write_report
original_stop = job_tracker.mark_stopped

def inject():
    if not injected[0]:
        offset[0] += 7
        injected[0] = True

def write_report(path, report):
    result = original_write(path, report)
    if phase == 'report_write' and report.get('queue_cycles', 0) >= 1:
        inject()
    return result

def mark_stopped(reason):
    result = original_stop(reason)
    if phase == 'cleanup' and reason == 'Synthetic fixture observed; no media render':
        inject()
    return result

soak.write_report = write_report
job_tracker.mark_stopped = mark_stopped
raise SystemExit(soak.child_run(runtime, output, .01, 1, mode='continuous', idle_seconds=0))
'''
    result = subprocess.run([sys.executable, '-B', '-c', script, str(runtime), str(output), stall_phase],
                            cwd=soak.ROOT, env=soak.isolated_environment(runtime),
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 1, result.stdout + result.stderr
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['queue_cycles'] == 1
    assert report['passed'] is False
    assert 'Polling gap exceeded' in report['error'], report
    assert report['max_poll_gap_seconds'] >= 7
    assert report['polling_coverage_ratio'] < 1


@pytest.mark.parametrize('records', [{}, {'replacement': 'same-content'}, {'receipt': 'changed-content'}])
def test_durable_ledger_rejects_lost_replaced_or_changed_receipt(records):
    from tools.soak_workload import verify_persistence
    previous = {'identity_ledger': {'receipts': {'receipt': 'same-content'}}, 'tracker_history': ['old']}
    current = {'identity_ledger': {'receipts': records}, 'tracker_history': ['old']}
    with pytest.raises(AssertionError, match='lost or changed'):
        verify_persistence(previous, current)


def test_durable_ledger_permits_only_documented_tracker_retention():
    from tools.soak_workload import verify_persistence
    before = {'identity_ledger': {}, 'tracker_history': [str(i) for i in range(50)]}
    after = {'identity_ledger': {}, 'tracker_history': ['new1', 'new2', *before['tracker_history'][:48]]}
    verify_persistence(before, after, 2)
    with pytest.raises(AssertionError, match='history'):
        verify_persistence(before, after)
    with pytest.raises(AssertionError, match='history'):
        verify_persistence(before, {'identity_ledger': {}, 'tracker_history': []}, 2)


def resource_window(rss_growth_per_hour=0):
    window = soak.ResourceWindow()
    for elapsed in range(0, 7201, 10):
        rss = 100 * 1024 ** 2 + max(0, elapsed - 2100) / 3600 * rss_growth_per_hour
        window.observe({'elapsed_seconds': elapsed, 'rss_bytes': rss, 'threads': 8, 'handles': 100})
    return window


def test_resource_gate_needs_full_observation_and_idle():
    window = soak.ResourceWindow()
    window.observe({'elapsed_seconds': 100, 'rss_bytes': 100, 'threads': 8, 'handles': 100})
    idle = {'threads': 8, 'handles': 100}
    assert window.evaluate(idle, 120)['status'] == 'not_observed'
    window = resource_window()
    assert window.evaluate(idle, 119)['status'] == 'not_observed'
    result = window.evaluate(idle, 120)
    assert result['status'] == 'passed'
    assert result['baseline_samples'] == 30
    assert result['last_hour_observed_seconds'] == 3600
    assert len(window.last_hour) <= 361
    window.observe({'elapsed_seconds': 10000, 'rss_bytes': 100 * 1024 ** 2, **idle})
    assert all(item['elapsed_seconds'] >= 6400 for item in window.last_hour)


def test_resource_hour_accepts_polling_sample_alignment_with_reported_tolerance():
    window = soak.ResourceWindow()
    for step in range(635):
        window.observe({'elapsed_seconds': round(step * 11.36, 3),
                        'rss_bytes': 100 * 1024 ** 2, 'threads': 8, 'handles': 100})
    result = window.evaluate({'threads': 8, 'handles': 100}, 120)
    assert result['status'] == 'passed'
    assert 3580 <= result['last_hour_observed_seconds'] < 3590
    assert result['window_sampling_tolerance_seconds'] == 20
    assert result['checks']['rss_median'] and result['checks']['rss_trend']


@pytest.mark.parametrize('growth,threads,handles,failed', [
    (64 * 1024 ** 2, 8, 100, 'rss_trend'),
    (0, 11, 100, 'idle_threads'),
    (0, 8, 121, 'idle_handles'),
])
def test_resource_gate_rejects_growth_and_idle_leaks(growth, threads, handles, failed):
    result = resource_window(growth).evaluate({'threads': threads, 'handles': handles}, 120)
    assert result['status'] == 'failed'
    assert result['checks'][failed] is False


def test_current_completion_requires_workload_hash_latency_and_continuous_resources():
    report = {'schema_version': 3, 'errors': [], 'observed_seconds': 86400, 'requested_seconds': 86400,
              'segments': 1, 'final_recovery_verified': True, 'polling_validated': True,
              'polling_coverage_ratio': 1, 'workload_checks': 1, 'code_unchanged': True,
              'api_read_p95_ms': 20, 'execution_mode': 'continuous'}
    assert not soak.completion_fields(report)['soak_completed']
    report['resource_gate'] = {'status': 'passed'}
    assert soak.completion_fields(report)['soak_completed']
    for field, invalid in (('workload_checks', 0), ('code_unchanged', False), ('api_read_p95_ms', 500)):
        assert not soak.completion_fields({**report, field: invalid})['passed']
    assert not soak.completion_fields({**report, 'requested_seconds': 7200,
                                      'resource_gate': {'status': 'failed'}})['passed']


def test_failed_child_retains_failure_and_cannot_pass(tmp_path, monkeypatch):
    class FailedChild:
        def __init__(self, command, **kwargs):
            destination = command[command.index('--output') + 1]
            soak.write_report(destination, {'passed': False, 'error': 'injected assertion failure'})
            self.stderr = io.BytesIO(b'fixture stderr\n' * 10000)
        def wait(self, timeout=None):
            return 1
        def poll(self):
            return 1
    monkeypatch.setattr(subprocess, 'Popen', FailedChild)
    output = tmp_path / 'failure.json'
    result = soak.run(1, 1, .1, output)
    assert result['passed'] is False and result['soak_completed'] is False
    assert result['errors'] == ['RuntimeError: injected assertion failure']
    assert result['recent_segments'][0]['returncode'] == 1
    retained = Path(result['retained_runtime'])
    assert (retained / soak.MARKER).is_file()
    stderr = (retained / 'child-stderr-tail.log').read_bytes()
    assert 0 < len(stderr) <= soak.STDERR_LIMIT
    assert 'injected assertion failure' in Path(result['segment_summaries']).read_text(encoding='utf-8')
    soak.remove_generated_runtime(retained)
    assert json.loads(output.read_text(encoding='utf-8')) == result


def test_interruption_stops_only_owned_child_and_saves_failure(tmp_path, monkeypatch):
    children = []
    class InterruptedChild:
        def __init__(self, *args, **kwargs):
            self.killed = False
            children.append(self)
        def wait(self, timeout=None):
            if not self.killed:
                raise KeyboardInterrupt()
            return 1
        def poll(self):
            return 1 if self.killed else None
        def kill(self):
            self.killed = True
    monkeypatch.setattr(subprocess, 'Popen', InterruptedChild)
    output = tmp_path / 'interrupt.json'
    result = soak.run(1, 1, .1, output)
    assert children[0].killed
    assert result['passed'] is False
    assert 'KeyboardInterrupt:' in result['errors'][0]
    assert output.exists()
    assert Path(result['retained_runtime']).is_dir()
    soak.remove_generated_runtime(Path(result['retained_runtime']))


def test_completion_requires_measured_time_and_last_crash_reconciliation():
    report = {'errors': [], 'observed_seconds': 86399, 'requested_seconds': 86400,
              'segments': 1, 'final_recovery_verified': True, 'polling_validated': True,
              'polling_coverage_ratio': 1}
    assert not soak.completion_fields(report)['passed']
    report['observed_seconds'] = 86400
    assert soak.completion_fields(report)['soak_completed']
    report['final_recovery_verified'] = False
    assert not soak.completion_fields(report)['soak_completed']
    report.update(final_recovery_verified=True, errors=['poll failed'])
    assert not soak.completion_fields(report)['soak_completed']


def test_unmarked_runtime_cannot_be_used(tmp_path):
    runtime = tmp_path / 'unmarked'
    runtime.mkdir()
    with pytest.raises(ValueError, match='marked'):
        soak.isolated_environment(runtime)
    assert list(runtime.iterdir()) == []


@pytest.mark.parametrize('value', [0, -1, float('nan'), float('inf')])
def test_invalid_duration_does_not_start_process(value, tmp_path):
    with pytest.raises(ValueError, match='positive finite'):
        soak.run(value, 1, .1, tmp_path / 'invalid.json')


def test_long_sleep_cannot_count_as_polling_coverage():
    report = {'observed_seconds': 0, 'covered_seconds': 0, 'max_poll_gap_seconds': 0}
    with pytest.raises(AssertionError, match='Polling gap'):
        soak.record_poll_coverage(report, 86400, 86400, 15)
    assert report['observed_seconds'] == 86400
    assert report['covered_seconds'] == 15
    assert report['polling_coverage_ratio'] < .001
    result = {'errors': [], 'observed_seconds': 86400, 'requested_seconds': 86400,
              'segments': 1, 'final_recovery_verified': True, 'polling_validated': True,
              'polling_coverage_ratio': report['polling_coverage_ratio']}
    assert not soak.completion_fields(result)['soak_completed']


def test_request_stall_is_rejected_even_with_successful_http(monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            return {}
    class Client:
        def get(self, endpoint):
            return Response()
    clock = iter([0, soak.MAX_REQUEST_SECONDS + 1])
    monkeypatch.setattr(soak.time, 'monotonic', lambda: next(clock))
    report = {}
    with pytest.raises(AssertionError, match='request exceeded'):
        soak.checked_get(Client(), '/api/status', report)
    assert report['max_request_seconds'] == soak.MAX_REQUEST_SECONDS + 1
    assert report['last_endpoint'] == '/api/status'


def test_unbounded_poll_interval_is_rejected(tmp_path):
    with pytest.raises(ValueError, match='at most'):
        soak.run(86400, 86400, 86400, tmp_path / 'invalid.json')


def test_report_read_and_atomic_write_retry_sharing_violations(tmp_path, monkeypatch):
    from pipeline_v2 import atomic_io
    output = tmp_path / 'retry.json'
    original_replace = atomic_io.os.replace
    replacements = []
    def busy_replace(source, target):
        replacements.append(target)
        if len(replacements) < 3:
            raise PermissionError('fixture sharing violation')
        return original_replace(source, target)
    monkeypatch.setattr(atomic_io.os, 'replace', busy_replace)
    soak.write_report(output, {'passed': True})
    assert len(replacements) == 3
    original_read = Path.read_text
    reads = []
    def busy_read(path, *args, **kwargs):
        if path == output:
            reads.append(path)
            if len(reads) < 3:
                raise PermissionError('fixture reader sharing violation')
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', busy_read)
    assert soak.read_report(output) == {'passed': True}
    assert len(reads) == 3
