"""Local control plane for the two fixed AutoDub installations."""
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import psutil
import voice_selection

ROOTS = {"v1": Path(r"C:\tool v1\backend"), "v2": Path(r"C:\tool v2\backend")}
PORTS = {"v1": 8088, "v2": 8089}
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
FLAGS = Path(r"C:\tool v1\workspace\control")
FLAGS.mkdir(parents=True, exist_ok=True)

def processes(key):
    root = ROOTS[key]
    scripts = {str(root / name).lower() for name in ("background_service.py", "main.py", "dashboard_monitor.py", "telegram_bot.py")}
    found = []
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if not name.startswith("python"):
                continue
            args = p.cmdline() or []
            for arg in args[1:]:
                candidate = Path(arg)
                if not candidate.is_absolute():
                    try:
                        candidate = Path(p.cwd()) / candidate
                    except (psutil.Error, OSError):
                        pass
                if str(candidate).lower() in scripts:
                    found.append((p, candidate.name))
                    break
        except (psutil.Error, OSError):
            continue
    return found

def read_telemetry(key):
    try:
        return json.loads((FLAGS / (key + '.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}

def status(key):
    procs = processes(key)
    bots = [p.pid for p, name in procs if name == 'telegram_bot.py']
    data = read_telemetry(key)
    fresh = data.get('pid') in bots and time.time() - data.get('at', 0) < 8
    dashboard = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/api/status' % PORTS[key], timeout=0.8) as response:
            dashboard = response.status == 200
    except Exception:
        pass
    ready = bool(fresh and data.get('polling') and dashboard)
    state = 'running' if ready else 'stopped' if not bots else 'partial'
    return dict(key=key, state=state, dashboard=dashboard, bot=bool(bots),
                heartbeat=bool(fresh), polling=bool(fresh and data.get('polling')),
                busy=bool(data.get('busy')) if fresh else None,
                queue=data.get('queue', 0) if fresh else None,
                paused=(FLAGS / (key + '.pause')).exists())

def launch(key, service):
    root = ROOTS[key]
    pythonw = root / 'venv' / 'Scripts' / 'pythonw.exe'
    if not pythonw.exists():
        pythonw = root / 'venv' / 'Scripts' / 'python.exe'
    subprocess.Popen([str(pythonw), str(root / 'background_service.py'), '--service', service],
                     cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)

def stop_and_release(key):
    """
    Dừng triệt để toàn bộ bot, batch processor, worker và giải phóng toàn bộ RAM/VRAM.
    Được gọi khi người dùng chủ động tắt tool hoặc khi chuyển sang tool khác.
    """
    flag = FLAGS / (key + '.pause')
    flag.write_text('paused', encoding='utf-8')

    root = ROOTS[key]
    root_str = str(root).lower()

    # 1. Nếu là Tool V1, kiểm tra xem có batch đang chạy không
    batch_active = False
    if key == 'v1':
        try:
            with urllib.request.urlopen('http://127.0.0.1:8088/api/status', timeout=0.8) as r:
                batch_active = bool(json.load(r).get('active'))
        except Exception:
            pass
        if batch_active:
            try:
                req = urllib.request.Request(
                    'http://127.0.0.1:8088/api/stop-batch',
                    data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-Dashboard-Input': '1'}
                )
                urllib.request.urlopen(req, timeout=1.0)
            except Exception:
                pass

    # 2. Thu thập toàn bộ tiến trình liên quan đến Tool (trừ tool_control.py)
    targets = {}
    restart_main_needed = batch_active

    for p in psutil.process_iter(['name']):
        try:
            name = (p.info['name'] or '').lower()
            if not (name.startswith('python') or name.startswith('ffmpeg') or name.startswith('ffprobe')):
                continue
            cmd = [c.lower() for c in (p.cmdline() or [])]
            cmd_str = ' '.join(cmd)

            # Bộ điều khiển trung tâm phải luôn sống
            if 'tool_control.py' in cmd_str:
                continue

            is_tool_proc = False
            if root_str in cmd_str or (key == 'v1' and r'c:\tool v1' in cmd_str) or (key == 'v2' and r'c:\tool v2' in cmd_str):
                is_tool_proc = True

            if is_tool_proc:
                # Bot telegram hoặc supervisor telegram
                if 'telegram_bot.py' in cmd_str or ('background_service.py' in cmd_str and 'telegram' in cmd_str):
                    targets[p.pid] = p
                    try:
                        for child in p.children(recursive=True):
                            ch_cmd = ' '.join([c.lower() for c in (child.cmdline() or [])])
                            if 'tool_control.py' not in ch_cmd:
                                targets[child.pid] = child
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                # Tiến trình ffmpeg / ffprobe do tool sinh ra trong lúc xử lý
                elif name.startswith('ffmpeg') or name.startswith('ffprobe'):
                    targets[p.pid] = p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 3. Gửi lệnh terminate tới tất cả các tiến trình mục tiêu
    for pid, p in list(targets.items()):
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Chờ tiến trình dừng
    gone, alive = psutil.wait_procs(list(targets.values()), timeout=2.0)

    # Force kill nếu còn tiến trình sống sót
    if alive:
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        psutil.wait_procs(alive, timeout=1.5)

    # 4. Nếu cần giải phóng VRAM của main.py (khi có batch chạy trên V1):
    if restart_main_needed and key == 'v1':
        for p, name in processes('v1'):
            if name == 'main.py':
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        # background_service sẽ tự động tạo main.py mới trong 0.5s sạch 100% VRAM

    # 5. Ghi đè telemetry thành đã dừng
    telemetry = {
        'pid': None,
        'at': time.time(),
        'busy': False,
        'queue': 0,
        'paused': True,
        'polling': False
    }
    FLAGS.mkdir(parents=True, exist_ok=True)
    try:
        (FLAGS / (key + '.json')).write_text(json.dumps(telemetry), encoding='utf-8')
    except Exception:
        pass

    # Xóa file tạm
    for tmp in FLAGS.glob(key + '.*.tmp'):
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass

    return True

def change(key, action):
    if key not in ROOTS or action not in ('on', 'off'):
        raise ValueError('Yêu cầu không hợp lệ.')
    flag = FLAGS / (key + '.pause')

    if action == 'off':
        stop_and_release(key)
        return f'Đã tắt Tool {key.upper()} và giải phóng toàn bộ RAM & VRAM.'

    if action == 'on':
        other = 'v2' if key == 'v1' else 'v1'
        # Tự động tắt và giải phóng toàn bộ tool đối diện nếu đang chạy để nhường GPU & tránh xung đột
        other_flag = FLAGS / (other + '.pause')
        other_procs = processes(other)
        other_bots = [p for p, name in other_procs if name == 'telegram_bot.py']
        if other_bots or not other_flag.exists():
            stop_and_release(other)
            time.sleep(0.5)

        # Xóa cờ pause để cho phép khởi động
        flag.unlink(missing_ok=True)

        procs_found = processes(key)
        names = [name for _, name in procs_found]

        # Đảm bảo dashboard đang hoạt động
        if not any(n in names for n in ('main.py', 'dashboard_monitor.py')):
            launch(key, 'dashboard')

        # Khởi động telegram bot
        data = read_telemetry(key)
        bots = [p for p, name in procs_found if name == 'telegram_bot.py']
        fresh = any(data.get('pid') == p.pid for p in bots) and (time.time() - data.get('at', 0) < 8)
        if not fresh and bots:
            for b in bots:
                try: b.kill()
                except psutil.NoSuchProcess: pass
        if 'telegram_bot.py' not in names or not fresh:
            launch(key, 'telegram')

        # Chờ tối đa 3 giây để bot khởi động
        for _ in range(6):
            time.sleep(0.5)
            check_procs = processes(key)
            if any(name == 'telegram_bot.py' for _, name in check_procs):
                break

        return f'Đã bật Tool {key.upper()} thành công!'

def change_voice(voice_id):
    for key in ROOTS:
        current = status(key)
        if current['bot'] and (not current['heartbeat'] or current['busy'] or current['queue']):
            raise ValueError('Tool đang xử lý hoặc còn hàng đợi; hãy chờ hoàn tất rồi đổi giọng.')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8088/api/status', timeout=2) as response:
            if json.load(response).get('active'):
                raise ValueError('Batch đang xử lý. Hãy chờ hoàn tất rồi đổi giọng.')
    except (OSError, TimeoutError):
        raise ValueError('Không xác minh được batch V1; chưa đổi giọng.')
    voice_selection.save(voice_id)
    return 'Đã lưu: ' + voice_selection.selected()['label'] + '. Video tiếp theo sẽ dùng giọng này.'

HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Điều khiển V1 / V2</title>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 14px 16px;
    background-color: #131b26;
    color: #f1f5f9;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    line-height: 1.5;
  }
  h2 {
    font-size: 15px;
    font-weight: 700;
    color: #ffffff;
    margin: 0 0 12px;
    letter-spacing: -0.2px;
  }
  .tools {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
  }
  .tool {
    flex: 1 1 calc(50% - 6px);
    min-width: 260px;
    background-color: #1a2433;
    border: 1px solid #2a374a;
    border-radius: 8px;
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .tool > div:first-child {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
  }
  .dot {
    width: 9px;
    height: 9px;
    border-radius: 50%;
    display: inline-block;
    background-color: #64748b;
    flex-shrink: 0;
  }
  .dot.running { background-color: #10b981; box-shadow: 0 0 6px rgba(16,185,129,0.5); }
  .dot.stopped { background-color: #ef4444; }
  .dot.partial { background-color: #f59e0b; box-shadow: 0 0 6px rgba(245,158,11,0.5); }
  small {
    color: #94a3b8;
    font-size: 11.5px;
    line-height: 1.4;
  }
  .tool-actions {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 2px;
    flex-wrap: wrap;
  }
  button, a {
    font-size: 12px;
    border-radius: 6px;
    border: 1px solid #334155;
    background-color: #1e293b;
    color: #f1f5f9;
    padding: 5px 10px;
    text-decoration: none;
    cursor: pointer;
    transition: background-color 0.15s, border-color 0.15s;
  }
  button:hover:not(:disabled), a:hover {
    background-color: #273549;
    border-color: #3b82f6;
  }
  button:disabled {
    opacity: 0.55;
    cursor: wait;
  }
  button[role=switch] {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 0;
    background: transparent;
    padding: 2px 0;
    touch-action: pan-y;
    user-select: none;
    vertical-align: middle;
    cursor: pointer;
  }
  .switch-track {
    display: inline-block;
    position: relative;
    width: 48px;
    height: 26px;
    border-radius: 13px;
    background-color: #334155;
    border: 1px solid #475569;
    transition: background-color 0.25s, border-color 0.25s;
  }
  .switch-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 20px;
    height: 20px;
    border-radius: 50%;
    background-color: #ffffff;
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
  }
  button[role=switch][aria-checked=true] .switch-track {
    background-color: #059669;
    border-color: #10b981;
  }
  button[role=switch][aria-checked=true] .switch-thumb {
    transform: translateX(22px);
  }
  button[role=switch][data-state=partial] .switch-track {
    background-color: #b45309;
    border-color: #f59e0b;
  }
  .switch-label {
    min-width: 28px;
    font-weight: 700;
    font-size: 12px;
    color: #f1f5f9;
  }
  .switch-label.pending {
    color: #fbbf24;
    font-style: italic;
  }
  @media (prefers-reduced-motion: reduce) {
    .switch-track, .switch-thumb { transition: none; }
  }
  #message {
    margin-top: 10px;
    color: #cbd5e1;
    font-size: 12px;
    line-height: 1.4;
  }
  .voice-section {
    margin-top: 14px;
    border-top: 1px solid #233144;
    padding-top: 12px;
  }
  .voice-section label {
    font-size: 13px;
    font-weight: 700;
    color: #ffffff;
    display: block;
    margin-bottom: 4px;
  }
  .voice-controls {
    display: flex;
    gap: 8px;
    align-items: center;
    margin-top: 8px;
    flex-wrap: wrap;
  }
  select {
    background-color: #101622;
    color: #ffffff;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 10px;
    font-size: 12px;
    max-width: 100%;
    outline: none;
  }
  select:focus {
    border-color: #3b82f6;
  }
  #save-voice {
    background-color: #2563eb;
    border-color: #3b82f6;
    color: #ffffff;
  }
  #save-voice:hover:not(:disabled) {
    background-color: #1d4ed8;
  }
  button:focus-visible, a:focus-visible, select:focus-visible {
    outline: 2px solid #38bdf8;
    outline-offset: 2px;
  }
</style>
</head>
<body>
<h2>Tool & Telegram bot</h2>
<div class="tools" id="tools"></div>
<div id="message" role="status">Đang kiểm tra dịch vụ…</div>
<section class="voice-section">
  <label for="voice">Giọng lồng tiếng · dùng chung V1 / V2 và Telegram</label>
  <small>Chọn giọng để tự lưu khi tool rảnh. Giọng đã lưu hiển thị bên dưới.</small>
  <div class="voice-controls">
    <select id="voice" onchange="saveVoice()" disabled></select>
    <button id="save-voice" onclick="saveVoice()" disabled>Lưu giọng</button>
  </div>
  <small id="voice-status" role="status" style="margin-top: 6px;">Đang tải danh sách giọng…</small>
</section>
<script>
const token='__TOKEN__';
let pending=false;
let pendingAction=null;
let states=[];
let dragStart=null;
let suppressClickUntil=0;

function renderTools() {
  const container = document.getElementById('tools');
  if (!container || !states.length) return;
  container.innerHTML = states.map(s => {
    const isThisPending = pending && pendingAction && pendingAction.key === s.key;
    const isOffPending = isThisPending && pendingAction.action === 'off';
    const isOnPending = isThisPending && pendingAction.action === 'on';

    let isChecked = s.state === 'running';
    // Khi đang tắt/giải phóng: giữ công tắc ở bên phải cho tới khi server hoàn tất việc giải phóng!
    if (isOffPending) isChecked = true;
    // Khi đang bật: giữ công tắc ở bên trái cho tới khi server khởi động xong!
    if (isOnPending) isChecked = false;

    let switchLabel = isOffPending ? 'Đang giải phóng…' : isOnPending ? 'Đang bật…' : (s.state === 'running' ? 'Bật' : s.state === 'stopped' ? 'Tắt' : 'Chờ');
    let stateBadge = s.state === 'running' ? 'Đang chạy' : s.state === 'stopped' ? 'Đã tắt' : 'Chưa sẵn sàng';
    if (isOffPending) stateBadge = 'Đang giải phóng RAM/VRAM…';
    if (isOnPending) stateBadge = 'Đang khởi động…';

    const botStatus = s.polling ? 'polling hoạt động' : (s.bot ? 'đang khởi động' : 'đã tắt');
    const queueStatus = s.busy ? ' · Đang xử lý' : (s.queue ? ' · ' + s.queue + ' video chờ' : '');

    return `
      <div class="tool">
        <div>
          <span class="dot ${isThisPending ? 'partial' : s.state}"></span>
          <b>Tool ${s.key.toUpperCase()}</b> · ${stateBadge}
        </div>
        <small>Dashboard: ${s.dashboard ? 'online' : 'offline'} · Bot: ${botStatus}${queueStatus}</small>
        <div class="tool-actions">
          <button role="switch"
            aria-label="Bật tắt Tool ${s.key.toUpperCase()} và Telegram bot"
            aria-checked="${isChecked}"
            data-state="${isThisPending ? 'partial' : s.state}"
            title="Bấm để bật hoặc tắt Tool"
            ${pending ? 'disabled' : ''}
            onclick="toggle('${s.key}', '${s.state === 'running' ? 'off' : 'on'}')">
            <span class="switch-track" aria-hidden="true">
              <span class="switch-thumb"></span>
            </span>
            <span class="switch-label ${isThisPending ? 'pending' : ''}">${switchLabel}</span>
          </button>
          ${s.state === 'partial' && !pending ? `<button onclick="toggle('${s.key}','on')">Khởi động bổ sung</button>` : ''}
          <a target="_top" href="http://127.0.0.1:${s.key === 'v1' ? 8088 : 8089}/">Dashboard ↗</a>
        </div>
      </div>
    `;
  }).join('');
}

async function refresh() {
  try {
    const r = await fetch('/status', { signal: AbortSignal.timeout(6000) });
    if (!r.ok) throw Error();
    states = await r.json();
    if (!pending) renderTools();
  } catch(e) {
    if (!pending) {
      document.getElementById('message').textContent = 'Đang kết nối lại bộ điều khiển...';
    }
  }
}

async function toggle(key, action) {
  if (pending) return;
  pending = true;
  pendingAction = { key, action };
  renderTools();

  const msgElem = document.getElementById('message');
  if (action === 'off') {
    msgElem.textContent = `⏳ Đang dừng Tool ${key.toUpperCase()} và giải phóng toàn bộ RAM, GPU VRAM...`;
  } else {
    msgElem.textContent = `⏳ Đang khởi động Tool ${key.toUpperCase()} và kiểm tra bot...`;
  }

  try {
    const r = await fetch('/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-Token': token },
      body: JSON.stringify({ key, action }),
      signal: AbortSignal.timeout(18000)
    });
    const d = await r.json();
    msgElem.textContent = (r.ok ? '✅ ' : '⚠️ ') + (d.message || 'Thao tác hoàn tất.');
  } catch(e) {
    msgElem.textContent = e.name === 'TimeoutError' ? 'Lệnh đang được hoàn tất ngầm...' : ('Lỗi: ' + (e.message || 'Không gửi được lệnh.'));
  } finally {
    pending = false;
    pendingAction = null;
    await refresh();
    renderTools();
  }
}

