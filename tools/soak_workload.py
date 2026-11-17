"""Generated media and real lifecycle adapters for the isolated dashboard soak."""
import asyncio
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def record_timing(report, name, elapsed, *, include_in_poll_gap=True):
    values = report.setdefault('timings', {}).setdefault(name, {'count': 0, 'total_seconds': 0, 'max_seconds': 0})
    values['count'] += 1
    values['total_seconds'] += elapsed
    values['max_seconds'] = max(values['max_seconds'], elapsed)
    values['last_seconds'] = elapsed
    if include_in_poll_gap:
        report.setdefault('current_poll_phases', {})[name] = elapsed


@contextmanager
def timed(report, name, *, include_in_poll_gap=True):
    started = time.monotonic()
    try:
        yield
    finally:
        record_timing(report, name, time.monotonic() - started, include_in_poll_gap=include_in_poll_gap)


@contextmanager
def checkpointed_step(report, name, poll):
    """Measure one blocking step, then validate all endpoints before the next."""
    with timed(report, name):
        yield
    poll()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def isolate_control_paths(runtime):
    runtime = Path(runtime).resolve()
    control = runtime / 'workspace/bot_system/control'
    require((runtime / '.dashboard-soak-fixture').is_file(), 'Unmarked control runtime')
    require(Path(os.environ['AUTODUB_WORKSPACE']).resolve() == runtime / 'workspace', 'Workspace isolation mismatch')
    require(Path(os.environ['TOOL_V1_CONTROL_DIR']).resolve() == control, 'Control isolation mismatch')
    import service_control
    paths = lambda *args, **kwargs: [control]
    service_control.control_directories = paths
    # Rebind aliases if a test preloaded either consumer. Fresh child processes
    # install this boundary before importing either module.
    for name in ('stop_service', 'video_pause'):
        if name in sys.modules:
            sys.modules[name].control_directories = paths


def verify_control_isolation(runtime):
    import service_control
    import stop_service
    import video_pause
    expected = [Path(runtime).resolve() / 'workspace/bot_system/control']
    for module in (service_control, stop_service, video_pause):
        require(module.control_directories(ROOT) == expected, 'Control alias escaped fixture: ' + module.__name__)
    return {'verified': True, 'directories': [str(path) for path in expected]}


def stable_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def verify_persistence(previous, current, new_history_rows=0):
    """Every retained identity must preserve its content, even at equal counts."""
    for kind, records in previous['identity_ledger'].items():
        actual = current['identity_ledger'].get(kind, {})
        for identity, expected in records.items():
            require(actual.get(identity) == expected, f'Durable {kind} record lost or changed: {identity}')
    before, after = previous['tracker_history'], current['tracker_history']
    expected_count = min(50, len(before) + new_history_rows)
    require(len(after) == expected_count, 'Tracker history retention count changed')
    retained = max(0, min(len(before), 50 - new_history_rows))
    require(after[min(50, new_history_rows):] == before[:retained], 'Tracker history was reset outside its retention policy')


