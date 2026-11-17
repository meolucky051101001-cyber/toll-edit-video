const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const html=fs.readFileSync(path.join(__dirname,'../../backend/templates/cloud_dashboard.html'),'utf8');
const code=html.slice(html.indexOf('    let workerActive = true;'),html.indexOf('    // Submit Enqueue'));
function fixture(job) {
  const elements=new Map();
  function element(id) {
    if(!elements.has(id))elements.set(id,{style:{},innerHTML:'',innerText:'',className:''});
    return elements.get(id);
  }
  const app=vm.createContext({document:{getElementById:element},console:{error:(...args)=>assert.fail(args.join(' '))},
    fetch:async url=>({json:async()=>url.startsWith('/api/cloud/jobs')?{status:'ok',jobs:[job]}:
      {status:'ok',active_job:job,worker_running:false,sessions:[],usage:{}}})});
  vm.runInContext(code,app);
  return {app,element};
}

test('cloud cleanup failure shows unconfirmed resources in both hero and history',async()=>{
  const job={job_id:'cloud-test',video_name:'A.mp4',gpu_type:'T4',voice:'fixture',state:'stop_incomplete',
    publication_state:'completed',output_committed:true,session_cleanup:'unconfirmed',percent:100,step_name:'old progress'};
  const {app,element}=fixture(job);
  await app.loadStatus();await app.loadJobs();
  assert.match(element('hero-status-heading').innerText,/Chưa xác nhận thu hồi/);
  assert.match(element('hero-step-desc').innerText,/Thành phẩm đã được lưu/);
  assert.match(element('hero-step-desc').innerText,/hàng đợi vẫn giữ chỗ/);
  assert.equal(element('hero-btn-stop').style.display,'none');
  assert.notEqual(element('step-8').className,'step-box step-completed');
  const table=element('table-jobs-body').innerHTML;
  assert.match(table,/Chưa xác nhận thu hồi/);assert.match(table,/Chờ đối soát/);
  assert.doesNotMatch(table,/Hoàn tất|deleteJob\(|cancelJob\(/);
});

test('cancel and recovery labels describe acknowledgement instead of completion',()=>{
  const {app}=fixture({});
  assert.match(app.getStateBadge('cancel_requested'),/Đã yêu cầu hủy/);
  assert.match(app.getStateBadge('stopping'),/Đang dừng và thu hồi/);
  assert.match(app.getStateBadge('recovery_required'),/Cần đối soát/);
  assert.equal(app.canCancelCloudJob({state:'validating'}),true);
  assert.equal(app.canDeleteCloudJob({state:'completed',session_cleanup:'unconfirmed'}),false);
  assert.equal(app.canDeleteCloudJob({state:'completed',session_cleanup:'confirmed'}),true);
});

const cancelCode=html.slice(html.indexOf('    async function emergencyStopAll()'),
  html.indexOf('    // Save CU Budget Limit'));
function cancellationFixture(ok,payload) {
  const alerts=[];
  const reads=[];
  const app=vm.createContext({confirm:()=>true,alert:message=>alerts.push(message),
    fetch:async(url,options)=>{
      assert.equal(url,'/api/cloud/stop');
      assert.equal(options.method,'POST');
      return {ok,json:async()=>payload};
    },loadStatus:async()=>reads.push('status'),loadJobs:async()=>reads.push('jobs')});
  vm.runInContext(cancelCode,app);
  return {app,alerts,reads};
}

test('cloud cancellation never acknowledges HTTP errors or malformed success payloads',async()=>{
  const stopped={job_id:'queued',accepted:true,state:'stopped'};
  for(const [ok,payload] of [
    [false,{status:'ok',jobs:[stopped],message:'Đã dừng tất cả'}],
    [true,{status:'error',message:'Đã dừng tất cả'}],
    [true,{status:'ok'}],
    [true,{status:'ok',jobs:[null]}],
    [true,{status:'ok',jobs:[{...stopped,accepted:'true'}]}],
    [true,{status:'ok',jobs:[{...stopped,state:'publishing'}]}],
    [true,null]
  ]) {
    const {app,alerts,reads}=cancellationFixture(ok,payload);
    await app.emergencyStopAll();
    assert.equal(alerts.length,1);
    assert.match(alerts[0],/^Không thể xác nhận hủy:/);
    assert.doesNotMatch(alerts[0],/Đã nhận yêu cầu|Đã dừng tất cả/);
    assert.deepEqual(reads,[]);
  }
});

test('cloud cancellation separates stopped jobs, pending cleanup and rejected publication',async()=>{
  const {app,alerts,reads}=cancellationFixture(true,{status:'ok',jobs:[
    {job_id:'queued',accepted:true,state:'stopped'},
    {job_id:'active',accepted:true,state:'cancel_requested'},
    {job_id:'published',accepted:false,state:'publishing'}
  ]});
  await app.emergencyStopAll();
  assert.equal(alerts.length,1);
  assert.match(alerts[0],/Đã nhận yêu cầu hủy: 2 job/);
  assert.match(alerts[0],/Đã dừng: 1 job/);
  assert.match(alerts[0],/Chờ worker xác nhận dừng và thu hồi tài nguyên: 1 job/);
  assert.match(alerts[0],/Không nhận yêu cầu hủy: 1 job/);
  assert.deepEqual(reads,['status','jobs']);
});
