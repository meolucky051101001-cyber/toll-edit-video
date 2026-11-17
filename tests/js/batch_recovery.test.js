const test = require('node:test');
const assert = require('node:assert/strict');
const {fixture, slice} = require('./dashboard_fixture.cjs');

function appFor(fetch, initial) {
  const notices=[];
  const app=fixture({fetch,showToast:(...args)=>notices.push(args),escapeHtml:s=>String(s ?? ''),
    closePreRunSummaryModal(){},clearPhoiSelection(){},fetchPhoi(){},fetchStatus(){},
    markDashboardRead(){},waitForV1Settings:async()=>{},confirm:()=>true,prompt:()=>'03:00',setTimeout:fn=>fn()});
  if(initial) app.context.sessionStorage.setItem('dashboardPendingBatch',JSON.stringify(initial));
  app.run("let isSubmittingBatch=false; const selectedPhoiFiles=new Set(['A.mp4']); const videoDraftsCache={}; const currentActivePresetId=null; const dashboardInputPath='input'; const dashboardOutputPath='output'; const currentV1Mode='AUTO';");
  app.run(slice('    let pendingBatchIntent = null;', '    /* =========================================================================='));
  app.run(slice('    let pendingScheduleIntent = null', '    /* =========================================================================='));
  return Object.assign(app,{notices});
}
const reply=(data,status=200)=>({ok:status<400,status,json:async()=>data});
const prepared={batch_id:'batch_a',state:'prepared',revision:1,action_identity:{expected_revision:1},items:[{filename:'A.mp4'}]};
const accepted={...prepared,state:'accepted',execution_id:'exec_a'};

test('busy prepared retry sends same frozen payload and does not trust result cache', async()=>{
  const posts=[]; let state=null;
  const app=appFor(async(url,opts={})=>{
    if(url==='/api/jobs/batch'){
      posts.push(opts.body); state=posts.length===1?prepared:accepted;
      return reply(posts.length===1?{code:'pipeline_busy'}:state,posts.length===1?409:202);
    }
    if(url.startsWith('/api/batch-operations/')) return reply(state);
    throw Error('Unexpected '+url);
  });
  await app.context.confirmBulkEnqueue();
  assert.match(app.element('summarySubmitStatus').textContent,/đang bận/);
  await app.context.confirmBulkEnqueue();
  assert.equal(posts.length,2); assert.equal(posts[0],posts[1]);
  assert.equal(app.context.sessionStorage.getItem('dashboardPendingBatch'),null);
});

test('reload with cached prepared only reads on load and retries with same key on click',async()=>{
  const initial={sent:true,schedule:false,payload:{idempotency_key:'original',items:[{filename:'A.mp4'}],trigger:true},result:prepared};
  const calls=[];
  const app=appFor(async(url,opts={})=>{calls.push([url,opts]);return reply(opts.method==='POST'?accepted:prepared);},initial);
  assert.equal(calls.length,0);
  await app.context.retryPendingBatch();
  assert.equal(calls[0][0],'/api/batch-operations/original');
  assert.equal(JSON.parse(calls[1][1].body).idempotency_key,'original');
});

test('lost admission response reconciles once without another POST',async()=>{
  let posts=0;
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='POST'){posts++;throw Error('lost response');}
    return reply(accepted);
  });
  await app.context.confirmBulkEnqueue();
  assert.equal(posts,1);assert.equal(app.context.sessionStorage.getItem('dashboardPendingBatch'),null);
});

test('unknown POST result retains key and explicit retry after 404 sends identical payload',async()=>{
  const bodies=[];
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='POST'){bodies.push(opts.body);if(bodies.length===1)throw Error('offline');return reply(accepted);}
    return reply({},404);
  });
  await app.context.confirmBulkEnqueue();
  assert.ok(app.context.sessionStorage.getItem('dashboardPendingBatch'));
  await app.context.confirmBulkEnqueue();
  assert.deepEqual(bodies,[bodies[0],bodies[0]]);
});

