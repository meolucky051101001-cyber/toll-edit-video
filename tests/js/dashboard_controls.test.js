const test = require('node:test');
const assert = require('node:assert/strict');
const {fixture, slice} = require('./dashboard_fixture.cjs');

const reply = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const flush = () => new Promise(resolve => setImmediate(resolve));

function settingsApp(fetch) {
  const notices = [];
  const app = fixture({fetch, showToast: (...args) => notices.push(args)});
  const buttons = ['AUTO', 'SHORT', 'MEDIUM', 'LONG'].map(mode => ({
    dataset: {mode}, disabled: false, pressed: 'false',
    setAttribute(key, value) { if (key === 'aria-pressed') this.pressed = value; }
  }));
  app.context.document.querySelectorAll = selector => selector === '.v1-mode-btn' ? buttons : [];
  app.run(slice("    let currentV1Mode = 'AUTO';", '    async function loadV1UpgradeSettings()'));
  return Object.assign(app, {notices, buttons});
}

test('mode changes immediately and rapid clicks persist the final selection in order', async () => {
  const requests = [];
  let release;
  const app = settingsApp(async (_url, opts) => {
    const body = JSON.parse(opts.body);
    requests.push(body);
    if (requests.length === 1) await new Promise(resolve => { release = resolve; });
    return reply({status: 'ok', flags: body});
  });
  const first = app.context.selectV1Mode('SHORT');
  assert.equal(app.element('v1RouterModeBadge').textContent, 'SHORT');
  assert.equal(app.buttons[1].pressed, 'true');
  assert(app.buttons.every(button => !button.disabled));
  await flush();
  const middle = app.context.selectV1Mode('MEDIUM');
  const last = app.context.selectV1Mode('LONG');
  assert.equal(app.element('v1RouterModeBadge').textContent, 'LONG');
  assert.equal(requests.length, 1);
  release();
  await Promise.all([first, middle, last]);
  assert.deepEqual(requests, [{V1_VIDEO_MODE: 'SHORT'}, {V1_VIDEO_MODE: 'LONG'}]);
  assert.equal(app.element('v1RouterModeBadge').textContent, 'LONG');
  assert.equal(app.notices.length, 1);
  assert.equal(app.notices[0][1], 'success');
});

test('old ASR failure cannot roll back a later click and latest failure restores last confirmed model', async () => {
  let release;
  let calls = 0;
  const app = settingsApp(async (_url, opts) => {
    calls++;
    if (calls === 1) {
      await new Promise(resolve => { release = resolve; });
      return reply({message: 'old failure'}, 409);
    }
    if (calls === 3) return reply({message: 'runtime missing'}, 409);
    return reply({status: 'ok', flags: JSON.parse(opts.body)});
  });
  const first = app.context.selectAsrModel('sensevoice_small');
  await flush();
  const last = app.context.selectAsrModel('whisper_turbo');
  assert.equal(app.run('currentAsrModel'), 'whisper_turbo');
  release();
  await Promise.all([first, last]);
  assert.equal(app.run('currentAsrModel'), 'whisper_turbo');
  assert(!app.notices.some(([text]) => text.includes('old failure')));
  await app.context.selectAsrModel('sensevoice_small');
  assert.equal(app.run('currentAsrModel'), 'whisper_turbo');
  assert(app.notices.some(([text, kind]) => text === 'runtime missing' && kind === 'error'));
});

test('per-length selectors stay usable and restore saved selection on rejected save', async () => {
  let release;
  const app = settingsApp(async () => {
    await new Promise(resolve => { release = resolve; });
    return reply({message: 'not ready'}, 409);
  });
  const select = app.element('V1_SHORT_ASR_MODEL');
  select.dataset.savedValue = 'whisper_turbo';
  select.value = 'sensevoice_small';
  const pending = app.context.saveLengthProfile(select);
  assert(!select.disabled);
  assert.equal(select.value, 'sensevoice_small');
  await flush();
  release();
  await pending;
  assert.equal(select.value, 'whisper_turbo');
});

