const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../../backend/templates/dashboard.html'), 'utf8');
const source = html.slice(html.indexOf('function renderPipelineUI('), html.indexOf('let lastStatusData = null;'));

function harness() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        textContent: '', innerHTML: '', style: {}, dataset: {}, attributes: {},
        classList: { add: (...values) => values.forEach(x => classes.add(x)),
          remove: (...values) => values.forEach(x => classes.delete(x)), contains: x => classes.has(x) },
        setAttribute(key, value) { this.attributes[key] = value; },
        removeAttribute(key) { delete this.attributes[key]; },
      });
    }
    return elements.get(id);
  }
  const context = { document: { getElementById: element }, escapeHtml: x => String(x),
    formatTime: x => String(x), displayedVideoKey: null, w1DisplayedKey: null, w2DisplayedKey: null };
  vm.runInNewContext(source, context);
  return { element, render: (prefix, data, global = {}) => context.renderPipelineUI(prefix, data, global) };
}

function running(overrides = {}) {
  return { active: true, status: 'running', video_status: 'running', job_id: 'job-one',
    video_name: 'one.mp4', start_time: 100, step: 9, stage: 'quality_gate', total_steps: 9,
    percent: 98, step_name: 'Bước 9/9: OCR hậu kiểm', step_durations: { video_rendering: 30, quality_gate: 4 },
    ...overrides };
}

for (const prefix of ['', 'w1', 'w2']) {
  test(`${prefix || 'single'} shows render completed while QC progresses separately`, () => {
    const { render, element } = harness();
    const id = suffix => prefix ? prefix + suffix[0].toUpperCase() + suffix.slice(1) : suffix;
    render(prefix, running());
    assert.ok(element(id('step8')).classList.contains('completed'));
    assert.ok(element(id('step9')).classList.contains('active'));
    assert.equal(element(id('step9')).attributes['aria-current'], 'step');
    assert.equal(element(id('timeStep8')).textContent, '✓ 30.0s');
    assert.equal(element(id('timeStep9')).textContent, 'Đang chạy · 4.0s');
    assert.equal(element(id('pctText')).textContent, '98%');
    assert.equal(element(id('progressBar')).style.transform, 'scaleX(0.98)');
    assert.equal(element(id('step9')).style.display, '');
  });
}

test('QC failure or cancellation never turns into completion or 100%', () => {
  const { render, element } = harness();
  for (const status of ['error', 'stopped', 'interrupted']) {
    render('', running({ status, active: false, video_status: status, percent: 100 }));
    assert.equal(element('pctText').textContent, '99%');
    assert.equal(element('step9').classList.contains('completed'), false);
    assert.ok(element('step9').classList.contains(status === 'error' ? 'failed' : 'stopped'));
  }
});

test('verified publication completes QC and a new job clears its timer and classes', () => {
  const { render, element } = harness();
  render('', running({ active: false, stage: 'publish', video_status: 'completed', percent: 100 }));
  assert.equal(element('pctText').textContent, '100%');
  assert.ok(element('step9').classList.contains('completed'));
  render('', running({ job_id: 'job-two', video_name: 'two.mp4', start_time: 200,
    step: 1, stage: 'extract_audio', percent: 10, step_durations: {} }));
  assert.equal(element('pctText').textContent, '10%');
  assert.equal(element('timeStep9').textContent, '--');
  assert.equal(element('step9').classList.contains('completed'), false);
  assert.equal(element('step9').classList.contains('active'), false);
});

test('eight-stage payload hides QC and QC words take precedence over OCR fallback', () => {
  const { render, element } = harness();
  render('', running({ step: 8, stage: 'video_rendering', total_steps: 8, percent: 95 }));
  assert.equal(element('step9').style.display, 'none');
  assert.ok(element('step8').classList.contains('active'));
  render('', running({ step: 0, stage: undefined, step_name: 'OCR hậu kiểm QC' }));
  assert.equal(element('step9').style.display, '');
  assert.ok(element('step9').classList.contains('active'));
  assert.equal(element('step4').classList.contains('active'), false);
});

test('two workers retain independent render and QC clocks', () => {
  const { render, element } = harness();
  render('w1', running());
  render('w2', running({ job_id: 'job-two', step: 8, stage: 'video_rendering', percent: 95,
    step_durations: { '8': 12 } }));
  assert.ok(element('w1Step9').classList.contains('active'));
  assert.ok(element('w2Step8').classList.contains('active'));
  assert.equal(element('w1TimeStep8').textContent, '✓ 30.0s');
  assert.equal(element('w2TimeStep8').textContent, 'Đang chạy · 12.0s');
  assert.equal(element('w2TimeStep9').textContent, '--');
});
