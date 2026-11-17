const test = require('node:test');
const assert = require('node:assert/strict');
const { fixture, slice } = require('./dashboard_fixture.cjs');

test('delete waits for the owner and sends a stable ID and queue revision', async () => {
  const calls = [];
  const notices = [];
  const app = fixture({
    confirm: () => true,
    queueDeleteIds: ['job-one'],
    queueMoving: false,
    newOperationId: () => 'queue-one',
    showToast: (...args) => notices.push(args),
    fetchQueue: async () => {},
    setTimeout: resolve => resolve(),
    fetch: async (url, config) => {
      calls.push({url, config});
      if (calls.length === 1) assert.equal(notices.length, 0);
      return {ok: true, json: async () => ({state: calls.length === 1 ? 'pending' : 'applied'})};
    },
  });
  app.run(slice('let queueSnapshot = null;', 'async function fetchQueue()'));
  app.run('queueSnapshot = {revision: 12}');
  await app.context.deleteQueueItem(0);
  assert.deepEqual(JSON.parse(calls[0].config.body), {
    target_job_ids: ['job-one'], expected_revision: 12, operation_id: 'queue-one',
  });
  assert.equal(calls[1].url, '/api/queue/operations/queue-one');
  assert.deepEqual(notices.map(([, type]) => type), ['success']);
});

test('schedule chip renders the hold, server time, expiry, and handoff', () => {
  const app = fixture({escapeHtml: value => String(value)});
  app.run(slice('function renderScheduleChip(data)', 'function matchingSchedule('));
  const runAt = Date.parse('2026-11-03T02:00:00+07:00') / 1000;
  const chip = app.element('scheduleChip');
  app.context.renderScheduleChip({state: 'holding', target_kind: 'telegram', run_at: runAt, timezone: 'Asia/Bangkok'});
  assert.match(chip.innerHTML, /Đang giữ các video/);
  assert.match(chip.innerHTML, /Asia\/Bangkok/);
  assert.ok(chip.innerHTML.includes(new Date(runAt * 1000).toLocaleString('vi-VN', {timeZone: 'Asia/Bangkok'})));
  app.context.renderScheduleChip({state: 'expired'});
  assert.match(chip.innerHTML, /quá hạn/);
  app.context.renderScheduleChip({state: 'fired'});
  assert.match(chip.innerHTML, /Đã bàn giao/);
  app.context.renderScheduleChip({active: false});
  assert.equal(chip.style.display, 'none');
});

test('explicit retry reuses a queue operation ID after a lost request', async () => {
  const sent = [];
  const app = fixture({
    queueMoving: false, queueDeleteIds: ['job-one'], confirm: () => true,
    newOperationId: () => 'same-queue-operation', showToast: () => {}, fetchQueue: async () => {},
    setTimeout: fn => fn(),
    fetch: async (url, config) => {
      if (config.method === 'POST') {
        sent.push(config.body);
        if (sent.length === 1) throw new Error('offline');
        return {ok: true, json: async () => ({state: 'pending'})};
      }
      if (sent.length === 1) return {status: 404};
      return {ok: true, json: async () => ({state: 'applied'})};
    },
  });
  app.run(slice('let queueSnapshot = null;', 'async function fetchQueue()'));
  app.run('queueSnapshot = {revision: 18}');
  await app.context.deleteQueueItem(0);
  await app.context.deleteQueueItem(0);
  assert.equal(sent.length, 2);
  assert.equal(sent[0], sent[1]);
  assert.equal(app.context.sessionStorage.getItem('dashboardQueueOperation'), null);
});
