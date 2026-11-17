"""Stop ACK is immediate while verified cleanup retains exclusive control."""
import io
import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def controller(monkeypatch):
    import tool_control

    monkeypatch.setattr(tool_control, "LOCK", threading.Lock())
    monkeypatch.setattr(tool_control, "_STOP_OPERATIONS", {})
    monkeypatch.setattr(tool_control, "request_service_stop", lambda *args, **kwargs: None)
    return tool_control


def post(controller, payload):
    body = json.dumps(payload).encode()
    responses = []
    handler = SimpleNamespace(path="/control", rfile=io.BytesIO(body),
        headers={"Host": "127.0.0.1:8090", "X-Control-Token": controller.TOKEN,
                 "Content-Length": str(len(body))},
        trusted=lambda: True, send=lambda code, data: responses.append((code, data)))
    controller.Handler.do_POST(handler)
    return responses[0]


def wait_for_cleanup(controller):
    deadline = time.monotonic() + 2
    while controller.LOCK.locked() and time.monotonic() < deadline:
        time.sleep(.005)
    assert not controller.LOCK.locked(), "Cleanup must release exclusive control"


@pytest.mark.parametrize("action", ["off", "off_all"])
def test_stop_ack_precedes_cleanup_and_rejects_restart(controller, monkeypatch, action):
    entered, release = threading.Event(), threading.Event()
    def cleanup(key):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(controller, "stop_and_release", cleanup)
    monkeypatch.setattr(controller, "processes", lambda key: pytest.fail("Stop polling must not probe processes"))
    try:
        code, data = post(controller, {"key": "v1", "action": action})
        assert code == 202 and data["status"] == data["state"] == "stopping"
        assert entered.wait(.5) and controller.LOCK.locked()
        assert controller.status("v1")["state"] == "stopping"
        assert post(controller, {"key": "v1", "action": "on"})[0] == 409
        assert post(controller, {"key": "v1", "action": "off"})[0] == 409
    finally:
        release.set()
        wait_for_cleanup(controller)
    assert controller._STOP_OPERATIONS["v1"]["state"] == "completed"


def test_cleanup_failure_is_reported_without_claiming_stopped(controller, monkeypatch):
    def cleanup(key):
        raise OSError("worker still cleaning up")
    monkeypatch.setattr(controller, "stop_and_release", cleanup)
    code, _ = post(controller, {"key": "v1", "action": "off"})
    assert code == 202
    wait_for_cleanup(controller)
    monkeypatch.setattr(controller, "processes", lambda key: [])
    monkeypatch.setattr(controller, "get_control_paths", lambda *args: [])
    monkeypatch.setattr(controller, "read_telemetry", lambda key: {})
    monkeypatch.setattr(controller.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    state = controller.status("v1")
    assert state["state"] == "error"
    assert state["operation"]["message"] == "worker still cleaning up"
    assert post(controller, {"key": "v1", "action": "on"})[0] == 409
    monkeypatch.setattr(controller, "stop_and_release", lambda key: None)
    assert post(controller, {"key": "v1", "action": "off"})[0] == 202
    wait_for_cleanup(controller)
    assert controller._STOP_OPERATIONS["v1"]["state"] == "completed"


def test_stop_intent_must_be_durable_before_ack(controller, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    monkeypatch.setattr(controller, "request_service_stop", fail)
    monkeypatch.setattr(controller, "stop_and_release", lambda key: pytest.fail("No cleanup before durable intent"))
    code, data = post(controller, {"key": "v1", "action": "off"})
    assert code == 409 and data["message"] == "disk unavailable"
    assert not controller.LOCK.locked() and controller._STOP_OPERATIONS == {}


def test_control_ui_keeps_stopping_ack_pending(controller):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the embedded control UI regression")
    source = controller.HTML.split("const token =", 1)[1].split("let currentPreviewVoiceId", 1)[0]
    script = r'''
const vm = require('node:vm'), assert = require('node:assert/strict');
const elements = new Map();
const element = id => {if (!elements.has(id)) elements.set(id, {innerHTML:'',textContent:'',className:''});return elements.get(id);};
let posts = 0;
const stopping = {key:'v1',state:'stopping',paused:true,operation:{operation_id:'stop-a',state:'stopping',message:'Still cleaning up'}};
const ctx = vm.createContext({AbortSignal,document:{getElementById:element},sendHeight(){},
  fetch:async(url, opts={}) => {if(opts.method==='POST'){posts++;return {ok:true,status:202,json:async()=>({status:'stopping',message:'Accepted'})};}
    return {ok:true,json:async()=>[stopping]};}});
vm.runInContext(SOURCE, ctx);
vm.runInContext("states = [{key:'v1',state:'running',paused:false}];", ctx);
(async()=>{
  await ctx.toggleTool('v1',true);
  assert.equal(posts,1);
  assert.equal(element('message').className,'');
  assert.equal(element('message').textContent,'Still cleaning up');
  assert.match(element('tools').innerHTML,/Đang dừng/);
  assert.match(element('tools').innerHTML,/disabled/);
  assert.match(element('tools').innerHTML,/aria-checked="true"/);
  assert.doesNotMatch(element('tools').innerHTML,/Dashboard: Offline/);
  await ctx.toggleTool('v1',false);
  assert.equal(posts,1);
  stopping.state='error';stopping.operation.state='error';stopping.operation.message='Cleanup failed';
  await ctx.refresh();
  assert.equal(element('message').className,'error');
  assert.match(element('tools').innerHTML,/Chưa dừng hoàn tất/);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''.replace("SOURCE", json.dumps("const token =" + source))
    result = subprocess.run([node, "-"], input=script, capture_output=True, text=True, timeout=10,
                            encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
