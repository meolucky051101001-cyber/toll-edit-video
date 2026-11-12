const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../backend/templates/dashboard.html'), 'utf8');

test('delete waits for acknowledgement and sends stable identity', async () => {
  const start = html.indexOf('async function deleteQueueItem(index)');
  const end = html.indexOf('async function fetchQueue()', start);
  const calls = [], toasts = [];
  const context = {
    window: { queueDeleteIds: ['job-one'] }, queueMoving: false,
    confirm: () => true, setTimeout: resolve => resolve(),
    showToast: (...args) => toasts.push(args), fetchQueue: async () => {},
    fetch: async (url, config) => {
      calls.push({url, config});
      return {ok: true, json: async () => calls.length === 1
        ? {status: 'pending', operation_id: 'one', message: 'Waiting'} : {state: 'applied'}};
    }
  };
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  await context.deleteQueueItem(0);
  assert.equal(JSON.parse(calls[0].config.body).job_id, 'job-one');
  assert.equal(calls[1].url, '/api/queue/operations/one');
  assert.equal(toasts[0][1], 'info');
  assert.equal(toasts[1][1], 'success');
});

test('schedule chip displays server date and marks expired schedule', () => {
  const start = html.indexOf('function renderScheduleChip(d)');
  const end = html.indexOf('async function scheduleNightBatch(', start);
  const chip = {style: {}};
  const context = {document: {getElementById: () => chip}, escapeHtml: x => x};
  vm.createContext(context);
  vm.runInContext(html.slice(start, end), context);
  const runAt = new Date('2026-11-03T02:00:00+07:00').getTime() / 1000;
  context.renderScheduleChip({active: true, time: '02:00', run_at: runAt});
  assert.ok(chip.innerHTML.includes(new Date(runAt * 1000).toLocaleString()));
  context.renderScheduleChip({active: false, state: 'expired'});
  assert.match(chip.textContent, /quá hạn/);
  context.renderScheduleChip({active: false, state: 'fired'});
  assert.equal(chip.style.display, 'none');
});
