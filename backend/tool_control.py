"""Local control plane for the two fixed AutoDub installations."""
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
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

CONTROL_DIRS = [
    Path(r"C:\tool v1\workspace\control"),
    Path(r"C:\tool v2\workspace\control"),
]
for _cd in CONTROL_DIRS:
    _cd.mkdir(parents=True, exist_ok=True)

def get_control_paths(key, filename):
    own = ROOTS[key].parent / "workspace" / "control" / filename
    paths = [own]
    for d in CONTROL_DIRS:
        p = d / filename
        if p not in paths:
            paths.append(p)
    return paths

def processes(key):
    root = ROOTS[key]
    root_str = str(root.parent).lower()
    scripts = {
        str(root / name).lower()
        for name in (
            "background_service.py",
            "main.py",
            "dashboard_monitor.py",
            "telegram_bot.py",
            "batch_processor.py",
            "gpu_worker.py",
            "model_runner.py",
            "model_runtime_runner.py",
        )
    }
    found = []
    for p in psutil.process_iter(["name"]):
        try:
            name = (p.info["name"] or "").lower()
            if not (name.startswith("python") or name.startswith("ffmpeg") or name.startswith("ffprobe")):
                continue
            cmd = [str(c).lower() for c in (p.cmdline() or [])]
            cmd_str = " ".join(cmd)
            if "tool_control.py" in cmd_str:
                continue

            is_match = False
            if root_str in cmd_str or (key == "v1" and r"c:\tool v1" in cmd_str) or (key == "v2" and r"c:\tool v2" in cmd_str):
                is_match = True
            if not is_match:
                for arg in cmd[1:]:
                    candidate = Path(arg)
                    if not candidate.is_absolute():
                        try:
                            candidate = Path(p.cwd()) / candidate
                        except (psutil.Error, OSError):
                            pass
                    if str(candidate).lower() in scripts:
                        is_match = True
                        break
            if is_match:
                script_name = "worker"
                for s in ("main.py", "dashboard_monitor.py", "telegram_bot.py", "background_service.py", "batch_processor.py", "gpu_worker.py", "model_runner.py", "model_runtime_runner.py"):
                    if s in cmd_str:
                        script_name = s
                        break
                found.append((p, script_name))
        except (psutil.Error, OSError):
            continue
    return found

def read_telemetry(key):
    best = {}
    best_at = -1
    for p in get_control_paths(key, key + '.json'):
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding='utf-8'))
                at = float(data.get('at') or 0)
                if at > best_at:
                    best_at = at
                    best = data
            except (OSError, ValueError):
                pass
    return best

def status(key):
    paused = any(p.exists() for p in get_control_paths(key, key + '.pause'))
    procs = processes(key)
    bots = [p.pid for p, name in procs if name == 'telegram_bot.py']
    data = read_telemetry(key)
    fresh = data.get('pid') in bots and time.time() - data.get('at', 0) < 12
    dashboard = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:%s/api/status' % PORTS[key], timeout=2.0) as response:
            dashboard = response.status == 200
    except Exception:
        pass

    if paused:
        state = 'stopped'
    elif fresh and data.get('polling') and dashboard:
        state = 'running'
    elif not bots and not dashboard:
        state = 'stopped'
    else:
        state = 'partial'

    return dict(key=key, state=state, dashboard=dashboard, bot=bool(bots),
                heartbeat=bool(fresh), polling=bool(fresh and data.get('polling')),
                busy=bool(data.get('busy')) if fresh else None,
                queue=data.get('queue', 0) if fresh else None,
                paused=paused)

def launch(key, service):
    root = ROOTS[key]
    pythonw = root / 'venv' / 'Scripts' / 'pythonw.exe'
    if not pythonw.exists():
        pythonw = root / 'venv' / 'Scripts' / 'python.exe'
    subprocess.Popen([str(pythonw), str(root / 'background_service.py'), '--service', service],
                     cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.CREATE_NO_WINDOW)

def _get_dashboard_token(key='v1'):
    ws = os.getenv("AUTODUB_WORKSPACE")
    if ws and key == 'v1':
        token_file = Path(ws) / ".dashboard_control_token"
        try:
            if token_file.is_file():
                return token_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    root = ROOTS.get(key)
    if not root:
        return ""
    token_file = root.parent / "workspace" / ".dashboard_control_token"
    try:
        if token_file.is_file():
            return token_file.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return ""

