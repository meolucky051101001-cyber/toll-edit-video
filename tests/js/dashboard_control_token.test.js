const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../../backend/templates/dashboard.html'), 'utf8');
const wrapper = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('__REPLACE_TOKEN__', 'fixture-token');

test('dashboard adds control token to all five controls with object or Headers config', async () => {
  const calls = [];
  const window = { fetch: async (resource, config) => { calls.push({ resource, config }); return { ok: true }; } };
  vm.runInNewContext(wrapper, { window, Headers, setTimeout });
  for (const endpoint of ['stop-batch', 'pause-video', 'resume-video', 'audio-settings', 'worker-settings']) {
    for (const headers of [undefined, { 'Content-Type': 'application/json' }, new Headers({ 'X-Dashboard-Input': '1' })]) {
      await window.fetch('/api/' + endpoint, { method: 'POST', headers });
      assert.equal(new Headers(calls.at(-1).config.headers).get('X-Local-Control-Token'), 'fixture-token');
    }
  }
  assert.equal(calls.length, 15);
});
