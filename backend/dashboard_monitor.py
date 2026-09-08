"""Lightweight read-only V2 dashboard; never imports or starts AI workers."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from environment import read_environment

ROOT = Path(__file__).resolve().parent
ENV = read_environment(ROOT)
WORKSPACE = Path(ENV.get("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
INPUT = Path(ENV.get("AUTODUB_INPUT_DIR", r"D:\video phôi"))
OUTPUT = Path(ENV.get("AUTODUB_OUTPUT_DIR", r"D:\banve"))
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

def read_status():
    candidates = sorted(WORKSPACE.glob("*/pipeline_v2/job_manifest.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    result = {"stages": [], "percent": 0, "video_name": "", "elapsed_seconds": 0,
              "message": "Chưa có manifest V2. Dashboard không tự khởi chạy bot hoặc xử lý video.",
              "status": "idle"}
    if not candidates:
        return result
    # Report the latest persisted job, not a fabricated global queue.
    path = candidates[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("stages", {})
    result["stages"] = [{"key": key, "label": label, "status": records.get(key, {}).get("status", "pending")}
                        for key, label in STAGES]
    result["video_name"] = Path(data.get("metadata", {}).get("source_path", path.parent.parent.name)).name
    result["updated_at"] = data.get("updated_at", "")
    finished = sum(s["status"] in ("completed", "skipped") for s in result["stages"])
    delivered = records.get("deliver", {}).get("status") == "completed"
    failed = [s["label"] for s in result["stages"] if s["status"] == "failed"]
    running = [s["label"] for s in result["stages"] if s["status"] == "running"]
    result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
    result["status"] = "completed" if delivered else "error" if failed else "recorded"
    result["message"] = ("Đã xuất thành phẩm." if delivered else
                         "Lỗi tại: " + ", ".join(failed) if failed else
                         "Bước ghi nhận: " + ", ".join(running) if running else "Chưa hoàn tất.")
    if not delivered and not failed:
        result["message"] += " Trạng thái lưu trên đĩa; chưa xác minh tiến trình còn chạy."
    try:
        started = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
        result["elapsed_seconds"] = max(0, int((ended - started).total_seconds()))
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
    for p in root.iterdir() if root.is_dir() else []:
        if p.is_file() and p.suffix.lower() in MEDIA:
            stat = p.stat()
            files.append({"name": p.name, "size_mb": round(stat.st_size / 1048576, 2),
                          "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
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
        return "".join(deque(f, maxlen=100))

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
      document.getElementById('statusTitle').textContent = 'TOOL V2 · ' + (state.status === 'completed' ? 'HOÀN TẤT' : state.status === 'error' ? 'CÓ LỖI' : 'TRẠNG THÁI ĐÃ GHI NHẬN');
      document.getElementById('currentVideoName').textContent = state.video_name || 'Chưa có video V2';
      document.getElementById('stepDescription').textContent = state.message + (state.updated_at ? ' • Cập nhật: ' + state.updated_at : '');
      document.getElementById('pctText').textContent = state.percent + '%';
      const bar = document.getElementById('progressBar');
      bar.style.width = state.percent + '%';
      bar.setAttribute('role', 'progressbar'); bar.setAttribute('aria-valuenow', state.percent);
      document.getElementById('valElapsed').textContent = formatTime(state.elapsed_seconds);
      document.getElementById('valEta').textContent = '--';
      document.getElementById('valQueue').textContent = 'Không có dữ liệu hàng đợi';
      const labels = {pending:'Chờ', running:'Đang chạy (đã ghi nhận)', completed:'Xong', skipped:'Bỏ qua', failed:'Lỗi'};
      const icons = ['📥','🎧','🧠','🤖','👀','🌐','⏱️','🎙️','🗣️','📝','🎚️','🎛️','🎬','🛡️','📁'];
      const grid = document.querySelector('.steps-grid');
      grid.classList.add('v2-steps');
      grid.setAttribute('role', 'list');
      grid.setAttribute('aria-label', 'Các bước xử lý Tool V2 theo thứ tự');
      grid.innerHTML = (state.stages.length ? state.stages : V2_STAGES).map((s, i) => {
        const cls = {completed:'completed', running:'active', failed:'failed', skipped:'skipped'}[s.status] || '';
        return '<div role="listitem" class="step-item ' + cls + '"' + (s.status === 'running' ? ' aria-current="step"' : '') +
          '><div class="v2-step-icon" aria-hidden="true">' + icons[i] + '</div>' +
          '<div class="v2-step-number">BƯỚC ' + String(i + 1).padStart(2, '0') + '</div>' +
          '<div class="v2-step-name">' + escapeHtml(s.label) + '</div>' +
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
      .v2-steps .step-item {position:relative;min-height:100px;padding:9px 8px;
        background:linear-gradient(145deg,#151f2d,#111722);border-radius:9px;gap:3px;}
      .v2-step-icon {font-size:19px;line-height:23px;}
      .v2-step-number {font-size:10px;letter-spacing:1.2px;font-weight:800;color:#94a3b8;}
      .v2-step-name {font-size:11px;line-height:1.4;font-weight:700;color:#dbeafe;
        min-height:28px;display:flex;align-items:center;justify-content:center;}
      .v2-step-status {font-size:10px;line-height:1.4;padding:2px 8px;border-radius:20px;
        background:#202c3d;color:#a9b7cc;margin-top:auto;}
      .v2-steps .step-item.active {background:linear-gradient(145deg,#172c49,#13243b);border-color:#3b82f6;
        box-shadow:0 0 0 1px #3b82f633,0 4px 18px #3b82f615;}
      .v2-steps .active .v2-step-status {color:#93c5fd;background:#2563eb30;}
      .v2-steps .step-item.completed {background:#102e2d;border-color:#10b98170;}
      .v2-steps .completed .v2-step-name,.v2-steps .completed .v2-step-status {color:#34d399;}
      .v2-steps .completed .v2-step-status {background:#10b98120;}
      .v2-steps .step-item.failed {border-color:#f87171;background:#311e29;}
      .v2-steps .failed .v2-step-status {color:#fca5a5;background:#ef444420;}
      .v2-steps .step-item.skipped {border-style:dashed;}
      .v2-steps .skipped .v2-step-status {color:#cbd5e1;background:#334155;}
      @media(max-width:1000px){.steps-grid.v2-steps{grid-template-columns:repeat(3,minmax(0,1fr));}}
      @media(max-width:560px){.steps-grid.v2-steps{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;}
        .v2-steps .step-item{padding:8px 6px;}}
      </style>""")
    return page

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8089)