def stop_and_release(key):
    """
    Dừng triệt để toàn bộ bot, dashboard, batch processor, worker và giải phóng toàn bộ RAM & VRAM.
    Được gọi khi người dùng chủ động tắt tool hoặc khi chuyển đổi giữa các tool.
    """
    # 1. Đặt cờ pause ngay lập tức trên tất cả thư mục điều khiển
    for p in get_control_paths(key, key + '.pause'):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('paused', encoding='utf-8')
        except Exception:
            pass

    root = ROOTS[key]
    root_str = str(root.parent).lower()

    # 2. Dừng batch đang xử lý (nếu có)
    if key == 'v1':
        try:
            with urllib.request.urlopen('http://127.0.0.1:8088/api/status', timeout=1.0) as r:
                if json.load(r).get('active'):
                    token = _get_dashboard_token('v1')
                    headers = {'Content-Type': 'application/json', 'X-Dashboard-Input': '1'}
                    if token:
                        headers['X-Local-Control-Token'] = token
                    req = urllib.request.Request('http://127.0.0.1:8088/api/stop-batch', data=b'{}', headers=headers)
                    urllib.request.urlopen(req, timeout=1.5)
        except Exception:
            pass
    elif key == 'v2':
        try:
            req = urllib.request.Request('http://127.0.0.1:8089/api/stop-batch', data=b'{}', headers={'Content-Type': 'application/json'})
            urllib.request.urlopen(req, timeout=1.5)
        except Exception:
            pass

    # 3. Thu thập TOÀN BỘ tiến trình thuộc về Tool (kể cả supervisor, worker, render...)
    targets = {}
    for p in psutil.process_iter(['name']):
        try:
            name = (p.info['name'] or '').lower()
            if not (name.startswith('python') or name.startswith('ffmpeg') or name.startswith('ffprobe')):
                continue
            cmd = [str(c).lower() for c in (p.cmdline() or [])]
            cmd_str = ' '.join(cmd)

            # Bảo vệ bộ điều khiển trung tâm 8090 và các test runner
            if any(k in cmd_str for k in ('tool_control.py', 'test_', 'unittest', 'pytest')):
                continue

            is_tool_proc = False
            if root_str in cmd_str or (key == 'v1' and r'c:\tool v1' in cmd_str) or (key == 'v2' and r'c:\tool v2' in cmd_str):
                is_tool_proc = True

            if is_tool_proc:
                targets[p.pid] = p
                try:
                    for child in p.children(recursive=True):
                        ch_cmd = ' '.join([str(c).lower() for c in (child.cmdline() or [])])
                        if not any(k in ch_cmd for k in ('tool_control.py', 'test_', 'unittest', 'pytest')):
                            targets[child.pid] = child

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 4. Gửi tín hiệu terminate tới các tiến trình
    for pid, p in list(targets.items()):
        try:
            p.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Chờ tiến trình thoát
    gone, alive = psutil.wait_procs(list(targets.values()), timeout=2.0)

    # 5. Cưỡng chế kill dứt điểm các tiến trình còn lại (kể cả worker GPU/CUDA)
    if alive:
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                os.system(f"taskkill /F /T /PID {p.pid} >nul 2>&1")
            except Exception:
                pass
        psutil.wait_procs(alive, timeout=1.0)

    # 6. Ghi đè telemetry thành đã dừng
    telemetry = {
        'pid': None,
        'at': time.time(),
        'busy': False,
        'queue': 0,
        'paused': True,
        'polling': False
    }
    for p in get_control_paths(key, key + '.json'):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(telemetry), encoding='utf-8')
        except Exception:
            pass

    # 7. Xóa các file tạm
    for d in CONTROL_DIRS:
        for tmp in d.glob(key + '.*.tmp'):
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    return True

def stop_all_tools():
    """Tắt cả hai tool và giải phóng 100% RAM & GPU VRAM."""
    stop_and_release('v1')
    stop_and_release('v2')
    return 'Đã tắt cả hai Tool và giải phóng toàn bộ RAM & VRAM.'

