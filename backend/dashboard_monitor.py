"""Lightweight read-only V2 dashboard; never imports or starts AI workers."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import time
from fastapi import FastAPI, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, FileResponse
from environment import read_environment

ROOT = Path(__file__).resolve().parent
ENV = read_environment(ROOT)
WORKSPACE = Path(ENV.get("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
INPUT = Path(ENV.get("AUTODUB_INPUT_DIR", r"D:\video phôi"))
_env_output = Path(ENV.get("AUTODUB_OUTPUT_DIR", r"D:\video tool v2")).resolve()
if not _env_output.is_dir():
    _env_output.mkdir(parents=True, exist_ok=True)
OUTPUT = _env_output
TEMPLATE = ROOT / "templates" / "dashboard.html"
STAGES = [
 ("input", "Tiếp nhận video"), ("extract_audio", "Tách âm thanh"),
 ("demucs", "BS-RoFormer / Demucs dự phòng"),
 ("transcribe", "Qwen3-ASR · căn timestamp"), ("ocr", "PP-OCRv6"),
 ("translate", "Dịch phụ đề theo cấu hình V2"), ("timing", "Căn thời gian lời đọc"),
 ("tts", "Tổng hợp giọng TTS"), ("rvc", "Chuyển giọng RVC"),
 ("subtitles", "Tạo phụ đề"), ("mix_legacy", "Trộn âm legacy"),
 ("mix_v2", "Trộn âm V2"), ("render", "Render video"),
 ("qc", "Kiểm tra chất lượng QC"), ("deliver", "Xuất thành phẩm")]
app = FastAPI()
MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

def is_v2_paused():
    pause_paths = [
        WORKSPACE / "control" / "v2.pause",
        Path(r"C:\tool v1\workspace\control\v2.pause"),
        Path(r"C:\tool v2\workspace\control\v2.pause"),
    ]
    return any(p.is_file() for p in pause_paths)

def is_v2_bot_running():
    if is_v2_paused():
        return False
    telem_paths = [
        WORKSPACE / "control" / "v2.json",
        Path(r"C:\tool v2\workspace\control\v2.json"),
        Path(r"C:\tool v1\workspace\control\v2.json"),
    ]
    now = time.time()
    for p in telem_paths:
        if p.is_file():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                pid = int(d.get("pid") or 0)
                if pid and (now - float(d.get("at", 0)) < 15):
                    return True
            except Exception:
                pass
    try:
        import psutil
        for p in psutil.process_iter(['name', 'cmdline']):
            cmd = " ".join(p.info.get('cmdline') or [])
            if 'telegram_bot.py' in cmd and ('tool v2' in cmd.lower() or 'tool_v2' in cmd.lower()):
                return True
    except Exception:
        pass
    return False

def read_status():
    candidates = sorted(WORKSPACE.glob("*/pipeline_v2/job_manifest.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    paused = is_v2_paused()
    bot_alive = is_v2_bot_running()
    
    # Query pending jobs from SQLite queue
    db_path = WORKSPACE / "queue_v2.sqlite3"
    q_count = 0
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1)
            row = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()
            q_count = row[0] if row else 0
            conn.close()
        except Exception:
            pass

    result = {"stages": [], "percent": 0, "video_name": "", "elapsed_seconds": 0,
              "message": "Chưa có manifest V2.",
              "status": "stopped" if paused else "idle",
              "is_paused": paused,
              "queue_count": q_count}
    if paused:
        result["message"] = "⏸ Tool V2 đang TẮT. Toàn bộ tiến trình bot và bộ nhớ VRAM đã được giải phóng."

    if not candidates:
        return result

    # Report the latest persisted job, not a fabricated global queue.
    path = candidates[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("stages", {})
    now = datetime.now(timezone.utc)

    stage_list = []
    for key, label in STAGES:
        st_data = records.get(key, {})
        st_status = st_data.get("status", "pending")
        if paused and st_status == "running":
            st_status = "stopped"

        dur_sec = None
        st_started = st_data.get("started_at")
        st_finished = st_data.get("finished_at")
        if st_started and st_finished:
            try:
                t0 = datetime.fromisoformat(st_started.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(st_finished.replace("Z", "+00:00"))
                dur_sec = max(0.0, round((t1 - t0).total_seconds(), 1))
            except Exception:
                pass
        elif st_started and st_status == "running":
            try:
                t0 = datetime.fromisoformat(st_started.replace("Z", "+00:00"))
                dur_sec = max(0.0, round((now - t0).total_seconds(), 1))
            except Exception:
                pass

        stage_list.append({
            "key": key,
            "label": label,
            "status": st_status,
            "duration_seconds": dur_sec,
            "duration_formatted": f"{dur_sec:.1f}s" if dur_sec is not None else "--"
        })

    result["stages"] = stage_list
    result["video_name"] = Path(data.get("metadata", {}).get("source_path", path.parent.parent.name)).name
    result["updated_at"] = data.get("updated_at", "")
    finished = sum(s["status"] in ("completed", "skipped") for s in result["stages"])
    delivered = records.get("deliver", {}).get("status") == "completed"
    failed = [s["label"] for s in result["stages"] if s["status"] == "failed"]
    running = [s["label"] for s in result["stages"] if s["status"] == "running"]

    if paused:
        result["status"] = "stopped"
        result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
        result["message"] = ("Đã xuất thành phẩm trước đó. Tool V2 hiện đang TẮT (VRAM đã giải phóng)." if delivered else
                             "Tool V2 hiện đang TẮT. Toàn bộ tiến trình bot và bộ nhớ VRAM đã được giải phóng.")
    else:
        result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
        result["status"] = "completed" if delivered else "error" if failed else "recorded"
        result["message"] = ("Đã xuất thành phẩm." if delivered else
                             "Lỗi tại: " + ", ".join(failed) if failed else
                             "Bước ghi nhận: " + ", ".join(running) if running else "Chưa hoàn tất.")
        if not delivered and not failed:
            result["message"] += " Trạng thái lưu trên đĩa; chưa xác minh tiến trình còn chạy."

    try:
        started = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        if delivered or failed or paused or not bot_alive:
            ended = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
            result["elapsed_seconds"] = max(0, int((ended - started).total_seconds()))
        else:
            result["elapsed_seconds"] = max(0, int((now - started).total_seconds()))
    except (ValueError, KeyError, TypeError):
        pass
    return result

@app.get("/api/status")
async def status():
    try:
        return await asyncio.to_thread(read_status)
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "Không đọc được manifest V2; sẽ thử lại.")

def listing(root):
    files = []
    try:
        from render_history import get_all_render_durations, format_duration
        durations = get_all_render_durations(root)
    except Exception:
        durations = {}
        format_duration = lambda s: "--"

    for p in root.iterdir() if root.is_dir() else []:
        if p.is_file() and p.suffix.lower() in MEDIA:
            stat = p.stat()
            dur_sec = (
                durations.get(p.name)
                or durations.get(p.name.replace("Dubbed_", ""))
                or durations.get(f"Dubbed_{p.name}")
                or 0
            )
            files.append({"name": p.name, "size_mb": round(stat.st_size / 1048576, 2),
                          "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                          "duration_seconds": dur_sec,
                          "duration_formatted": format_duration(dur_sec) if dur_sec else "--",
                          "status": "waiting", "status_label": "Chưa xác minh"})
    files.sort(key=lambda item: item["created"], reverse=True)
    return {"files": files, "total_count": len(files),
            "total_size_mb": round(sum(f["size_mb"] for f in files), 2)}

@app.get("/api/phoi")
async def inputs():
    return await asyncio.to_thread(listing, INPUT)

@app.get("/api/banve")
async def outputs():
    return await asyncio.to_thread(listing, OUTPUT)

def log_tail():
    paths = [ROOT / "app.log", ROOT.parent / "app.log"]
    paths = [p for p in paths if p.is_file()]
    if not paths:
        return ""
    with max(paths, key=lambda p: p.stat().st_mtime).open(encoding="utf-8", errors="replace") as f:
        content = "".join(deque(f, maxlen=500))
        try:
            from mojibake_repair import repair_vietnamese_mojibake
            return repair_vietnamese_mojibake(content)
        except Exception:
            return content

@app.get("/api/logs")
async def logs():
    return {"logs": await asyncio.to_thread(log_tail)}

def folder(key):
    if key not in {"phoi", "banve"}:
        raise HTTPException(400, "Thư mục không hợp lệ")
    return INPUT if key == "phoi" else OUTPUT

@app.post("/api/open-folder")
async def open_folder(folder: str = Form(...)):
    target = globals()["folder"](folder).resolve()
    if not target.is_dir():
        raise HTTPException(404, "Thư mục chưa tồn tại: " + str(target))
    try:
        await asyncio.to_thread(os.startfile, str(target), "open", "", None, 1)
    except OSError:
        raise HTTPException(500, "Windows không mở được thư mục.")
    return {"message": "Đã gửi yêu cầu mở thư mục"}

@app.get("/api/stream/{key}/{filename}")
async def stream(key: str, filename: str):
    root = folder(key).resolve()
    target = (root / filename).resolve()
    if target.parent != root or target.suffix.lower() not in MEDIA or not target.is_file():
        raise HTTPException(404, "Video không tồn tại")
    return FileResponse(target)

from audio_settings import get_audio_settings, save_audio_settings

@app.get("/api/audio-settings")
async def api_get_audio_settings():
    return get_audio_settings()

@app.post("/api/audio-settings")
async def api_save_audio_settings(payload: dict = Body(...)):
    bgm = payload.get("bgm_volume_db", -2.0)
    dub = payload.get("dubbing_volume_db", 1.0)
    return save_audio_settings(bgm, dub)

def read_queue():
    paused = is_v2_paused()
    bot_alive = is_v2_bot_running()
    alive = bot_alive and not paused
    items = []

    db_path = WORKSPACE / "queue_v2.sqlite3"
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, payload FROM jobs WHERE state = 'queued' ORDER BY id ASC"
            ).fetchall()
            for idx, r in enumerate(rows, 1):
                try:
                    payload = json.loads(r["payload"])
                except Exception:
                    payload = {}
                name = str(payload.get("filename") or payload.get("url") or "")
                if not name and payload.get("path"):
                    name = Path(payload["path"]).name
                if not name:
                    name = f"Video #{r['id']}"
                source = "Batch" if payload.get("type") == "local" else "Telegram"
                items.append({
                    "position": idx,
                    "name": name,
                    "source": source
                })
            conn.close()
            return {
                "items": items,
                "available": alive,
                "message": "" if alive else "Bot đã dừng; danh sách là bản ghi cuối cùng."
            }
        except Exception:
            pass

    for q_candidate in [WORKSPACE / "telegram_queue.json", Path(r"C:\tool v2\workspace\telegram_queue.json"), Path(r"C:\tool v1\workspace\telegram_queue.json")]:
        if q_candidate.is_file():
            try:
                data = json.loads(q_candidate.read_text(encoding="utf-8"))
                return {
                    "items": data.get("items", []),
                    "available": alive,
                    "message": "" if alive else "Bot đã dừng; danh sách là bản ghi cuối cùng."
                }
            except Exception:
                pass

    return {
        "items": [],
        "available": alive,
        "message": "" if alive else "Bot đã dừng; danh sách là bản ghi cuối cùng."
    }

@app.get("/api/queue")
async def api_get_queue():
    try:
        return await asyncio.to_thread(read_queue)
    except Exception:
        raise HTTPException(503, "Không đọc được hàng đợi V2")

@app.get("/", response_class=HTMLResponse)
def dashboard():
    page = TEMPLATE.read_text(encoding="utf-8")
    page = page.replace("TOOL V1", "TOOL V2").replace("Tool V1 Legacy", "Tool V2 · Giám sát")
    page = page.replace('href="/a2ui"', 'href="http://127.0.0.1:8088/a2ui"')
    page = re.sub(r'<button\b[^>]*onclick="(?:startBatch|stopBatch)\(\)"[^>]*>.*?</button>', '', page)
    # Replace paths separately in HTML and JavaScript so backslashes remain valid.
    head, script = page.split("<script>", 1)
    head = head.replace(r"D:\video phôi", html.escape(str(INPUT))).replace(r"D:\banve", html.escape(str(OUTPUT)) + " · thư mục dùng chung nếu cùng cấu hình")
    script = script.replace(r"D:\\video phôi", str(INPUT).replace("\\", "\\\\")).replace(r"D:\\banve", str(OUTPUT).replace("\\", "\\\\"))
    page = head + "<script>" + script
    override = r'''
    renderStatus = function(state) {
      const isPaused = state.is_paused || state.status === 'stopped';
      const statusTitle = document.getElementById('statusTitle');
      const bar = document.getElementById('progressBar');

      if (isPaused) {
        statusTitle.innerHTML = '<span class="dot dot-red" style="background-color:#ef4444;box-shadow:0 0 6px rgba(239,68,68,0.5);"></span> TOOL V2 · ĐÃ TẮT (ĐÃ GIẢI PHÓNG VRAM)';
        document.getElementById('pctText').textContent = 'Đã tắt';
        bar.style.width = '0%';
        bar.style.background = '#475569';
        bar.style.boxShadow = 'none';
        bar.setAttribute('aria-valuenow', '0');
      } else {
        const dotColor = state.status === 'completed' ? '#10b981' : state.status === 'error' ? '#ef4444' : '#10b981';
        const dotShadow = state.status === 'error' ? 'rgba(239,68,68,0.5)' : 'rgba(16,185,129,0.5)';
        const titleText = state.status === 'completed' ? 'HOÀN TẤT' : state.status === 'error' ? 'CÓ LỖI' : 'ĐANG HOẠT ĐỘNG';
        statusTitle.innerHTML = '<span class="dot" style="background-color:' + dotColor + ';box-shadow:0 0 6px ' + dotShadow + ';"></span> TOOL V2 · ' + titleText;
        document.getElementById('pctText').textContent = state.percent + '%';
        bar.style.width = state.percent + '%';
        bar.style.background = '';
        bar.style.boxShadow = '';
        bar.setAttribute('aria-valuenow', String(state.percent));
      }

      document.getElementById('currentVideoName').textContent = state.video_name || (isPaused ? 'Tool V2 hiện đang tắt' : 'Chưa có video V2');
      document.getElementById('stepDescription').textContent = state.message + (state.updated_at ? ' • Cập nhật: ' + state.updated_at : '');
      bar.setAttribute('role', 'progressbar');

      const elSec = Number(state.elapsed_seconds) || 0;
      document.getElementById('valElapsed').textContent = elSec ? `${formatTime(elSec)} (${elSec}s)` : '00:00';
      document.getElementById('valEta').textContent = '--';
      const qCount = Number(state.queue_count) || 0;
      document.getElementById('valQueue').textContent = isPaused ? 'Đã tạm dừng' : (qCount > 0 ? (qCount + ' video chờ') : '0 video chờ');

      const labels = {pending:'Chờ', running:'Đang chạy', completed:'Xong', skipped:'Bỏ qua', failed:'Lỗi', stopped:'Đã dừng'};
      const icons = ['📥','🎧','🧠','🤖','👀','🌐','⏱️','🎙️','🗣️','📝','🎚️','🎛️','🎬','🛡️','📁'];
      const grid = document.querySelector('.steps-grid');
      grid.classList.add('v2-steps');
      grid.setAttribute('role', 'list');
      grid.setAttribute('aria-label', 'Các bước xử lý Tool V2 theo thứ tự');
      grid.innerHTML = (state.stages.length ? state.stages : V2_STAGES).map((s, i) => {
        const cls = {completed:'completed', running:'active', failed:'failed', skipped:'skipped', stopped:'stopped'}[s.status] || '';
        const durText = (s.duration_seconds !== undefined && s.duration_seconds !== null)
          ? (s.status === 'completed' ? `✓ ${s.duration_seconds}s` : (s.status === 'running' ? `⏱ ${s.duration_seconds}s` : `${s.duration_seconds}s`))
          : (s.status === 'completed' ? '✓ Xong' : (s.status === 'running' ? '⏱ Đang chạy' : '--'));

        return '<div role="listitem" class="step-item ' + cls + '"' + (s.status === 'running' ? ' aria-current="step"' : '') +
          '><div class="v2-step-icon" aria-hidden="true">' + icons[i] + '</div>' +
          '<div class="v2-step-number">BƯỚC ' + String(i + 1).padStart(2, '0') + '</div>' +
          '<div class="v2-step-name">' + escapeHtml(s.label) + '</div>' +
          '<div class="v2-step-duration" title="Thời gian chạy thực tế bước ' + (i + 1) + '">' + durText + '</div>' +
          '<span class="v2-step-status">' + (labels[s.status] || 'Chờ') + '</span></div>';
      }).join('');
    };
    fetchStatus = async function() {
      if (statusRequestPending) return;
      statusRequestPending = true;
      try {
        const response = await fetch('/api/status', {cache:'no-store', signal:AbortSignal.timeout(8000)});
        if (!response.ok) throw new Error('Không đọc được trạng thái');
        renderStatus(await response.json());
      } catch (error) {
        document.getElementById('stepDescription').textContent = 'Mất kết nối trạng thái V2; dữ liệu hiển thị có thể đã cũ.';
      } finally { statusRequestPending = false; }
    };
    '''
    override = "const V2_STAGES = " + json.dumps([{"label": label, "status": "pending"} for _, label in STAGES], ensure_ascii=False) + ";\n" + override
    page = page.replace("    // Auto-refresh loops", override + "\n    // Auto-refresh loops")
    page = page.replace("</style>", """
      .steps-grid.v2-steps {grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-top:14px;}
      .v2-steps .step-item {position:relative;min-height:105px;padding:9px 8px;
        background:linear-gradient(145deg,#151f2d,#111722);border-radius:9px;gap:3px;}
      .v2-step-icon {font-size:19px;line-height:23px;}
      .v2-step-number {font-size:10px;letter-spacing:1.2px;font-weight:800;color:#94a3b8;}
      .v2-step-name {font-size:11px;line-height:1.35;font-weight:700;color:#dbeafe;
        min-height:28px;display:flex;align-items:center;justify-content:center;text-align:center;}
      .v2-step-duration {font-size:11px;line-height:1.3;font-weight:700;font-variant-numeric:tabular-nums;
        color:#94a3b8;padding:1px 6px;border-radius:4px;background:#0f172a80;margin:2px 0 3px;}
      .v2-step-status {font-size:10px;line-height:1.4;padding:2px 8px;border-radius:20px;
        background:#202c3d;color:#a9b7cc;margin-top:auto;}
      .v2-steps .step-item.active {background:linear-gradient(145deg,#172c49,#13243b);border-color:#3b82f6;
        box-shadow:0 0 0 1px #3b82f633,0 4px 18px #3b82f615;}
      .v2-steps .active .v2-step-duration {color:#38bdf8;background:#1e3a8a60;font-weight:800;}
      .v2-steps .active .v2-step-status {color:#93c5fd;background:#2563eb30;}
      .v2-steps .step-item.completed {background:#102e2d;border-color:#10b98170;}
      .v2-steps .completed .v2-step-name,.v2-steps .completed .v2-step-status {color:#34d399;}
      .v2-steps .completed .v2-step-duration {color:#34d399;background:#064e3b50;}
      .v2-steps .completed .v2-step-status {background:#10b98120;}
      .v2-steps .step-item.failed {border-color:#f87171;background:#311e29;}
      .v2-steps .failed .v2-step-status {color:#fca5a5;background:#ef444420;}
      .v2-steps .step-item.skipped {border-style:dashed;}
      .v2-steps .skipped .v2-step-status {color:#cbd5e1;background:#334155;}
      .v2-steps .step-item.stopped {border-color:#475569;background:#18202c;opacity:0.75;}
      .v2-steps .stopped .v2-step-status {color:#94a3b8;background:#33415550;}
      @media(max-width:1000px){.steps-grid.v2-steps{grid-template-columns:repeat(3,minmax(0,1fr));}}
      @media(max-width:560px){.steps-grid.v2-steps{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}
        .v2-steps .step-item{padding:8px 6px;}}
      </style>""")
    return page

from workflow_api import router as workflow_router
app.include_router(workflow_router)

try:
    from script_api import router as script_router
    app.include_router(script_router)
    print("[Studio Kich Ban] script_router mounted successfully on V2.")
except Exception as _se:
    print(f"[Studio Kich Ban] Failed to mount script_router on V2: {_se}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8089)