test('failed server state supersedes cached accepted and releases submission without rerun',async()=>{
  const methods=[];
  const app=appFor(async(url,opts={})=>{methods.push(opts.method||'GET');return reply({...prepared,state:'failed'});},
    {sent:true,schedule:false,payload:{idempotency_key:'old',items:[{filename:'A.mp4'}]},result:accepted});
  await app.context.confirmBulkEnqueue();
  assert.deepEqual(methods,['GET','GET']);
  assert.equal(app.context.sessionStorage.getItem('dashboardPendingBatch'),null);
  assert.match(app.element('recoverableBatches').innerHTML,/Batch thất bại/);
});

test('double click cannot submit a second request while first is in flight',async()=>{
  let release; const wait=new Promise(resolve=>release=resolve);let posts=0;
  const app=appFor(async()=>{posts++;await wait;return reply(accepted);});
  const first=app.context.confirmBulkEnqueue();
  await app.context.confirmBulkEnqueue();
  assert.equal(posts,1);release();await first;
});

test('GET failure preserves the pending intent without a mutation',async()=>{
  let calls=0;
  const initial={sent:true,schedule:false,payload:{idempotency_key:'old',items:[{filename:'A.mp4'}]},result:prepared};
  const app=appFor(async(_url,opts={})=>{assert.notEqual(opts.method,'POST');calls++;throw Error('offline');},initial);
  await app.context.confirmBulkEnqueue();
  assert.equal(calls,1);assert.equal(JSON.parse(app.context.sessionStorage.getItem('dashboardPendingBatch')).payload.idempotency_key,'old');
});

test('canceled saved batch exposes all three explicit actions after reload',async()=>{
  const held={...prepared,state:'scheduled',schedule:{state:'canceled'},actions:['start','reschedule','abandon']};
  const app=appFor(async()=>reply({items:[held]}));
  await app.context.fetchRecoverableBatches();
  const text=app.element('recoverableBatches').innerHTML;
  assert.match(text,/Chạy batch đã lưu/);assert.match(text,/Đặt lại lịch/);assert.match(text,/Bỏ batch đã lưu/);
});

test('schedule operation matching uses operation ID rather than only batch ID',()=>{
  const app=appFor(async()=>{throw Error('unexpected');});
  assert.equal(app.context.matchingSchedule({operation_id:'old',batch_id:'A'},{operation_id:'new',batch_id:'A'}),false);
  // schedule_id in reschedule payload is the OLD expected binding, not new identity.
  assert.equal(app.context.matchingSchedule({operation_id:'new',batch_id:'A',schedule_id:'new-sid',generation:2},
    {operation_id:'new',batch_id:'A',schedule_id:'old-sid',schedule_generation:1}),true);
});

test('lost cancel response queries exact operation and preserves newer current schedule',async()=>{
  const old={state:'pending',schedule_id:'old_sid',generation:1,operation_id:'old_op'};
  const newer={state:'pending',schedule_id:'new_sid',generation:2,operation_id:'new_op'};
  const mutations=[];
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='DELETE'){mutations.push(JSON.parse(opts.body));throw Error('lost');}
    if(url==='/api/schedule-operations/old_op')return reply({...old,state:'canceled'});
    if(url==='/api/schedule')return reply(newer);
    if(url==='/api/batches?recoverable=true')return reply({items:[]});
    throw Error(url);
  });
  app.context.renderScheduleChip(old);
  await app.context.cancelNightBatch();
  assert.deepEqual(mutations,[{schedule_id:'old_sid',generation:1,operation_id:'old_op'}]);
  assert.equal(app.context.sessionStorage.getItem('dashboardScheduleCancel'),null);
  assert.equal(app.run('lastScheduleData.schedule_id'),'new_sid');
});

