"""Offline browser acceptance: real controls/stores, isolated localhost, no provider."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'isolated-batch-browser-token'


def serve(runtime, port):
    for key, folder in {'AUTODUB_WORKSPACE':'workspace', 'AUTODUB_INPUT_DIR':'input',
            'AUTODUB_OUTPUT_DIR':'output', 'AUTODUB_DATA_DIR':'data', 'AUTODUB_MODEL_CACHE':'models',
            'AUTODUB_SHARED_ASSETS_DIR':'assets'}.items():
        path = runtime / folder
        path.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(path)
    os.environ.update(AUTODUB_TESTING='1', AUTODUB_LOCAL_TOKEN=TOKEN, AUTODUB_PORT=str(port))
    control = (runtime / 'workspace' / 'bot_system' / 'control').resolve()
    os.environ.update(TOOL_V1_CONTROL_DIR=str(control), AUTODUB_CONTROL_DIR=str(control))
    sys.path.insert(0, str(ROOT / 'backend'))
    # Environment paths alone still retain legacy root/workspace fallbacks.
    # Install the boundary before any tracker/stop/pause consumer is imported.
    import service_control
    only_fixture_controls = lambda *args, **kwargs: [control]
    service_control.control_directories = only_fixture_controls
    service_control.control_directory = lambda *args, **kwargs: control
    for name in ('stop_service', 'video_pause'):
        if name in sys.modules:
            sys.modules[name].control_directories = only_fixture_controls
            sys.modules[name].control_directory = service_control.control_directory
    import main
    import job_tracker
    from batch_plan_service import BatchPlanStore
    from batch_lifecycle_service import BatchLifecycle
    from fastapi import Body
    from types import SimpleNamespace
    from cloud import cloud_routes
    from cloud.cloud_artifacts import sha256_file, probe_media, verify_artifact, stage_for_publication
    from telegram_queue_monitor import MonitoredQueue
    from telegram_local_source import capture_source
    import video_pause
    import stop_service
    import uvicorn
    for module in (service_control, stop_service, video_pause):
        assert module.control_directories(ROOT) == [control], module.__name__
    assert video_pause.FLAG.parent == control
    (runtime / 'control-isolation.json').write_text(json.dumps({'verified':True,
        'directories':[str(control)], 'legacy_fallbacks_disabled':True}),encoding='utf-8')

    # Real Cloud routes and SQLite; no daemon, credentials, CLI or provider calls.
    cloud_routes.get_cloud_worker = lambda: SimpleNamespace(get_current_job=lambda: None, is_running=lambda: False)
    cloud_routes.get_usage = lambda: {}
    cloud_routes.get_sessions = lambda: []
    main.app.include_router(cloud_routes.router)
    audit = []
    @main.app.middleware('http')
    async def capture_control_calls(request, call_next):
        response = await call_next(request)
        if request.method == 'POST' and request.url.path.startswith('/api/'):
            audit.append({'path': request.url.path, 'status': response.status_code,
                          'origin': request.headers.get('origin'), 'dashboard_input': request.headers.get('x-dashboard-input')})
        return response

    @main.app.get('/__test/audit')
    async def control_audit():
        return audit

    @main.app.post('/__test/execution')
    async def execution(payload: dict = Body(...)):
        current = job_tracker.get_status(strict=True)
        if current.get('occupies_slot'):
            job_tracker.release_batch(current['execution_id'])
        if payload.get('execution_id'):
            job_tracker.start_batch(1, job_id=payload['execution_id'])
        return job_tracker.get_status(strict=True)

    @main.app.post('/__test/ack-pause')
    async def acknowledge_pause(payload: dict = Body(...)):
        return {'acknowledged': video_pause._acknowledge(payload['execution_id'], payload['generation'])}

    @main.app.post('/__test/local-source')
    async def local_source():
        source = runtime / 'Nguồn local bị mất.mp4'
        shutil.copyfile(runtime / 'input' / 'A thử.mp4', source)
        queue = MonitoredQueue()
        queue.restore()
        job = {'job_id': 'browser-local-source', 'type': 'local_file', 'filename': source.name,
               'video_path': str(source), 'force_cloud': True, 'resume_state': {}}
        capture_source(job)
        queue.put_nowait(job)
        source.unlink()
        restored = MonitoredQueue()
        restored.restore()
        return {'snapshot': str(restored.snapshot), 'revision': restored.revision}

    @main.app.post('/__test/cloud')
    async def cloud_fixture(payload: dict = Body(...)):
        store = cloud_routes.store
        if payload.get('confirm_job'):
            job = store.get_job(payload['confirm_job'])
            assert store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=True)
            return {'job': store.get_job(job.job_id).to_dict(), 'receipt': store.get_receipt(job.job_id)}
        outcome = payload['outcome']
        source = runtime / 'input' / 'A thử.mp4'
        job = store.enqueue(str(source), video_name='browser-' + outcome + '.mp4', claim_owner='browser-fixture')
        assert job.attempt_id
        if outcome == 'error':
            assert store.finish_attempt(job.job_id, job.attempt_id, job.owner_id,
                                        error='cloud_transfer_incomplete', cleanup_ok=True)
        else:
            token = dict(attempt_id=job.attempt_id, owner_id=job.owner_id)
            digest = sha256_file(source)
            for state in ('uploading', 'processing', 'downloading', 'validating'):
                assert store.update_progress(job.job_id, state=state, source_sha256=digest, **token)
            manifest = dict(job_id=job.job_id, attempt_id=job.attempt_id, source_sha256=digest,
                            sha256=digest, size_bytes=source.stat().st_size,
                            duration_seconds=float(probe_media(source)['format']['duration']))
            verified = verify_artifact(source, manifest, job.job_id, job.attempt_id, digest)
            target = runtime / 'cloud-output'
            staged = stage_for_publication(source, target, job.job_id, job.attempt_id, verified)
            assert store.publish(job.job_id, job.attempt_id, job.owner_id, staged, target / job.video_name, verified)
            assert store.finish_attempt(job.job_id, job.attempt_id, job.owner_id, cleanup_ok=outcome == 'success')
        return {'job': store.get_job(job.job_id).to_dict(), 'receipt': store.get_receipt(job.job_id)}

    async def runner(*args, **kwargs):
        plan = kwargs['batch_plan']
        runfile = runtime / 'runs.json'
        runs = json.loads(runfile.read_text()) if runfile.exists() else []
        runs.append({'batch_id':plan['batch_id'], 'execution_id':plan['execution_id'], 'items':plan['items']})
        runfile.write_text(json.dumps(runs), encoding='utf-8')
        store = BatchPlanStore(main.WORKSPACE)
        for item in plan['items']:
            store.item_result(plan['batch_id'], item['job_id'], 'completed', stub=True)
        store.update(plan['batch_id'], state='completed')
        job_tracker.finish_batch()
        job_tracker.release_batch(plan['execution_id'])

    main.batch_runner = runner
    main._processing_disabled = lambda: False
    original_bind = BatchLifecycle._bind
    def bind(self, plan, schedule):
        marker = runtime / 'fail-binding-once'
        if marker.exists():
            marker.unlink()
            raise OSError('Acceptance fault after schedule JSON, before plan binding')
        return original_bind(self, plan, schedule)
    BatchLifecycle._bind = bind

    @main.app.post('/__test/fail-binding')
    async def fail_binding():
        (runtime / 'fail-binding-once').write_text('1')
        return {'ok': True}

    @main.app.post('/__test/busy')
    async def busy(payload: dict = Body(...)):
        if payload['busy']:
            job_tracker.start_batch(1, job_id='browser-busy-owner')
        else:
            job_tracker.finish_batch()
            job_tracker.release_batch('browser-busy-owner')
        return {'ok':True}

    uvicorn.run(main.app, host='127.0.0.1', port=port, log_level='warning')


def run(output):
    from playwright.sync_api import expect, sync_playwright
    output.mkdir(parents=True, exist_ok=True)
    browser_exe = next((Path(os.environ['LOCALAPPDATA']) / 'ms-playwright').rglob('chrome-headless-shell.exe'))
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    checks, errors = [], []
    def source_hashes():
        return {str(path.relative_to(ROOT)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in ('backend', 'tools', 'tests') for path in sorted((ROOT / folder).rglob('*'))
                if path.is_file() and path.suffix in ('.py', '.html', '.js', '.cjs') and '__pycache__' not in path.parts}
    source_before = source_hashes()
    started = time.time()
    with tempfile.TemporaryDirectory(prefix='batch_browser_acceptance_') as temporary:
        runtime = Path(temporary)
        inputs = runtime / 'input'
        inputs.mkdir()
        sample = inputs / 'A thử.mp4'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=160x90:r=5',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=44100',
            '-t','0.4','-c:v','mpeg4','-c:a','aac','-y',str(sample)], check=True, timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        shutil.copyfile(sample, inputs / 'B.mp4')
        logfile = (runtime / 'server.log').open('w', encoding='utf-8')
        proc = subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve()),'--serve','--runtime',str(runtime),'--port',str(port)],
            cwd=ROOT,stdout=logfile,stderr=logfile,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    raise RuntimeError((runtime / 'server.log').read_text(encoding='utf-8',errors='replace'))
                try:
                    with urlopen(url + '/api/status', timeout=.5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.2)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=str(browser_exe))
                try:
                    exercise(browser, url, runtime, checks, errors, output, expect)
                except BaseException:
                    page = browser.contexts[0].pages[0]
                    (output/'control-audit.json').write_text(json.dumps(page.request.get(url+'/__test/audit').json(),indent=2),encoding='utf-8')
                    page.screenshot(path=str(output/'failure.png'))
                    (output/'failure.json').write_text(json.dumps({'errors':errors,
                        'pending':page.evaluate('sessionStorage.getItem("dashboardPendingBatch")'),
                        'status':page.locator('#summarySubmitStatus').text_content(),
                        'recovery':page.locator('#recoverableBatches').text_content()},ensure_ascii=False,indent=2),encoding='utf-8')
                    raise
                finally:
                    browser.close()
        finally:
            import psutil
            try:
                children = psutil.Process(proc.pid).children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in children:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            proc.terminate()
            proc.wait(timeout=15)
            _, remaining = psutil.wait_procs(children, timeout=5)
            for child in remaining:
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(remaining, timeout=5)
            logfile.close()
            shutil.copyfile(runtime/'server.log',output/'browser-server.log')
            if (runtime/'control-isolation.json').exists():
                shutil.copyfile(runtime/'control-isolation.json',output/'control-isolation.json')
    source_after = source_hashes()
    changed_sources = [name for name in source_before.keys() | source_after.keys()
                       if source_before.get(name) != source_after.get(name)]
    (output/'source-hashes.json').write_text(json.dumps(source_after,indent=2)+'\n',encoding='utf-8')
    result={'passed':checks,'page_errors':errors,'isolated':True,'runner':'stub','real_model':False,
            'real_telegram':False,'cloud_provider':'offline status stub; real SQLite/publication/API/template',
            'pause_origin_adapter':'Use allowed Origin 127.0.0.1:8088 on pause/resume; destination remains isolated random port.',
            'source_unchanged_during_run':not changed_sources,'changed_sources':sorted(changed_sources),
            'source_hashes':'source-hashes.json','control_isolation':'control-isolation.json',
            'duration_seconds':round(time.time()-started,2)}
    (output/'browser.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    assert not changed_sources, 'Source changed during browser acceptance: ' + ', '.join(changed_sources)


def exercise(browser, url, runtime, checks, errors, output, expect):
    page = browser.new_page(viewport={'width':1440,'height':1080})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept('03:00') if dialog.type == 'prompt' else dialog.accept())
    posts=[]
    page.on('request', lambda request: posts.append(request.post_data_json)
            if request.url == url + '/api/jobs/batch' and request.method == 'POST' else None)
    page.goto(url, wait_until='domcontentloaded')
    panel=page.locator('#recoverableBatches')
    def select_a():
        checkbox=page.locator('input[data-filename="A thử.mp4"]')
        expect(checkbox).to_be_visible(timeout=20000)
        expect(page.locator('#btnStartPhoi')).to_be_enabled(timeout=10000)
        checkbox.check()
        page.locator('#btnStartPhoi').click()
        expect(page.locator('#preRunSummaryModal')).to_be_visible(timeout=15000)
    def canceled():
        page.locator('#scheduleChip').get_by_role('button', name='Hủy', exact=True).click()
        expect(page.locator('#scheduleChip')).to_contain_text('Đã hủy lịch', timeout=10000)
        expect(panel.get_by_role('button',name='Bỏ batch đã lưu')).to_be_visible(timeout=10000)
    select_a()
    assert page.request.post(url+'/__test/busy',data={'busy':True},headers={'X-Local-Control-Token':TOKEN}).ok
    page.locator('#btnConfirmBulkEnqueue').click()
    expect(page.locator('#summarySubmitStatus')).to_contain_text('đang bận',timeout=10000)
    assert page.request.post(url+'/__test/busy',data={'busy':False},headers={'X-Local-Control-Token':TOKEN}).ok
    page.reload(wait_until='domcontentloaded')
    panel.get_by_role('button',name='Xác minh / thử lại',exact=True).click(timeout=20000)
    expect(panel.get_by_role('button',name='Xác minh / thử lại',exact=True)).to_have_count(0,timeout=20000)
    assert len(posts)==2 and posts[0]==posts[1], posts
    checks.append('busy then reload retries the same frozen batch/key exactly once')
    select_a()
    assert page.request.post(url+'/__test/fail-binding',headers={'X-Local-Control-Token':TOKEN}).ok
    page.locator('#btnScheduleBulk').click()
    expect(page.locator('#scheduleChip')).to_contain_text('Lịch cần kiểm tra',timeout=20000)
    incomplete=page.request.get(url+'/api/schedule').json()
    assert incomplete['binding_confirmed'] is False
    assert page.evaluate('JSON.parse(sessionStorage.getItem("dashboardScheduleIntent")).sent') is True
    page.reload(wait_until='domcontentloaded')
    panel.get_by_role('button',name='Xác minh / thử lại',exact=True).click(timeout=20000)
    expect(page.locator('#scheduleChip')).to_contain_text('Đã xác nhận lịch',timeout=20000)
    first=page.request.get(url+'/api/schedule').json()
    assert first['binding_confirmed'] and first['operation_id']==incomplete['operation_id']
    assert first['run_at']==incomplete['run_at']
    checks.append('half-written binding remains unconfirmed across reload and same POST repairs it')
    frozen=page.request.get(url+'/api/batches/'+first['batch_id']).json()['items']
    page.reload(wait_until='domcontentloaded')
    canceled()
    panel.get_by_role('button',name='Đặt lại lịch',exact=True).click()
    expect(page.locator('#scheduleChip')).to_contain_text('Đã xác nhận lịch',timeout=20000)
    second=page.request.get(url+'/api/schedule').json()
    assert first['batch_id']==second['batch_id'] and first['schedule_id']!=second['schedule_id']
    assert page.request.get(url+'/api/batches/'+second['batch_id']).json()['items']==frozen
    checks.append('cancel after reload exposes actions and reschedule retains exact items/config')
    canceled()
    panel.get_by_role('button',name='Bỏ batch đã lưu',exact=True).click()
    expect(panel.get_by_role('button',name='Bỏ batch đã lưu')).to_have_count(0,timeout=10000)
    assert page.request.get(url+'/api/batches/'+second['batch_id']).json()['state']=='abandoned'
    checks.append('abandon releases reservation without deleting sources')
    select_a()
    page.locator('#btnScheduleBulk').click()
    expect(page.locator('#scheduleChip')).to_contain_text('Đã xác nhận lịch',timeout=20000)
    third=page.request.get(url+'/api/schedule').json()
    assert third['batch_id']!=second['batch_id']
    canceled()
    second_context = browser.new_context(viewport={'width':1440,'height':1080})
    other = second_context.new_page()
    other.on('pageerror', lambda error: errors.append(str(error)))
    other.on('dialog', lambda dialog: dialog.accept())
    other.goto(url, wait_until='domcontentloaded')
    other_start = other.locator('#recoverableBatches').get_by_role('button',name='Chạy batch đã lưu',exact=True)
    expect(other_start).to_be_visible(timeout=20000)
    # Freeze only reads on tab B; its rendered action genuinely carries the old
    # revision while tab A starts the batch. Mutations still reach the server.
    held_list = other.request.get(url+'/api/batches?recoverable=true').json()
    other.route('**/api/batches?recoverable=true', lambda route: route.fulfill(json=held_list))
    panel.get_by_role('button',name='Chạy batch đã lưu',exact=True).click()
    expect(panel.get_by_role('button',name='Chạy batch đã lưu',exact=True)).to_have_count(0,timeout=10000)
    with other.expect_response(lambda response: response.url == url+'/api/batches/'+third['batch_id']+'/start') as replay:
        other_start.click()
    assert replay.value.status == 200
    runs=json.loads((runtime/'runs.json').read_text())
    assert len(runs)==2, runs
    assert runs[1]['batch_id']==third['batch_id']
    assert all([item['filename'] for item in entry['items']]==['A thử.mp4'] for entry in runs)
    assert (runtime/'input'/'A thử.mp4').is_file() and (runtime/'input'/'B.mp4').is_file()
    assert not list((runtime/'output').glob('*.mp4'))
    checks.append('same source selectable again; canceled batch starts once and never selects B')
    checks.append('two browser contexts start the same held batch; stale tab replay creates no second execution')
    other.close()
    second_context.close()
    exercise_stale_controls(browser, url, page, checks, errors, output, expect)
    exercise_local_and_cloud(browser, url, page, checks, errors, output, expect)
    audit = page.request.get(url+'/__test/audit').json()
    (output/'control-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    assert not errors, errors
    page.screenshot(path=str(output/'batch-recovery.png'), full_page=False)


def exercise_stale_controls(browser, url, page, checks, errors, output, expect):
    headers = {'X-Local-Control-Token':TOKEN}
    def fixture(path, payload):
        response = page.request.post(url+'/__test/'+path,data=payload,headers=headers)
        assert response.ok, response.text()
        return response.json()
    def local_origin(route):
        request_headers = dict(route.request.headers)
        request_headers['origin'] = 'http://127.0.0.1:8088'
        route.fulfill(response=route.fetch(headers=request_headers))
    page.route('**/api/*-video', local_origin)
    state_a = fixture('execution', {'execution_id':'browser-control-A'})
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('#btnPauseVideo')).to_be_enabled(timeout=20000)
    with page.expect_response(url+'/api/pause-video') as paused:
        page.locator('#btnPauseVideo').click()
    assert paused.value.status == 200, (paused.value.status, paused.value.text())
    identity_a = {key:state_a[key] for key in ('execution_id','generation')}
    assert fixture('ack-pause', identity_a)['acknowledged']
    frozen_a = page.request.get(url+'/api/status').json()
    assert frozen_a['pause_state'] == 'paused'
    page.route('**/api/status', lambda route: route.fulfill(json=frozen_a))
    page.evaluate('fetchStatus()')
    expect(page.locator('#btnResumeVideo')).to_be_enabled()
    state_b = fixture('execution', {'execution_id':'browser-control-B'})
    context_b = browser.new_context(viewport={'width':1440,'height':1080})
    current = context_b.new_page()
    current.on('pageerror', lambda error: errors.append(str(error)))
    current.route('**/api/*-video', local_origin)
    current.goto(url,wait_until='domcontentloaded')
    expect(current.locator('#btnPauseVideo')).to_be_enabled(timeout=20000)
    with current.expect_response(url+'/api/pause-video') as paused_b:
        current.locator('#btnPauseVideo').click()
    assert paused_b.value.status == 200
    identity_b = {key:state_b[key] for key in ('execution_id','generation')}
    assert fixture('ack-pause',identity_b)['acknowledged']
    before = page.request.get(url+'/api/status').json()
    with page.expect_response(url+'/api/resume-video') as resumed:
        page.locator('#btnResumeVideo').click()
    assert resumed.value.status == 409 and resumed.value.json()['detail']['code'] == 'stale_execution'
    # The older page can also contain a still-enabled Pause from before ACK.
    frozen_a.update(pause_state='running',capabilities={**frozen_a['capabilities'],'can_pause':True,'can_resume':False})
    page.evaluate('fetchStatus()')
    expect(page.locator('#btnPauseVideo')).to_be_enabled()
    with page.expect_response(url+'/api/pause-video') as stale_pause:
        page.locator('#btnPauseVideo').click()
    assert stale_pause.value.status == 409 and stale_pause.value.json()['detail']['code'] == 'stale_execution'
    after = page.request.get(url+'/api/status').json()
    for key in ('execution_id','generation','pause_state','history'):
        assert before[key] == after[key], key
    assert after['execution_id'] == identity_b['execution_id'] and after['pause_state'] == 'paused'
    current.evaluate('fetchStatus()')
    expect(current.locator('#btnResumeVideo')).to_be_enabled()
    with current.expect_response(url+'/api/resume-video') as resumed_b:
        current.locator('#btnResumeVideo').click()
    assert resumed_b.value.status == 200
    assert page.request.get(url+'/api/status').json()['pause_state'] == 'running'
    page.screenshot(path=str(output/'stale-controls.png'))
    checks.append('two contexts retain A/B identity; stale pause/resume return 409 and preserve paused B; current B resumes')
    current.close()
    context_b.close()
    page.unroute('**/api/status')
    fixture('execution', {'execution_id':None})


def exercise_local_and_cloud(browser, url, page, checks, errors, output, expect):
    headers = {'X-Local-Control-Token':TOKEN}
    response = page.request.post(url+'/__test/local-source',headers=headers)
    assert response.ok, response.text()
    page.reload(wait_until='domcontentloaded')
    row = page.locator('#queueTableBody tr').filter(has_text='Nguồn local bị mất.mp4')
    expect(row).to_contain_text('Nguồn local chưa xác minh',timeout=20000)
    snapshot = page.request.get(url+'/api/queue').json()
    assert snapshot['items'][0]['hold']['reason_code'] == 'missing_local_source'
    audit_before = page.request.get(url+'/__test/audit').json()
    page.locator('#btnProcessQueue').click()
    expect(page.get_by_text('Các video đang được giữ theo lịch hoặc chưa xác minh nguồn.',exact=False)).to_be_visible()
    assert page.request.get(url+'/__test/audit').json() == audit_before
    again = page.request.get(url+'/api/queue').json()
    assert again['revision'] == snapshot['revision'] and again['items'] == snapshot['items']
    page.screenshot(path=str(output/'local-source-hold.png'))
    checks.append('restored local_file with missing source displays manual hold; Process sends no mutation and preserves record')
    cloud = browser.new_page(viewport={'width':1440,'height':1080})
    cloud.on('pageerror', lambda error: errors.append(str(error)))
    jobs = {}
    for outcome in ('success','error','cleanup'):
        response = page.request.post(url+'/__test/cloud',data={'outcome':outcome},headers=headers)
        assert response.ok, response.text()
        jobs[outcome] = response.json()
    assert jobs['success']['receipt'] and jobs['success']['job']['state'] == 'completed'
    assert not jobs['error']['receipt'] and jobs['error']['job']['state'] == 'error'
    assert not jobs['cleanup']['receipt'] and jobs['cleanup']['job']['state'] == 'stop_incomplete'
    cloud.goto(url+'/cloud',wait_until='domcontentloaded')
    for outcome,label in (('success','Hoàn tất'),('error','Lỗi'),('cleanup','Chưa xác nhận thu hồi tài nguyên')):
        row = cloud.locator('#table-jobs-body tr').filter(has_text=jobs[outcome]['job']['job_id'])
        expect(row).to_contain_text(label,timeout=15000)
        if outcome == 'cleanup':
            expect(row).to_contain_text('Thành phẩm đã được lưu')
            expect(row.get_by_role('button')).to_have_count(0)
        else:
            expect(row.get_by_role('button',name='Xoá',exact=True)).to_be_visible()
    expect(cloud.locator('#hero-status-heading')).to_contain_text('Chưa xác nhận thu hồi')
    expect(cloud.locator('#hero-step-desc')).to_contain_text('hàng đợi vẫn giữ chỗ')
    expect(cloud.locator('#hero-btn-stop')).to_be_hidden()
    assert 'step-completed' not in cloud.locator('#step-8').get_attribute('class')
    cloud.screenshot(path=str(output/'cloud-cleanup-hold.png'))
    response = page.request.post(url+'/__test/cloud',data={'confirm_job':jobs['cleanup']['job']['job_id']},headers=headers)
    assert response.ok and response.json()['receipt']
    cloud.reload(wait_until='domcontentloaded')
    row = cloud.locator('#table-jobs-body tr').filter(has_text=jobs['cleanup']['job']['job_id'])
    expect(row).to_contain_text('Hoàn tất',timeout=15000)
    expect(row.get_by_role('button',name='Xoá',exact=True)).to_be_visible()
    expect(cloud.locator('#hero-status-heading')).to_contain_text('Sẵn sàng')
    (output/'cloud-states.json').write_text(json.dumps(jobs,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    checks.append('real Cloud SQLite/API render verified success, failed transfer and cleanup hold; confirmed cleanup becomes completed')
    cloud.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--runtime',type=Path)
    parser.add_argument('--port',type=int)
    parser.add_argument('--output',type=Path,default=ROOT/'test_evidence'/'dashboard-remediation-f5-f6-browser')
    args=parser.parse_args()
    if args.serve:
        if not args.runtime or not args.port:
            parser.error('--serve requires --runtime and --port')
        serve(args.runtime,args.port)
    else:
        run(args.output)