def persistence_inventory(runtime, queue):
    from queue_control_service import read_operation, read_snapshot
    import job_tracker
    state = read_snapshot(queue.snapshot)
    receipts = state.get('operation_receipts', {})
    for operation_id, receipt in receipts.items():
        operation = read_operation(queue.snapshot, operation_id)
        require(operation and operation['state'] == receipt['state'], 'Durable operation ACK cannot be reconciled')
    db_path = runtime / 'workspace/control/batch_plans.sqlite3'
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as db:
            batches = {row[0]: stable_hash(json.loads(row[1]))
                       for row in db.execute('SELECT batch_id, plan FROM batch_plans')}
    else:
        batches = {}
    batch_count = len(batches)
    commands = {p.stem: stable_hash(json.loads(p.read_text(encoding='utf-8')))
                for p in (queue.snapshot.parent / 'queue_commands').glob('*.json')}
    stored = [p for p in (runtime / 'workspace').rglob('*') if p.is_file()]
    history = job_tracker.get_status().get('history') or []
    outputs = sorted(p for p in (runtime / 'output').rglob('*') if p.is_file())
    inputs = sorted(p for p in (runtime / 'input').rglob('*') if p.is_file())
    ledger = {'receipts': {key: stable_hash(value) for key, value in receipts.items()},
              'tombstones': {key: key for key in state.get('tombstones', [])},
              'commands': commands, 'batch_plans': batches,
              'outputs': {str(p.relative_to(runtime / 'output')): digest(p) for p in outputs},
              'sources': {str(p.relative_to(runtime / 'input')): digest(p) for p in inputs}}
    history = [stable_hash(item) for item in history]
    return {'receipt_count': len(receipts), 'tombstone_count': len(state.get('tombstones', [])),
            'command_count': len(commands),
            'batch_count': batch_count, 'tracker_history_rows': len(history),
            'identity_ledger': ledger, 'tracker_history': history,
            'identity_ledger_sha256': stable_hash({'ledger': ledger, 'history': history}),
            'persistent_bytes': sum(p.stat().st_size for p in stored),
            'input_files': len(inputs), 'input_bytes': sum(p.stat().st_size for p in inputs),
            'output_files': len(outputs), 'output_bytes': sum(p.stat().st_size for p in outputs),
            'bytes_per_batch': sum(p.stat().st_size for p in stored) / max(1, batch_count),
            'retention': {'tracker_history_rows': 50, 'batch_plans': 'retained for full run',
                          'queue_receipts_and_tombstones': 'retained for full run'},
            'receipt_identity_sha256': hashlib.sha256(json.dumps(sorted(receipts)).encode()).hexdigest()}


def inventory_summary(inventory):
    return {key: value for key, value in inventory.items() if key not in ('identity_ledger', 'tracker_history')}