async function loadVoices() {
  try {
    const r = await fetch('/voices', { signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw Error();
    const d = await r.json();
    const select = document.getElementById('voice');
    select.replaceChildren();
    for (const source of ['rvc', 'edge', 'capcut']) {
      const group = document.createElement('optgroup');
      group.label = { rvc: 'Giọng mặc định · RVC', edge: 'Microsoft TTS', capcut: 'CapCut TTS' }[source];
      for (const v of d.voices.filter(v => v.source === source)) {
        const option = document.createElement('option');
        option.value = v.id;
        option.textContent = v.label + (v.verified_at ? ' ✓' : '');
        group.append(option);
      }
      select.append(group);
    }
    select.value = d.selected.id;
    select.disabled = false;
    document.getElementById('save-voice').disabled = false;
    document.getElementById('voice-status').textContent = 'Đang lưu: ' + d.selected.label;
  } catch(e) {
    document.getElementById('voice-status').textContent = 'Không tải được cấu hình giọng. Hãy tải lại trang.';
  }
}

async function saveVoice() {
  const button = document.getElementById('save-voice');
  const select = document.getElementById('voice');
  button.disabled = true;
  select.disabled = true;
  try {
    const r = await fetch('/voices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-Token': token },
      body: JSON.stringify({ id: select.value })
    });
    const d = await r.json();
    if (!r.ok) throw Error(d.message || 'Không lưu được giọng');
    await loadVoices();
    document.getElementById('voice-status').textContent = d.message;
  } catch(e) {
    await loadVoices();
    alert('Giọng chưa được đổi: ' + e.message);
  } finally {
    button.disabled = false;
    select.disabled = false;
  }
}