test('saved batch start uses displayed CAS identity and reconciles a lost acknowledgement',async()=>{
  const identity={expected_revision:4,schedule_id:'old-sid',schedule_generation:1,schedule_operation_id:'old-op'};
  const held={...prepared,state:'scheduled',actions:['start','reschedule','abandon'],action_identity:identity};
  const posts=[];
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='POST'){posts.push(JSON.parse(opts.body));throw Error('lost ACK');}
    if(url==='/api/batches/batch_a')return reply(accepted);
    if(url==='/api/schedule')return reply({active:false});
    if(url==='/api/batches?recoverable=true')return reply({items:[]});
    throw Error(url);
  });
  app.run('recoverableBatchData.set("batch_a",'+JSON.stringify(held)+')');
  await app.context.recoverBatchAction('batch_a','start');
  assert.deepEqual(posts,[identity]);
  assert.ok(app.notices.some(([message,kind])=>kind==='success'&&message.includes('tiếp nhận')));
});

test('reschedule lost ACK replays by new operation ID with original time and binding',async()=>{
  let posted;
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='POST'){posted=JSON.parse(opts.body);throw Error('lost ACK');}
    assert.equal(url,'/api/schedule-operations/'+posted.operation_id);
    return reply({state:'pending',active:true,binding_confirmed:true,operation_id:posted.operation_id,batch_id:'batch_a',
      schedule_id:'new-sid',generation:2,run_at:1800100000,timezone:'Asia/Bangkok'});
  });
  const result=await app.context.scheduleNightBatch('03:00',{target_kind:'local',batch_id:'batch_a',expected_revision:4,
    schedule_id:'old-sid',schedule_generation:1,schedule_operation_id:'old-op'});
  assert.equal(result.schedule_id,'new-sid');
  assert.equal(posted.hhmm,'03:00');assert.equal(posted.schedule_id,'old-sid');
  assert.equal(app.context.sessionStorage.getItem('dashboardScheduleIntent'),null);
});

test('half-committed local binding is not acknowledged and next click repairs with same POST',async()=>{
  const posts=[];let operation;
  const app=appFor(async(url,opts={})=>{
    if(opts.method==='POST'){
      posts.push(opts.body);operation=JSON.parse(opts.body).operation_id;
      if(posts.length===1)return reply({detail:'write failed'},503);
      return reply({state:'pending',active:true,binding_confirmed:true,operation_id:operation,batch_id:'batch_a',run_at:1800100000});
    }
    assert.equal(url,'/api/schedule-operations/'+operation);
    return reply({state:'pending',active:true,binding_confirmed:false,operation_id:operation,batch_id:'batch_a',run_at:1800100000});
  });
  assert.equal(await app.context.scheduleNightBatch('03:00',{target_kind:'local',batch_id:'batch_a',expected_revision:1}),null);
  assert.equal(posts.length,1);assert.ok(app.context.sessionStorage.getItem('dashboardScheduleIntent'));
  const result=await app.context.retryPendingSchedule();
  assert.equal(result.binding_confirmed,true);assert.deepEqual(posts,[posts[0],posts[0]]);
  assert.equal(app.context.sessionStorage.getItem('dashboardScheduleIntent'),null);
});

test('pause/resume sends the exact displayed execution identity',async()=>{
  const bodies=[];
  const app=fixture({lastStatusData:{execution_id:'viewed-execution',generation:7},alert:message=>assert.fail(message),
    fetch:async(_url,opts)=>{bodies.push(JSON.parse(opts.body));return reply({message:'ok'});}});
  app.run(slice('    async function controlVideo(action)', '    async function stopBatch()'));
  await app.context.controlVideo('pause');await app.context.controlVideo('resume');
  assert.deepEqual(bodies,[{execution_id:'viewed-execution',generation:7},{execution_id:'viewed-execution',generation:7}]);
});

test('unverified local sources cannot be manually released through process queue',async()=>{
  const sent=[];
  const app=fixture({queueSnapshot:{items:[{job_id:'bad',hold:{kind:'manual',reason_code:'missing_local_source'}},
    {job_id:'good',hold:{kind:'manual'}}]},submitQueueOperation:(...args)=>sent.push(args),showToast(){}});
  app.run(slice('    async function processQueue()', '    async function clearQueue()'));
  await app.context.processQueue();
  assert.equal(sent[0][0],'process');assert.deepEqual(Array.from(sent[0][1]),['good']);
});