def change(key, action):
    if action == 'off_all':
        return stop_all_tools()

    if key not in ROOTS or action not in ('on', 'off'):
        raise ValueError('Yêu cầu không hợp lệ.')

    if action == 'off':
        stop_and_release(key)
        return f'Đã tắt Tool {key.upper()} và giải phóng toàn bộ RAM & VRAM.'

    if action == 'on':
        other = 'v2' if key == 'v1' else 'v1'
        # RÀNG BUỘC ĐỘC QUYỀN: Bắt buộc tắt và giải phóng 100% tool đối diện trước
        stop_and_release(other)
        time.sleep(0.5)

        # Xóa cờ pause của tool được kích hoạt
        for p in get_control_paths(key, key + '.pause'):
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

        procs_found = processes(key)
        names = [name for _, name in procs_found]

        # Đảm bảo dashboard đang hoạt động
        if not any(n in names for n in ('main.py', 'dashboard_monitor.py')):
            launch(key, 'dashboard')

        # Đảm bảo telegram bot đang hoạt động
        data = read_telemetry(key)
        bots = [p for p, name in procs_found if name == 'telegram_bot.py']
        fresh = any(data.get('pid') == p.pid for p in bots) and (time.time() - data.get('at', 0) < 8)
        if not fresh and bots:
            for b in bots:
                try: b.kill()
                except psutil.NoSuchProcess: pass
        if 'telegram_bot.py' not in names or not fresh:
            launch(key, 'telegram')

        # Chờ bot và dashboard sẵn sàng (tối đa 6 giây)
        for _ in range(12):
            time.sleep(0.5)
            check_procs = processes(key)
            check_bots = [p.pid for p, name in check_procs if name == 'telegram_bot.py']
            check_data = read_telemetry(key)
            if check_bots and (check_data.get('pid') in check_bots) and check_data.get('polling'):
                break

        return f'Đã bật Tool {key.upper()} thành công (Đã tắt Tool {other.upper()} & giải phóng RAM/VRAM).'

def apply_boot_defaults():
    """
    Thiết lập trạng thái mặc định khi mở máy tính:
    - Tool V1: BẬT
    - Tool V2: TẮT (ghi cờ v2.pause, giải phóng RAM/VRAM)
    """
    try:
        # 1. Khóa và dọn Tool V2
        stop_and_release('v2')
        # 2. Mở khóa Tool V1
        for p in get_control_paths('v1', 'v1.pause'):
            try: p.unlink(missing_ok=True)
            except Exception: pass
        # 3. Khởi chạy dịch vụ Tool V1
        procs = processes('v1')
        names = [name for _, name in procs]
        if not any(n in names for n in ('main.py', 'dashboard_monitor.py')):
            launch('v1', 'dashboard')
        data = read_telemetry('v1')
        bots = [p for p, name in procs if name == 'telegram_bot.py']
        fresh = any(data.get('pid') == p.pid for p in bots) and (time.time() - data.get('at', 0) < 8)
        if 'telegram_bot.py' not in names or not fresh:
            launch('v1', 'telegram')
    except Exception:
        pass

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

HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Điều khiển Tool V1 / V2</title>
<style>
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 10px 14px;
    background-color: #0b111a;
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 12px;
    line-height: 1.4;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
    flex-wrap: wrap;
    gap: 6px;
  }
  .title-group {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  h2 {
    font-size: 13.5px;
    font-weight: 700;
    color: #ffffff;
    margin: 0;
    letter-spacing: -0.2px;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .badge-rule {
    font-size: 11px;
    color: #94a3b8;
    background: #1e293b;
    border: 1px solid #334155;
    padding: 2px 7px;
    border-radius: 4px;
  }
  .tools {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 10px;
    margin-bottom: 8px;
  }
  .tool {
    background-color: #141d2b;
    border: 1px solid #233144;
    border-radius: 8px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    transition: border-color 0.2s, box-shadow 0.2s;
  }
  .tool.active {
    border-color: #10b981;
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.12);
  }
  .tool.inactive {
    opacity: 0.85;
  }
  .tool-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .tool-title {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13px;
    font-weight: 700;
    color: #f8fafc;
  }
  .status-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 7px;
    border-radius: 12px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .status-badge.running {
    background-color: rgba(16, 185, 129, 0.15);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.3);
  }
  .status-badge.stopped {
    background-color: rgba(100, 116, 139, 0.15);
    color: #94a3b8;
    border: 1px solid rgba(100, 116, 139, 0.3);
  }
  .status-badge.pending {
    background-color: rgba(245, 158, 11, 0.15);
    color: #fbbf24;
    border: 1px solid rgba(245, 158, 11, 0.3);
  }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    display: inline-block;
  }
  .dot.running { background-color: #10b981; box-shadow: 0 0 5px #10b981; }
  .dot.stopped { background-color: #64748b; }
  .dot.pending { background-color: #f59e0b; box-shadow: 0 0 5px #f59e0b; }
  .tool-metrics {
    font-size: 11px;
    color: #94a3b8;
    line-height: 1.3;
  }
  .tool-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    margin-top: 2px;
  }
  button, a {
    font-size: 11.5px;
    border-radius: 6px;
    border: 1px solid #334155;
    background-color: #1e293b;
    color: #f1f5f9;
    padding: 4px 9px;
    text-decoration: none;
    cursor: pointer;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  button:hover:not(:disabled), a:hover {
    background-color: #273549;
    border-color: #3b82f6;
  }
  button:disabled, button[aria-disabled="true"] {
    opacity: 0.5;
    cursor: not-allowed;
    pointer-events: none;
  }
  .switch-btn {
    border: 0;
    background: transparent;
    padding: 0;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    user-select: none;
  }
  .switch-track {
    width: 44px;
    height: 24px;
    background-color: #334155;
    border-radius: 12px;
    border: 1px solid #475569;
    position: relative;
    transition: background-color 0.25s, border-color 0.25s;
    flex-shrink: 0;
  }
  .switch-thumb {
    width: 18px;
    height: 18px;
    background-color: #ffffff;
    border-radius: 50%;
    position: absolute;
    top: 2px;
    left: 2px;
    transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: 0 1px 3px rgba(0,0,0,0.4);
  }
  .switch-btn.is-active .switch-track {
    background-color: #059669;
    border-color: #10b981;
  }
  .switch-btn.is-active .switch-thumb {
    transform: translateX(20px);
  }
  .switch-btn.is-pending .switch-track {
    background-color: #b45309;
    border-color: #f59e0b;
  }
  .switch-label-text {
    font-size: 11.5px;
    font-weight: 700;
    color: #e2e8f0;
    min-width: 24px;
  }
  .switch-label-text.on { color: #34d399; }
  .switch-label-text.off { color: #94a3b8; }
  .switch-label-text.pending { color: #fbbf24; font-style: italic; }

  .actions-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    flex-wrap: wrap;
  }
  .btn-stop-all {
    background-color: #241419;
    border-color: #7f1d1d;
    color: #fca5a5;
    font-weight: 600;
    padding: 5px 12px;
    border-radius: 6px;
    font-size: 11.5px;
  }
  .btn-stop-all:hover:not(:disabled) {
    background-color: #3b141e;
    border-color: #ef4444;
    color: #fee2e2;
  }
  #message {
    padding: 5px 10px;
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 6px;
    color: #93c5fd;
    font-size: 11.5px;
    line-height: 1.4;
    margin-bottom: 8px;
  }
  #message.error {
    color: #fca5a5;
    border-color: #7f1d1d;
  }
  #message.success {
    color: #6ee7b7;
    border-color: #065f46;
  }
  .voice-section {
    border-top: 1px solid #1e293b;
    padding-top: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }
  .voice-section label {
    font-size: 11.5px;
    font-weight: 600;
    color: #cbd5e1;
    white-space: nowrap;
  }
  .voice-controls {
    display: flex;
    gap: 6px;
    align-items: center;
    flex-grow: 1;
    justify-content: flex-end;
  }
  select {
    background-color: #0f172a;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 11.5px;
    outline: none;
    max-width: 250px;
  }
  select:focus { border-color: #3b82f6; }
  #save-voice {
    background-color: #2563eb;
    border-color: #3b82f6;
    color: #ffffff;
    padding: 4px 10px;
  }
  #save-voice:hover:not(:disabled) { background-color: #1d4ed8; }
</style>
</head>
<body>
<div class="header">
  <div class="title-group">
    <h2>⚡ Bộ điều khiển Tool V1 & V2</h2>
    <span class="badge-rule">Chế độ độc quyền: Bật 1 Tool duy nhất hoặc Tắt cả 2</span>
  </div>
</div>

<div class="tools" id="tools"></div>

<div class="actions-bar">
  <button id="btn-off-all" class="btn-stop-all" onclick="stopAll()" title="Dừng triệt để cả 2 tool và giải phóng toàn bộ RAM & VRAM">
    ⏹ Tắt cả 2 Tool (Giải phóng 100% RAM & VRAM)
  </button>
  <div id="message" role="status" style="margin-bottom:0; flex-grow:1;">Đang tải trạng thái hệ thống…</div>
</div>

<section class="voice-section">
  <label for="voice">Giọng lồng tiếng (Dùng chung):</label>
  <div class="voice-controls">
    <select id="voice" onchange="saveVoice()" disabled></select>
    <button id="save-voice" onclick="saveVoice()" disabled>Lưu</button>
    <small id="voice-status" style="color:#94a3b8; font-size:11px; margin-left:4px;"></small>
  </div>
</section>

<script>
const token = '__TOKEN__';
let pending = false;
let pendingTarget = null;
let states = [];

function renderTools() {
  const container = document.getElementById('tools');
  const btnOffAll = document.getElementById('btn-off-all');
  if (btnOffAll) btnOffAll.disabled = pending;

  if (!container || !states.length) return;

  container.innerHTML = states.map(s => {
    const isThisPending = pending && (pendingTarget === s.key || pendingTarget === 'all');
    const isRunning = (s.state === 'running');
    const isPartial = (s.state === 'partial');
    const isActive = isRunning || isPartial || s.bot || (!s.paused);

    let badgeClass = 'stopped';
    let badgeText = 'Đã tắt';
    if (isThisPending) {
      badgeClass = 'pending';
      badgeText = 'Đang xử lý…';
    } else if (isRunning) {
      badgeClass = 'running';
      badgeText = 'Đang bật';
    } else if (isPartial) {
      badgeClass = 'pending';
      badgeText = 'Đang khởi động';
    }

    const botStatus = s.polling ? 'Bot: Polling ✓' : (s.bot ? 'Bot: Khởi động' : 'Bot: Tắt');
    const queueStatus = s.busy ? ' (Đang xử lý)' : (s.queue ? ` (${s.queue} video chờ)` : '');
    const dashStatus = s.dashboard ? 'Dashboard: Online' : 'Dashboard: Offline';
    const switchClass = isThisPending ? 'is-pending' : (isActive ? 'is-active' : '');
    const switchLabel = isThisPending ? 'Chờ…' : (isActive ? 'BẬT' : 'TẮT');
    const switchLabelClass = isThisPending ? 'pending' : (isActive ? 'on' : 'off');
    const targetUrl = `http://127.0.0.1:${s.key === 'v1' ? 8088 : 8089}/`;

    return `
      <div class="tool ${isActive ? 'active' : 'inactive'}">
        <div class="tool-top">
          <div class="tool-title">
            <span class="dot ${isThisPending ? 'pending' : (isActive ? 'running' : 'stopped')}"></span>
            Tool ${s.key.toUpperCase()}
          </div>
          <span class="status-badge ${badgeClass}">
            ${badgeText}
          </span>
        </div>
        <div class="tool-metrics">
          <span>${dashStatus}</span> · <span>${botStatus}${queueStatus}</span>
        </div>
        <div class="tool-actions">
          <button class="switch-btn ${switchClass}"
            role="switch"
            aria-checked="${isActive}"
            aria-label="Bật hoặc tắt Tool ${s.key.toUpperCase()}"
            title="${isActive ? 'Bấm để TẮT Tool ' + s.key.toUpperCase() + ' và giải phóng tài nguyên' : 'Bấm để BẬT Tool ' + s.key.toUpperCase() + ' (Sẽ tự động tắt tool còn lại)'}"
            ${pending ? 'disabled' : ''}
            onclick="toggleTool('${s.key}', ${isActive})">
            <span class="switch-track" aria-hidden="true">
              <span class="switch-thumb"></span>
            </span>
            <span class="switch-label-text ${switchLabelClass}">${switchLabel}</span>
          </button>
          <a href="${targetUrl}" target="_top" title="Mở trang quản lý Tool ${s.key.toUpperCase()}">Mở Dashboard ↗</a>
        </div>
      </div>
    `;
  }).join('');
}

async function refresh() {
  try {
    const r = await fetch('/status', { signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw Error();
    states = await r.json();
    if (!pending) renderTools();
  } catch(e) {
    if (!pending) {
      const msg = document.getElementById('message');
      if (msg) msg.textContent = 'Đang kết nối lại bộ điều khiển (Port 8090)...';
    }
  }
}

async function toggleTool(key, currentlyActive) {
  if (pending) return;
  const action = currentlyActive ? 'off' : 'on';
  const other = key === 'v1' ? 'v2' : 'v1';

  pending = true;
  pendingTarget = key;
  renderTools();

  const msgElem = document.getElementById('message');
  msgElem.className = '';
  if (action === 'off') {
    msgElem.textContent = `⏳ Đang tắt Tool ${key.toUpperCase()} và giải phóng toàn bộ RAM & VRAM...`;
  } else {
    msgElem.textContent = `⏳ Đang dọn sạch Tool ${other.toUpperCase()} & khởi động Tool ${key.toUpperCase()}...`;
  }

  try {
    const r = await fetch('/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-Token': token },
      body: JSON.stringify({ key, action }),
      signal: AbortSignal.timeout(20000)
    });
    const d = await r.json();
    if (r.ok) {
      msgElem.className = 'success';
      msgElem.textContent = '✅ ' + (d.message || 'Thao tác hoàn tất.');
    } else {
      msgElem.className = 'error';
      msgElem.textContent = '⚠️ ' + (d.message || 'Có lỗi xảy ra.');
    }
  } catch(e) {
    msgElem.className = 'error';
    msgElem.textContent = e.name === 'TimeoutError' ? 'Lệnh đang được hoàn tất ngầm...' : ('Lỗi kết nối: ' + e.message);
  } finally {
    pending = false;
    pendingTarget = null;
    await refresh();
    renderTools();
  }
}

async function stopAll() {
  if (pending) return;
  pending = true;
  pendingTarget = 'all';
  renderTools();

  const msgElem = document.getElementById('message');
  msgElem.className = '';
  msgElem.textContent = '⏳ Đang tắt cả 2 Tool và giải phóng 100% RAM & GPU VRAM...';

  try {
    const r = await fetch('/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Control-Token': token },
      body: JSON.stringify({ action: 'off_all' }),
      signal: AbortSignal.timeout(20000)
    });
    const d = await r.json();
    if (r.ok) {
      msgElem.className = 'success';
      msgElem.textContent = '✅ ' + (d.message || 'Đã tắt cả hai Tool.');
    } else {
      msgElem.className = 'error';
      msgElem.textContent = '⚠️ ' + (d.message || 'Có lỗi xảy ra.');
    }
  } catch(e) {
    msgElem.className = 'error';
    msgElem.textContent = 'Lỗi: ' + e.message;
  } finally {
    pending = false;
    pendingTarget = null;
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
    document.getElementById('voice-status').textContent = 'Đang dùng: ' + d.selected.label;
  } catch(e) {
    document.getElementById('voice-status').textContent = 'Không tải được giọng.';
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
    document.getElementById('voice-status').textContent = 'Đã lưu: ' + d.message;
  } catch(e) {
    await loadVoices();
    alert('Không đổi được giọng: ' + e.message);
  } finally {
    button.disabled = false;
    select.disabled = false;
  }
}

loadVoices();
refresh();
setInterval(() => { if (!pending) refresh(); }, 3000);
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
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
            _s.settimeout(0.5)
            if _s.connect_ex(('127.0.0.1', 8090)) == 0:
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8090/status', timeout=2) as resp:
                        if resp.status == 200:
                            return 'already_running'
                except Exception:
                    time.sleep(1)
                    try:
                        with urllib.request.urlopen('http://127.0.0.1:8090/status', timeout=2) as resp:
                            if resp.status == 200:
                                return 'already_running'
                    except Exception:
                        pass
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
    allow_reuse_address = False


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

    # Áp dụng mặc định khi khởi động máy: Tool V1 BẬT, Tool V2 TẮT
    threading.Thread(target=apply_boot_defaults, daemon=True).start()
    server.serve_forever()