loadVoices();
refresh();
setInterval(() => { if (!pending && !dragStart) refresh(); }, 3000);
document.getElementById('message').textContent = 'Xanh: dashboard + bot hoạt động · Đỏ: bộ xử lý/bot đã tắt · Vàng: đang chuyển trạng thái.';
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send(self, code, data, mime='application/json'):
        raw = data.encode('utf-8') if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', mime + '; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "frame-ancestors http://127.0.0.1:8088 http://localhost:8088 http://127.0.0.1:8089 http://localhost:8089 http://127.0.0.1:8090")
        self.end_headers()
        self.wfile.write(raw)

    def trusted(self):
        return self.headers.get('Host') in ('127.0.0.1:8090', 'localhost:8090')

    def do_GET(self):
        if not self.trusted(): return self.send(403, {'message': 'Invalid host'})
        if self.path == '/': return self.send(200, HTML.replace('__TOKEN__', TOKEN), 'text/html')
        if self.path == '/voices': return self.send(200, {'voices': voice_selection.catalog(), 'selected': voice_selection.selected()})
        if self.path == '/status': return self.send(200, [status(k) for k in ROOTS])
        self.send(404, {})

    def do_POST(self):
        if not self.trusted() or self.path not in ('/control', '/voices') or not secrets.compare_digest(self.headers.get('X-Control-Token', ''), TOKEN):
            return self.send(403, {'message': 'Unauthorized'})
        # Chờ tối đa 15s để xử lý tuần tự, tránh trả về 409 khi đang dọn dẹp
        if not LOCK.acquire(timeout=15): return self.send(409, {'message': 'Đang thực hiện lệnh khác, vui lòng thử lại sau giây lát.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size < 512: raise ValueError('Invalid request')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict): raise ValueError('Invalid request')
            message = change_voice(data.get('id')) if self.path == '/voices' else change(data.get('key'), data.get('action'))
            self.send(200, {'message': message})
        except (ValueError, OSError, psutil.Error) as e:
            self.send(409, {'message': str(e)})
        finally:
            LOCK.release()

def ensure_single_controller():
    current_pid = os.getpid()
    try: parent_pid = os.getppid()
    except Exception: parent_pid = -1

    try:
        with urllib.request.urlopen('http://127.0.0.1:8090/status', timeout=1) as resp:
            if resp.status == 200:
                return 'already_running'
    except Exception:
        pass

    killed = False
    try:
        for conn in psutil.net_connections(kind='inet'):
            if conn.laddr and conn.laddr.port == 8090 and conn.pid:
                if conn.pid not in (current_pid, parent_pid):
                    try:
                        proc = psutil.Process(conn.pid)
                        cmdline = " ".join(proc.cmdline() or []).lower()
                        if "tool_control.py" in cmdline:
                            proc.kill()
                            killed = True
                    except (psutil.Error, OSError): pass
    except (psutil.Error, OSError): pass
    if killed:
        time.sleep(1)
    return 'proceed'

class SingleInstanceServer(ThreadingHTTPServer):
    allow_reuse_address = True

if __name__ == '__main__':
    log_path = Path(r"C:\tool v1\workspace\service_logs\tool_control.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_check = ensure_single_controller()
    if status_check == 'already_running':
        with open(log_path, "a", encoding="utf-8") as lf:
            lf.write(f"PID {os.getpid()}: Controller already running and healthy on 8090. Exiting cleanly.\n")
        raise SystemExit(0)

    server = None
    for attempt in range(5):
        try:
            server = SingleInstanceServer(('127.0.0.1', 8090), Handler)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"Listening on 8090 PID {os.getpid()}\n")
            break
        except OSError as e:
            if attempt < 4:
                time.sleep(1)
            else:
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(f"Failed to bind 8090: {e}\n")
                raise SystemExit(0)
    server.serve_forever()
