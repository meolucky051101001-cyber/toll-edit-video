const test = require('node:test');
const assert = require('node:assert/strict');
const { html, fixture } = require('./js/dashboard_fixture.cjs');

function loadDashboard() {
  const app = fixture();
  for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) app.run(match[1]);
  return app;
}

test('dashboard milestones, logs and stage changes', () => {
  const {context, element, run} = loadDashboard();
  const state = {active:true, status:'running', job_id:'batch', video_name:'one.mp4', queue_index:1,
    queue_total:2, start_time:1, video_status:'running'};
  for (const [step, percent] of [[1,10],[2,25],[3,40],[4,55],[5,65],[6,75],[7,85],[8,95]]) {
    context.renderStatus({...state, step});
    assert.equal(element('pctText').textContent, percent + '%');
    assert.equal(element('progressBar').style.transform, 'scaleX(' + percent / 100 + ')');
    assert(element('step' + step).classList.contains('active'));
  }
  context.renderStatus({...state, total_steps:6, step:3.5});
  assert(element('step4').classList.contains('active'));
  context.renderStatus({...state, step:8, video_status:'completed'});
  assert.equal(element('pctText').textContent, '100%');
  context.renderStatus({...state, video_name:'two.mp4', start_time:2, queue_index:2, step:1});
  assert.equal(element('pctText').textContent, '10%');
  assert(!element('step8').classList.contains('completed'));
  assert(element('currentVideoName').textContent.includes('2/2'));
  const sample = '2026-09-08 01:25:34,633 - v1.performance - INFO - stage=voice seconds=35.53 success=True\n2026-09-08 01:25:35,000 - render - ERROR - NVENC failed <script>alert(1)</script>\n  traceback continuation\n2026-09-08 01:25:36,000 - model - INFO - HubertModel Config: {}';
  const parsed = context.parseLogs(sample);
  assert.equal(parsed.length, 3);
  assert(parsed[1].raw.includes('traceback continuation'));
  assert.match(context.explainLog(parsed[0]), /Lồng tiếng AI.*Hoàn tất thành công/);
  assert(context.explainLog(parsed[1]).includes('NVENC failed'));
  assert.match(context.explainLog(parsed[2]), /HubertModel Config/);
  context.sample = sample;
  run('logText = sample; renderLogs()');
  assert(!element('logTableBody').innerHTML.includes('<script>'));
  assert(element('logTableBody').innerHTML.includes('&lt;script&gt;'));
  element('logFilter').value = 'error';
  context.renderLogs();
  assert.match(element('logSummaryBadge').textContent, /^1 \/ 3/);
  element('logSearch').value = 'không tìm thấy';
  context.renderLogs();
  assert(element('logTableBody').innerHTML.includes('Không có nhật ký phù hợp'));
  element('logSearch').value = '';
  element('logFilter').value = 'all';
  context.renderLogs();
  assert.match(element('logSummaryBadge').textContent, /^3 \/ 3/);
  context.observeProgressLog({...state, step:2, step_name:'Demucs đang tách giọng', updated_at:1788800000});
  context.observeProgressLog({...state, step:3, step_name:'Whisper đang nhận diện', updated_at:1788800001});
  const before = element('logTableBody').innerHTML;
  assert(before.includes('Demucs đang tách giọng'));
  assert(before.includes('Whisper đang nhận diện'));
  context.observeProgressLog({...state, step:3, step_name:'Whisper đang nhận diện', updated_at:1788800002});
  assert.equal(element('logTableBody').innerHTML, before);
  context.observeProgressLog({...state, video_name:'next.mp4', step:1, step_name:'Tách âm', updated_at:1788800010});
  assert(!element('logTableBody').innerHTML.includes('Whisper đang nhận diện'));
});
