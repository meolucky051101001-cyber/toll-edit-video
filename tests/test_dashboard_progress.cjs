const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {style: {}, textContent: '', innerHTML: '', offsetWidth: 1,
      addEventListener() {}, setAttribute() {}, removeAttribute() {},
      classList: {add: x => classes.add(x), remove: (...xs) => xs.forEach(x => classes.delete(x)),
        contains: x => classes.has(x)}});
  }
  return elements.get(id);
}
const html = fs.readFileSync(path.join(__dirname, '../backend/templates/dashboard.html'), 'utf8');
const context = vm.createContext({document: {getElementById: element},
  fetch: () => new Promise(() => {}), setInterval() {}, console, AbortSignal});
for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInContext(match[1], context);
const state = {active:true, job_id:'batch', video_name:'one.mp4', queue_index:1,
  queue_total:2, start_time:1, video_status:'running'};
for (const [step, percent, id] of [[1,10,'step1'],[2,25,'step2'],[3,40,'step3'],
    [3.5,55,'step35'],[4,70,'step4'],[5,85,'step5'],[6,95,'step6']]) {
  context.renderStatus({...state, step});
  assert.equal(element('progressBar').style.width, percent + '%');
  assert(element(id).classList.contains('active'));
}
context.renderStatus({...state, step:6, video_status:'completed'});
assert.equal(element('progressBar').style.width, '100%');
context.renderStatus({...state, video_name:'two.mp4', start_time:2, queue_index:2, step:1});
assert.equal(element('progressBar').style.width, '10%');
assert(!element('step6').classList.contains('completed'));
assert(element('currentVideoName').textContent.includes('2/2'));
console.log('PASS: seven step milestones, completion, next-video reset and queue label');
const sample = '2026-09-08 01:25:34,633 - v1.performance - INFO - stage=voice seconds=35.53 success=True\n2026-09-08 01:25:35,000 - render - ERROR - NVENC failed <script>alert(1)</script>\n  traceback continuation\n2026-09-08 01:25:36,000 - model - INFO - HubertModel Config: {}';
const parsed = context.parseLogs(sample);
assert.equal(parsed.length, 3);
assert(parsed[1].raw.includes('traceback continuation'));
assert(context.explainLog(parsed[0]).includes('Lồng tiếng hoàn tất'));
assert(context.explainLog(parsed[1]).includes('NVENC failed'));
assert(context.explainLog(parsed[2]).includes('Thông số khởi tạo'));
context.sample = sample;
vm.runInContext('logText = sample; renderLogs()', context);
assert(!element('terminalLogs').innerHTML.includes('<script>'));
assert(element('terminalLogs').innerHTML.includes('&lt;script&gt;'));
element('logFilter').value = 'error';
context.renderLogs();
assert(element('logSummary').textContent.startsWith('1/3'));
element('logSearch').value = 'không tìm thấy';
context.renderLogs();
assert(element('terminalLogs').innerHTML.includes('Không có nhật ký phù hợp'));
context.clearLogs();
assert(element('logSummary').textContent.startsWith('3/3'));
console.log('PASS: log parsing, multiline, Vietnamese summaries, error priority, HTML escaping, filters and reset');
context.observeProgressLog({...state, step:2, step_name:'Demucs đang tách giọng', updated_at:1788800000});
context.observeProgressLog({...state, step:3, step_name:'Whisper đang nhận diện', updated_at:1788800001});
const before = element('terminalLogs').innerHTML;
assert(before.includes('Demucs đang tách giọng'));
assert(before.includes('Whisper đang nhận diện'));
context.observeProgressLog({...state, step:3, step_name:'Whisper đang nhận diện', updated_at:1788800002});
assert.equal(element('terminalLogs').innerHTML, before);
context.observeProgressLog({...state, video_name:'next.mp4', step:1, step_name:'Tách âm', updated_at:1788800010});
assert(!element('terminalLogs').innerHTML.includes('Whisper đang nhận diện'));
console.log('PASS: live stage log, duplicate polling suppression, next-video reset');