class FixtureWorkload:
    def __init__(self, runtime, queue, report):
        self.runtime, self.queue, self.report = runtime, queue, report
        self.source, self.output = runtime / 'input', runtime / 'output'
        corpus = runtime / 'corpus'
        corpus.mkdir(exist_ok=True)
        self.seed = corpus / 'Nguồn 中文 mẫu.mp4'
        if not self.seed.exists():
            ffmpeg = shutil.which('ffmpeg')
            require(ffmpeg, 'ffmpeg is required to build the generated media corpus')
            generated = subprocess.run([ffmpeg, '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=navy:s=160x90:r=10',
                            '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', '0.5',
                            '-c:v', 'mpeg4', '-q:v', '4', '-c:a', 'aac', '-threads', '1', str(self.seed)],
                           capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            require(generated.returncode == 0, 'Corpus generation failed: ' + generated.stderr.decode(errors='replace')[-2000:])
        for name in ('Corpus A.mp4', 'Corpus Việt 中文.mp4', 'Corpus # percent %.mp4'):
            if not (self.source / name).exists():
                shutil.copy2(self.seed, self.source / name)
        for name in ('Dubbed_Corpus A.mp4', 'Dubbed_Corpus Việt 中文.mp4'):
            if not (self.output / name).exists():
                shutil.copy2(self.seed, self.output / name)
        self.corpus = {str(p.relative_to(runtime)): digest(p) for p in [self.seed,
                       *sorted(self.source.glob('Corpus*.mp4')), *sorted(self.output.glob('Dubbed_Corpus*.mp4'))]}
        report['corpus'] = {'files': self.corpus, 'media_count': len(self.corpus), 'generated': True,
                            'duration_seconds_per_file': .5, 'video': '160x90 10fps', 'audio': '16kHz mono'}

    def bind(self, client, api, checked_read):
        self.client, self.api = client, api
        self.read = lambda endpoint: checked_read(client, endpoint, self.report)

    def post(self, endpoint, payload, expected):
        start = time.monotonic()
        response = self.client.post(endpoint, json=payload)
        kind = '/api/batches/<id>/abandon' if endpoint.endswith('/abandon') else endpoint
        record_timing(self.report, 'post:' + kind, time.monotonic() - start)
        require(time.monotonic()-start <= 5, 'Mutation request exceeded 5s: ' + endpoint)
        require(response.status_code == expected, f'{endpoint}: {response.status_code} {response.text}')
        self.report['mutation_requests'] = self.report.get('mutation_requests', 0) + 1
        return response.json()

    def handoff(self):
        import job_tracker
        old = 'handoff_old_' + uuid.uuid4().hex
        new = 'handoff_new_' + uuid.uuid4().hex
        job_tracker.start_batch(1, job_id=old)
        job_tracker.get_status()  # Warm the process-local state before release.
        job_tracker.mark_stopped('Synthetic handoff')
        job_tracker.release_batch(old)
        ready, proceed = self.runtime / (new + '.ready'), self.runtime / (new + '.continue')
        command = [sys.executable, '-B', str(Path(__file__).resolve()), '--handoff', str(self.runtime), new]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                require(process.poll() is None and time.monotonic() < deadline, 'Handoff peer did not acquire slot')
                time.sleep(.02)
            job_tracker.release_batch(old)
            stale = job_tracker.current_execution.set(old)
            try:
                job_tracker.update_step(8, 'Rejected stale owner', percent=99)
            finally:
                job_tracker.current_execution.reset(stale)
            status = job_tracker.get_status()
            require(status['execution_id'] == new and status['active'] and status['occupies_slot'],
                    'Old process overwrote the next execution')
            proceed.write_text('verify', encoding='utf-8')
            _, stderr = process.communicate(timeout=15)
            require(process.returncode == 0, 'Handoff peer failed: ' + stderr.decode(errors='replace')[-1000:])
            self.report['handoff_checks'] += 1
        finally:
            if process.poll() is None:
                process.kill(); process.wait(timeout=5)

    def cycle(self, identity):
        """Exercise real API/queue/lifecycle; replace only paid/native render work."""
        with timed(self.report, 'workload:cancel'):
            self.cancel_pending(identity)
        self.poll_checkpoint()
        batch_started = time.monotonic()
        def batch_step(name):
            return checkpointed_step(self.report, 'batch:' + name, self.poll_checkpoint)
        with batch_step('imports'):
            import batch_processor
            import job_tracker
            from batch_plan_service import BatchPlanStore, fingerprint
            from batch_control import to_thread_drained
            from pipeline_v2.atomic_io import atomic_write_json
        with batch_step('prepare_source'):
            source = self.source / 'processed' / ('Thử lại ' + identity + '.mp4')
            source.parent.mkdir(exist_ok=True)
            shutil.copy2(self.seed, source)
        operation = 'retry_' + identity
        blocker = 'busy_' + identity
        called = []
        async def render(path, output_dir, *args, **kwargs):
            called.append(kwargs['job_id'])
            target = Path(output_dir) / ('Dubbed_' + Path(path).stem + '.mp4')
            require(not target.exists(), 'Fixture attempted to overwrite an output')
            shutil.copy2(self.seed, target)
            atomic_write_json(batch_processor._receipt_path(target), {
                'job_id': kwargs['job_id'], 'input_sha256': fingerprint(path)['sha256'],
                'output_sha256': fingerprint(target)['sha256'], 'source_path': str(path),
                'source_name': Path(path).name})
            with timed(self.report, 'workload:batch_finalize'):
                await to_thread_drained(job_tracker.finish_video, Path(path).name, str(target), .5)
            return True
        with batch_step('claim_blocker'):
            job_tracker.start_batch(1, job_id=blocker)
        payload = {'filename': source.name, 'operation_id': operation}
        with batch_step('retry_busy'):
            self.post('/api/retry-video', payload, 409)
        with batch_step('read_prepared'):
            plan = BatchPlanStore(os.environ['AUTODUB_WORKSPACE']).get(operation_id=operation)
            require(plan['state'] == 'prepared', 'Busy retry did not preserve prepared operation')
        with batch_step('stop_blocker'):
            job_tracker.mark_stopped('Release synthetic blocker')
        with batch_step('release_blocker'):
            job_tracker.release_batch(blocker)
        with patch.object(batch_processor, 'process_single_local_video', render):
            with batch_step('retry_accept'):
                accepted = self.post('/api/retry-video', payload, 202)
            async def drain():
                await asyncio.wait_for(asyncio.shield(self.api.BATCH_TASK), 5)
            with batch_step('drain'):
                self.client.portal.call(drain)
            with batch_step('retry_replay'):
                replay = self.post('/api/retry-video', payload, 200)
        with batch_step('read_completed'):
            final = BatchPlanStore(os.environ['AUTODUB_WORKSPACE']).get(operation_id=operation)
            item_error = final['items'][0].get('result', {}).get('error')
            require(final['state'] == 'completed' and len(called) == 1,
                    f"Retry did not finish exactly once: state={final['state']}, renders={len(called)}, error={item_error}")
            require(accepted['batch_id'] == replay['batch_id'] == plan['batch_id'], 'Retry operation identity changed')
        with batch_step('verify_source'):
            require(source.is_file() and digest(source) == digest(self.seed), 'Archived source changed during retry')
        with batch_step('prepare_abandon'):
            prepared = self.post('/api/jobs/batch', {'items': [{'filename': 'Corpus A.mp4'}],
                                 'trigger': False, 'idempotency_key': 'abandon_' + identity}, 201)
        with batch_step('describe_abandon'):
            described = self.read('/api/batches/' + prepared['batch_id'])
        with batch_step('abandon'):
            abandoned = self.post('/api/batches/' + prepared['batch_id'] + '/abandon', described['action_identity'], 200)
            require(abandoned['state'] == 'abandoned', 'Prepared batch could not be explicitly abandoned')
        record_timing(self.report, 'workload:batch', time.monotonic() - batch_started, include_in_poll_gap=False)
        self.poll_checkpoint()
        with timed(self.report, 'workload:pause'):
            self.client.portal.call(self.pause_cycle, identity)
        self.poll_checkpoint()
        with timed(self.report, 'workload:local_file'):
            self.client.portal.call(self.local_file, identity)
        self.poll_checkpoint()
        self.report['workload_checks'] += 1
        for name, expected in self.corpus.items():
            require(digest(self.runtime / name) == expected, 'Fixed corpus was overwritten')

    def cancel_pending(self, identity):
        canceled = {'job_id': 'canceled_' + identity, 'type': 'url',
                    'url': 'https://example.invalid/canceled-fixture'}
        self.queue.put_nowait(canceled)
        operation = 'cancel_' + identity
        payload = {'target_job_ids': [canceled['job_id']], 'expected_revision': self.queue.revision,
                   'operation_id': operation}
        accepted = self.post('/api/queue/delete', payload, 202)
        require(accepted['state'] == 'pending', 'Queue delete was falsely acknowledged before the owner applied it')
        self.queue.apply_commands()
        acknowledged = self.read('/api/queue/operations/' + operation)
        require(acknowledged['state'] == 'applied' and self.queue.qsize() == 0, 'Queue delete was not applied')
        revision = self.queue.revision
        replay = self.post('/api/queue/delete', payload, 202)
        self.queue.apply_commands()
        require(replay['state'] == 'applied' and self.queue.revision == revision, 'Queue delete replay changed committed state')
        try:
            self.queue.put_nowait(canceled)
        except ValueError:
            pass
        else:
            raise AssertionError('Canceled job was resurrected')
        require(self.queue.qsize() == 0 and canceled['job_id'] in self.queue.tombstones,
                'Canceled identity was not retained')
        self.report['queue_command_checks'] = self.report.get('queue_command_checks', 0) + 1

    async def pause_cycle(self, identity):
        import job_tracker
        import video_pause
        old, new = 'pause_old_' + identity, 'pause_new_' + identity
        job_tracker.start_batch(1, job_id=old)
        old_generation = job_tracker.get_status()['generation']
        video_pause.request_pause(old, old_generation)
        token = job_tracker.current_execution.set(old)
        checkpoint = asyncio.create_task(video_pause.checkpoint())
        job_tracker.current_execution.reset(token)
        try:
            for _ in range(100):
                if video_pause.state(old, old_generation) == 'paused':
                    break
                await asyncio.sleep(.01)
            require(video_pause.state(old, old_generation) == 'paused', 'Pause did not acknowledge current execution')
            video_pause.resume(old, old_generation)
            await asyncio.wait_for(checkpoint, 2)
            job_tracker.mark_stopped('Pause/resume fixture complete')
            job_tracker.release_batch(old)
            job_tracker.start_batch(1, job_id=new)
            new_generation = job_tracker.get_status()['generation']
            video_pause.request_pause(new, new_generation)
            video_pause.clear_execution_pause(old, old_generation)
            video_pause.resume(old, old_generation)
            token = job_tracker.current_execution.set(old)
            try:
                await video_pause.checkpoint()
            finally:
                job_tracker.current_execution.reset(token)
            require(video_pause.state(new, new_generation) == 'pausing', 'Stale execution changed the next pause')
            require(not video_pause.ACK.exists(), 'Stale execution acknowledged the next pause')
            video_pause.resume(new, new_generation)
            require(video_pause.state(new, new_generation) == 'running', 'Current execution could not resume')
            job_tracker.mark_stopped('Stale pause cleanup verified')
            job_tracker.release_batch(new)
            self.report['pause_checks'] += 1
        finally:
            checkpoint.cancel()
            await asyncio.gather(checkpoint, return_exceptions=True)

    async def local_file(self, identity):
        import telegram_bot as bot
        import job_tracker
        from batch_control import to_thread_drained
        job = {'type': 'local_file', 'job_id': 'local_' + identity, 'video_path': str(self.seed),
               'filename': self.seed.name, 'force_cloud': True,
               'job_overrides': {'voice_mode': 'manual'}, 'resume_state': {}}
        calls = []
        async def cloud_stub(path, frozen, message):
            require(Path(path) == self.seed and frozen['job_id'] == job['job_id'], 'local_file source/identity changed')
            calls.append(frozen['job_id'])
            target = self.output / (frozen['job_id'] + '.mp4')
            require(not target.exists(), 'local_file output overwrite')
            shutil.copy2(self.seed, target)
            with timed(self.report, 'workload:local_finalize'):
                await to_thread_drained(job_tracker.finish_video, self.seed.name, str(target), .5)
            return {'status': 'success', 'tracker_finalized': True, 'final_video': str(target), 'mode': 'cloud'}
        with patch.object(bot, 'global_queue', self.queue), patch.object(bot, 'GLOBAL_BOT_APP', None), \
             patch.object(bot, 'run_pipeline_cloud_for_telegram', cloud_stub):
            self.queue.put_nowait(bot.prepare_telegram_job(job))
            worker = asyncio.create_task(bot.video_worker())
            try:
                await asyncio.wait_for(self.queue.join(), 5)
                require(calls == [job['job_id']], 'local_file was dropped or executed twice')
                require(job_tracker.get_status()['video_status'] == 'completed', 'Cloud fixture recorded false failure')
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


def handoff_peer(runtime, identity):
    sys.path.insert(0, str(ROOT / 'backend'))
    isolate_control_paths(runtime)
    import job_tracker
    verify_control_isolation(runtime)
    job_tracker.start_batch(1, job_id=identity)
    (runtime / (identity + '.ready')).write_text('owned', encoding='utf-8')
    deadline = time.monotonic() + 15
    while not (runtime / (identity + '.continue')).exists():
        require(time.monotonic() < deadline, 'Parent did not release handoff barrier')
        time.sleep(.02)
    status = job_tracker.get_status()
    require(status['execution_id'] == identity and status['active'], 'Peer state was overwritten')
    job_tracker.mark_stopped('Synthetic handoff verified')
    job_tracker.release_batch(identity)


if __name__ == '__main__':
    require(len(sys.argv) == 4 and sys.argv[1] == '--handoff', 'Only internal handoff mode is supported')
    runtime = Path(sys.argv[2]).resolve()
    require((runtime / '.dashboard-soak-fixture').is_file(), 'Unmarked runtime')
    handoff_peer(runtime, sys.argv[3])