test('late settings hydration cannot overwrite a selection made while GET was pending', async () => {
  let releaseGet;
  const app = settingsApp(async (_url, opts = {}) => {
    if (!opts.method) return new Promise(resolve => { releaseGet = () => resolve(reply({V1_VIDEO_MODE: 'AUTO', V1_ASR_MODEL: 'sensevoice_small'})); });
    return reply({status: 'ok', flags: JSON.parse(opts.body)});
  });
  app.context.loadVisualCoverSettings = async () => {};
  app.run(slice('    async function loadV1UpgradeSettings()', '    async function loadVisualCoverSettings()'));
  const loading = app.context.loadV1UpgradeSettings();
  await app.context.selectV1Mode('LONG');
  await app.context.selectAsrModel('whisper_turbo');
  releaseGet();
  await loading;
  assert.equal(app.run('currentV1Mode'), 'LONG');
  assert.equal(app.run('currentAsrModel'), 'whisper_turbo');
});

function addAdmission(app) {
  Object.assign(app.context, {closePreRunSummaryModal() {}, clearPhoiSelection() {}, fetchPhoi() {}, fetchStatus() {}});
  app.run("let isSubmittingBatch=false; const selectedPhoiFiles=new Set(['A.mp4']); const videoDraftsCache={}; const currentActivePresetId=null; const dashboardInputPath='input'; const dashboardOutputPath='output';");
  app.run(slice('    let pendingBatchIntent = null;', '    /* =========================================================================='));
}

test('click model then start waits for its save before freezing a new batch', async () => {
  let release;
  let storedModel = 'whisper_sensevoice';
  const frozen = [];
  const app = settingsApp(async (url, opts) => {
    if (url === '/api/v1/feature-flags') {
      await new Promise(resolve => { release = resolve; });
      const flags = JSON.parse(opts.body);
      storedModel = flags.V1_ASR_MODEL;
      return reply({status: 'ok', flags});
    }
    assert.equal(url, '/api/jobs/batch');
    frozen.push(storedModel);
    return reply({batch_id: 'fresh', state: 'accepted'}, 202);
  });
  addAdmission(app);
  const saving = app.context.selectAsrModel('whisper_turbo');
  await flush();
  const starting = app.context.confirmBulkEnqueue();
  await flush();
  assert.deepEqual(frozen, []);
  release();
  await Promise.all([saving, starting]);
  assert.deepEqual(frozen, ['whisper_turbo']);
});

test('rejected model save blocks a new batch and reports the failure', async () => {
  let batchRequests = 0;
  const app = settingsApp(async (url) => {
    if (url === '/api/jobs/batch') batchRequests++;
    return reply({message: 'runtime missing'}, 409);
  });
  addAdmission(app);
  await app.context.selectAsrModel('sensevoice_small');
  await app.context.confirmBulkEnqueue();
  assert.equal(batchRequests, 0);
  assert.match(app.element('summarySubmitStatus').textContent, /Chưa lưu được cấu hình model/);
});

function stopApp(fetch) {
  const notices = [];
  const app = fixture({fetch, showToast: (...args) => notices.push(args),
    apiErrorMessage: (data, fallback) => data.message || fallback,
    confirm: () => assert.fail('stop must send without a blocking confirmation'),
    fetchStatus() {}, refreshAll() {}});
  app.run("let lastStatusData={execution_id:'exec-a'}; let stopRequestPending=false; let stopPendingExecution=null;");
  app.run(slice('    async function stopBatch()', '    async function openFolder('));
  return Object.assign(app, {notices});
}

test('stop sends immediately, deduplicates clicks and keeps stopping after durable ACK', async () => {
  let release;
  let calls = 0;
  const app = stopApp(async (_url, opts) => {
    calls++;
    assert.equal(opts.method, 'POST');
    await new Promise(resolve => { release = resolve; });
    return reply({status: 'stopping', message: 'Đã nhận yêu cầu dừng'}, 202);
  });
  const pending = app.context.stopBatch();
  assert.equal(calls, 1);
  assert(app.element('btnStop').disabled);
  assert.match(app.element('statusTitle').innerHTML, /ĐANG DỪNG/);
  await app.context.stopBatch();
  assert.equal(calls, 1);
  release();
  await pending;
  assert(app.element('btnStop').disabled);
  assert.equal(app.run('stopPendingExecution'), 'exec-a');
});

test('stop error is reported and permits retry rather than claiming completion', async () => {
  const app = stopApp(async () => reply({message: 'tracker unavailable'}, 503));
  await app.context.stopBatch();
  assert(!app.element('btnStop').disabled);
  assert.equal(app.run('stopPendingExecution'), null);
  assert(app.notices.some(([text, kind]) => text.includes('tracker unavailable') && kind === 'error'));
  assert.match(app.element('stepDescription').textContent, /Chưa xác nhận/);
});
