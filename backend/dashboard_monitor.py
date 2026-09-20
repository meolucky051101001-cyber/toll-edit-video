"""Lightweight read-only V2 dashboard; never imports or starts AI workers."""
import asyncio
from collections import deque
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from fastapi import FastAPI, Form, HTTPException, Body
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
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
    ("input", "Tiếp nhận video"),
    ("extract_audio", "Tách âm thanh"),
    ("demucs", "BS-RoFormer / Demucs dự phòng"),
    ("transcribe", "Qwen3-ASR · căn timestamp"),
    ("ocr", "PP-OCRv6"),
    ("translate", "Dịch phụ đề theo cấu hình V2"),
    ("timing", "Căn thời gian lời đọc"),
    ("tts", "Tổng hợp giọng TTS"),
    ("rvc", "Chuyển giọng RVC"),
    ("subtitles", "Tạo phụ đề"),
    ("mix_legacy", "Trộn âm legacy"),
    ("mix_v2", "Trộn âm V2"),
    ("render", "Render video"),
    ("qc", "Kiểm tra chất lượng QC"),
    ("deliver", "Xuất thành phẩm"),
]

UI_STEPS = [
    (
        "step1",
        "Tách Âm & Giữ Nhạc Nền",
        "BS-RoFormer GPU · SDR 12.9dB",
        ["input", "extract_audio", "demucs"],
        "🎧",
        [
            "Trích xuất âm thanh gốc 24kHz PCM",
            "Tách giọng nói nhân vật (Vocal track)",
            "Bảo tồn nhạc nền nguyên bản (Beat SDR 12.9dB)",
        ],
    ),
    (
        "step2",
        "Nhận Diện & Dịch Thuật AI",
        "Whisper ASR · Gemini 3.8 ➔ 3.5 & Lite Backup",
        ["transcribe", "ocr", "translate", "timing"],
        "🤖",
        [
            "Chuyển giọng sang chữ (Whisper Large-v3)",
            "Dịch thuật ngữ cảnh (Gemini 3.8 ➔ 3.5 & Lite)",
            "Căn vị trí Sub 0.01s (Khung 9:16 / 16:9)",
        ],
    ),
    (
        "step3",
        "Lồng Tiếng & Hòa Âm Studio",
        "Neural TTS · Treble >14kHz · Ducking",
        ["tts", "rvc", "subtitles", "mix_legacy", "mix_v2"],
        "🗣️",
        [
            "Tạo giọng đọc AI truyền cảm (Neural TTS)",
            "Phục hồi dải cao Treble >14kHz",
            "Hòa âm Dynamic Ducking (Chuẩn EBU R128)",
        ],
    ),
    (
        "step4",
        "Render Video Thành Phẩm",
        "NVIDIA NVENC · RTX 4050",
        ["render", "qc", "deliver"],
        "🎬",
        [
            "Ghép phụ đề ASS & hòa âm đồng bộ",
            "Render GPU phần cứng NVENC 8000k",
            "Khớp luồng chống đơ & Xuất D:\\video tool v2",
        ],
    ),
]
app = FastAPI()
MEDIA = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}

def is_v2_paused():
    return (WORKSPACE / "control" / "v2.pause").is_file()

_last_bot_check = 0.0
_cached_bot_alive = False

def is_v2_bot_running():
    global _last_bot_check, _cached_bot_alive
    if is_v2_paused():
        return False
    telem_paths = [
        WORKSPACE / "control" / "v2.json",
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
    if now - _last_bot_check < 5.0:
        return _cached_bot_alive
    _last_bot_check = now
    _cached_bot_alive = False
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            if 'python' not in (p.info.get('name') or '').lower():
                continue
            try:
                cmd = " ".join(p.cmdline() or [])
                if 'telegram_bot.py' in cmd and ('tool v2' in cmd.lower() or 'tool_v2' in cmd.lower()):
                    _cached_bot_alive = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return _cached_bot_alive

BATCH_PROCESS = None
_last_batch_check = 0.0
_cached_batch_alive = False

def is_v2_batch_running():
    global BATCH_PROCESS, _last_batch_check, _cached_batch_alive
    if BATCH_PROCESS is not None:
        if BATCH_PROCESS.poll() is None:
            return True
        BATCH_PROCESS = None
    now = time.time()
    if now - _last_batch_check < 2.0:
        return _cached_batch_alive
    _last_batch_check = now
    _cached_batch_alive = False
    try:
        import psutil
        for p in psutil.process_iter(['name']):
            if 'python' not in (p.info.get('name') or '').lower():
                continue
            try:
                cmd = " ".join(p.cmdline() or [])
                if 'batch_processor.py' in cmd and ('tool v2' in cmd.lower() or 'tool_v2' in cmd.lower()):
                    _cached_batch_alive = True
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return _cached_batch_alive

def read_status():
    candidates = sorted(WORKSPACE.glob("*/pipeline_v2/job_manifest.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    paused = is_v2_paused()
    bot_alive = is_v2_bot_running()
    batch_alive = is_v2_batch_running()
    
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

    stage_list = [
        {"key": key, "label": label, "status": "pending"}
        for key, label in STAGES
    ]
    ui_step_list = [
        {
            "key": item[0],
            "label": item[1],
            "model": item[2],
            "icon": item[4],
            "subtasks": item[5],
            "status": "pending",
            "duration_seconds": None,
            "duration_formatted": "--",
        }
        for item in UI_STEPS
    ]

    result = {"stages": stage_list, "ui_steps": ui_step_list, "percent": 0, "video_name": "", "elapsed_seconds": 0,
              "message": "Chưa có tác vụ nào đang chạy.",
              "status": "stopped" if paused else ("running" if batch_alive else "idle"),
              "is_paused": paused,
              "batch_running": batch_alive,
              "active": batch_alive,
              "queue_count": q_count,
              "step": 0,
              "step_name": "Sẵn sàng xử lý video"}
    if paused:
        result["message"] = "⏸ Tool V2 đang TẮT. Toàn bộ tiến trình bot và bộ nhớ VRAM đã được giải phóng."
        return result

    # Kiểm tra log tiến độ xử lý batch nếu batch_processor đang chạy
    if batch_alive:
        log_paths = [
            WORKSPACE / "service_logs" / "batch_processor.log",
            ROOT / "app.log",
            ROOT.parent / "app.log",
        ]
        log_file = next((p for p in log_paths if p.is_file()), None)
        if log_file:
            try:
                with log_file.open("r", encoding="utf-8", errors="replace") as f:
                    lines = list(deque(f, maxlen=50))
                for line in reversed(lines):
                    m_v = re.search(r"🎬 \[\d+/\d+\] Đang xử lý:\s*`([^`]+)`", line) or re.search(r"\[(.*?)\] (?:🎧|🤖|🗣️|🎬|✅|❌)", line)
                    if m_v and not result["video_name"]:
                        result["video_name"] = m_v.group(1).strip()

                    if "Bước 1/4" in line:
                        result["step"] = 1
                        result["percent"] = 25
                        result["step_name"] = "Bước 1/4: Đang trích xuất & tách âm thanh (BS-RoFormer GPU)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 2/4" in line:
                        result["step"] = 2
                        result["percent"] = 50
                        result["step_name"] = "Bước 2/4: Nhận diện giọng nói & Dịch thuật AI (Whisper + Gemini)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 3/4" in line:
                        result["step"] = 3
                        result["percent"] = 75
                        result["step_name"] = "Bước 3/4: Lồng tiếng AI & Hòa âm trong trẻo..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 4/4" in line:
                        result["step"] = 4
                        result["percent"] = 90
                        result["step_name"] = "Bước 4/4: Đang Render video thành phẩm (NVENC GPU)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Hoàn thành video" in line:
                        result["step"] = 4
                        result["percent"] = 100
                        result["step_name"] = "Đã hoàn thành video"
                        result["message"] = line.strip()
                        break
            except Exception:
                pass

        cur_s = result["step"]
        pct = result["percent"]
        for idx, s in enumerate(ui_step_list, 1):
            if pct == 100:
                s["status"] = "completed"
            elif cur_s == idx:
                s["status"] = "running"
            elif cur_s > idx:
                s["status"] = "completed"
            else:
                s["status"] = "pending"
        result["ui_steps"] = ui_step_list
        return result

    if not candidates:
        return result

    path = candidates[0]
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("stages", {})
    now = datetime.now(timezone.utc)

    result["stages"] = [
        {"key": key, "label": label, "status": records.get(key, {}).get("status", "pending")}
        for key, label in STAGES
    ]

    ui_step_list = []
    current_step = 0
    for idx, (key, label, model_tag, sub_keys, icon, subtasks) in enumerate(UI_STEPS, 1):
        sub_statuses = [records.get(sk, {}).get("status", "pending") for sk in sub_keys]
        sub_durations = []
        for sk in sub_keys:
            st_data = records.get(sk, {})
            st_started = st_data.get("started_at")
            st_finished = st_data.get("finished_at")
            if st_started and st_finished:
                try:
                    t0 = datetime.fromisoformat(st_started.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(st_finished.replace("Z", "+00:00"))
                    sub_durations.append(max(0.0, (t1 - t0).total_seconds()))
                except Exception:
                    pass

        if any(s == "failed" for s in sub_statuses):
            st_status = "failed"
        elif any(s == "running" for s in sub_statuses):
            st_status = "running"
            current_step = idx
        elif all(s in ("completed", "skipped") for s in sub_statuses):
            st_status = "completed"
        else:
            st_status = "pending"

        dur_sec = round(sum(sub_durations), 1) if sub_durations else None
        ui_step_list.append({
            "key": key,
            "label": label,
            "model": model_tag,
            "icon": icon,
            "subtasks": subtasks,
            "status": st_status,
            "duration_seconds": dur_sec,
            "duration_formatted": f"{dur_sec:.1f}s" if dur_sec is not None else "--"
        })

    result["ui_steps"] = ui_step_list
    result["video_name"] = Path(data.get("metadata", {}).get("source_path", path.parent.parent.name)).name
    result["updated_at"] = data.get("updated_at", "")
    finished = sum(s["status"] in ("completed", "skipped") for s in result["stages"])
    delivered = records.get("deliver", {}).get("status") == "completed"
    vname = result["video_name"]
    has_output = (OUTPUT / f"Dubbed_{vname}").is_file() or (OUTPUT / f"Dubbed_{Path(vname).stem}.mp4").is_file()
    if not delivered and has_output and records.get("render", {}).get("status") == "completed":
        delivered = True
    failed = [s["label"] for s in result["stages"] if s["status"] == "failed"]
    running = [s["label"] for s in result["stages"] if s["status"] == "running"]
    active = batch_alive or (bot_alive and bool(running))
    result["active"] = active
    result["batch_running"] = batch_alive

    if paused:
        result["status"] = "stopped"
        result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
        result["message"] = ("Đã xuất thành phẩm trước đó. Tool V2 hiện đang TẮT (VRAM đã giải phóng)." if delivered else
                             "Tool V2 hiện đang TẮT. Toàn bộ tiến trình bot và bộ nhớ VRAM đã được giải phóng.")
    else:
        result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
        if failed:
            result["status"] = "error"
            result["message"] = "Lỗi tại: " + ", ".join(failed)
        elif delivered:
            result["status"] = "completed"
            result["message"] = "Đã xuất thành phẩm."
            result["step"] = 4
        elif active:
            result["status"] = "running"
            result["message"] = ("Đang xử lý hàng loạt video..." if batch_alive else "Đang thực thi: " + (", ".join(running) if running else "Khởi tạo..."))
            result["step"] = current_step
        else:
            result["status"] = "recorded"
            result["message"] = ("Bước ghi nhận: " + ", ".join(running) if running else "Chưa hoàn tất.") + " Trạng thái lưu trên đĩa; chưa xác minh tiến trình còn chạy."

    try:
        started = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        if delivered or failed or paused or (not bot_alive and not batch_alive):
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

    is_input_folder = (str(root.resolve()).lower() == str(INPUT.resolve()).lower())
    output_files = set()
    if is_input_folder and OUTPUT.is_dir():
        output_files = set(f.name for f in OUTPUT.iterdir() if f.is_file())

    cur_status = read_status()
    active_video = cur_status.get("video_name", "")
    is_active = cur_status.get("active", False)
    pct = cur_status.get("percent", 0)

    for p in root.iterdir() if root.is_dir() else []:
        if p.is_file() and p.suffix.lower() in MEDIA:
            if is_input_folder and p.name.startswith("Dubbed_"):
                continue
            stat = p.stat()
            dur_sec = (
                durations.get(p.name)
                or durations.get(p.name.replace("Dubbed_", ""))
                or durations.get(f"Dubbed_{p.name}")
                or 0
            )

            if is_input_folder:
                stem = p.stem
                has_dubbed = (
                    f"Dubbed_{stem}.mp4" in output_files
                    or f"Dubbed_{p.name}" in output_files
                )
                if p.name == active_video and is_active:
                    status_val = "running"
                    label = f"Đang chạy ({pct}%)"
                elif has_dubbed:
                    status_val = "completed"
                    label = "Thành công"
                else:
                    status_val = "waiting"
                    label = "Chờ xử lý"
            else:
                status_val = "completed"
                label = "Thành công"

            files.append({
                "name": p.name,
                "size_mb": round(stat.st_size / 1048576, 2),
                "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "duration_seconds": dur_sec,
                "duration_formatted": format_duration(dur_sec) if dur_sec else "--",
                "status": status_val,
                "status_label": label
            })
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

@app.post("/api/run-batch")
async def api_run_batch():
    """Kích hoạt chạy batch toàn bộ video phôi ngầm cho Tool V2."""
    global BATCH_PROCESS
    if is_v2_paused():
        return JSONResponse(
            status_code=409,
            content={"status": "paused", "message": "Tool V2 đang TẮT. Vui lòng bật lại ở Bảng Điều Khiển (Port 8090) trước khi chạy."}
        )
    if is_v2_batch_running():
        return JSONResponse(
            status_code=409,
            content={"status": "busy", "message": "Tiến trình xử lý video đang chạy, vui lòng đợi!"}
        )

    # Tự động di chuyển các video đã có thành phẩm sang thư mục processed để không xử lý lại
    processed_dir = INPUT / "processed"
    processed_dir.mkdir(parents=True, exist_ok=True)
    output_files = set(f.name for f in OUTPUT.iterdir() if f.is_file()) if OUTPUT.is_dir() else set()
    for vf in [f for f in INPUT.glob("*") if f.is_file() and f.suffix.lower() in MEDIA and not f.name.startswith("Dubbed_")]:
        stem = vf.stem
        if f"Dubbed_{stem}.mp4" in output_files or f"Dubbed_{vf.name}" in output_files:
            try:
                dest = processed_dir / vf.name
                if dest.exists():
                    dest = processed_dir / f"{stem}_{int(time.time())}{vf.suffix}"
                import shutil
                shutil.move(str(vf), str(dest))
            except Exception:
                pass

    # Kiểm tra thư mục đầu vào có file video cần xử lý không
    video_files = [
        f for f in INPUT.glob("*")
        if f.suffix.lower() in MEDIA and not f.name.startswith("Dubbed_") and f.is_file()
    ] if INPUT.is_dir() else []

    if not video_files:
        return JSONResponse(
            status_code=200,
            content={"status": "completed", "message": f"Toàn bộ video trong thư mục {INPUT} đều đã được hoàn thành và chuyển sang processed!"}
        )

    python_exe = ROOT / "venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    script = ROOT / "batch_processor.py"
    logs_dir = WORKSPACE / "service_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "batch_processor.log"

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    try:
        log_fp = open(log_file, "ab")
        BATCH_PROCESS = subprocess.Popen(
            [
                str(python_exe),
                "-u",
                str(script),
                "--input", str(INPUT),
                "--output", str(OUTPUT),
            ],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return JSONResponse(
            status_code=202,
            content={
                "status": "started",
                "pid": BATCH_PROCESS.pid,
                "message": f"Đã bắt đầu xử lý {len(video_files)} video từ {INPUT} sang {OUTPUT}!",
            }
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"Lỗi khởi chạy batch: {e}"}
        )

@app.post("/api/stop-batch")
async def api_stop_batch():
    """Dừng tiến trình batch đang chạy của Tool V2."""
    global BATCH_PROCESS
    stopped = False

    if BATCH_PROCESS is not None and BATCH_PROCESS.poll() is None:
        try:
            import psutil
            parent = psutil.Process(BATCH_PROCESS.pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except Exception:
                    pass
            parent.kill()
            stopped = True
        except Exception:
            try:
                BATCH_PROCESS.terminate()
                stopped = True
            except Exception:
                pass

    try:
        import psutil
        for p in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmd = " ".join(p.info.get('cmdline') or [])
                if 'batch_processor.py' in cmd and ('tool v2' in cmd.lower() or 'tool_v2' in cmd.lower()):
                    proc = psutil.Process(p.info['pid'])
                    for child in proc.children(recursive=True):
                        try:
                            child.kill()
                        except Exception:
                            pass
                    proc.kill()
                    stopped = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass

    if stopped:
        return {"status": "stopped", "message": "Đã dừng tiến trình xử lý video thành công!"}
    else:
        return JSONResponse(
            status_code=409,
            content={"status": "idle", "message": "Không có tiến trình xử lý nào đang chạy."}
        )

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
    page = page.replace('onclick="startBatch()"', '')
    # Replace paths separately in HTML and JavaScript so backslashes remain valid.
    head, script = page.split("<script>", 1)
    head = head.replace(r"D:\video phôi", html.escape(str(INPUT))).replace(r"D:\banve", html.escape(str(OUTPUT)) + " · thư mục dùng chung nếu cùng cấu hình")
    script = script.replace(r"D:\\video phôi", str(INPUT).replace("\\", "\\\\")).replace(r"D:\\banve", str(OUTPUT).replace("\\", "\\\\"))
    page = head + "<script>" + script
    override = r'''
    renderStatus = function(state) {
      const isPaused = state.is_paused || state.status === 'stopped';
      const isRunning = !isPaused && (state.active || state.batch_running || state.status === 'running');
      const statusTitle = document.getElementById('statusTitle');
      const bar = document.getElementById('progressBar');
      const btnStop = document.getElementById('btnStop');
      const btnRunHero = document.getElementById('btnRunHero');
      const btnRunPanel = document.getElementById('btnRunPanel');

      if (btnStop) btnStop.style.display = isRunning ? 'inline-flex' : 'none';
      if (btnRunHero) btnRunHero.style.display = isRunning ? 'none' : 'inline-flex';
      if (btnRunPanel) {
        btnRunPanel.disabled = isRunning;
        btnRunPanel.style.opacity = isRunning ? '0.5' : '1';
        btnRunPanel.style.cursor = isRunning ? 'not-allowed' : 'pointer';
      }

      if (isPaused) {
        statusTitle.innerHTML = '<span class="dot dot-red" style="background-color:#ef4444;box-shadow:0 0 6px rgba(239,68,68,0.5);"></span> TOOL V2 · ĐÃ TẮT (ĐÃ GIẢI PHÓNG VRAM)';
        document.getElementById('pctText').textContent = 'Đã tắt';
        bar.style.width = '0%';
        bar.style.background = '#475569';
        bar.style.boxShadow = 'none';
        bar.setAttribute('aria-valuenow', '0');
      } else {
        const dotColor = isRunning ? '#f59e0b' : (state.status === 'completed' ? '#10b981' : state.status === 'error' ? '#ef4444' : '#10b981');
        const dotShadow = isRunning ? 'rgba(245,158,11,0.5)' : (state.status === 'error' ? 'rgba(239,68,68,0.5)' : 'rgba(16,185,129,0.5)');
        const titleText = isRunning ? 'ĐANG XỬ LÝ & RENDER NGẦM' : (state.status === 'completed' ? 'HOÀN TẤT' : state.status === 'error' ? 'CÓ LỖI' : 'HỆ THỐNG SẴN SÀNG');
        statusTitle.innerHTML = '<span class="dot" style="background-color:' + dotColor + ';box-shadow:0 0 6px ' + dotShadow + ';"></span> TOOL V2 · ' + titleText;
        document.getElementById('pctText').textContent = state.percent + '%';
        bar.style.width = state.percent + '%';
        bar.style.background = '';
        bar.style.boxShadow = '';
        bar.setAttribute('aria-valuenow', String(state.percent));
      }

      document.getElementById('currentVideoName').textContent = state.video_name || (isPaused ? 'Tool V2 hiện đang tắt' : (isRunning ? 'Đang chuẩn bị xử lý video...' : 'Chưa có video V2'));
      document.getElementById('stepDescription').textContent = state.message + (state.updated_at ? ' • Cập nhật: ' + state.updated_at : '');
      bar.setAttribute('role', 'progressbar');

      const elSec = Number(state.elapsed_seconds) || 0;
      document.getElementById('valElapsed').textContent = elSec ? `${formatTime(elSec)} (${elSec}s)` : '00:00';
      document.getElementById('valEta').textContent = '--';
      const qCount = Number(state.queue_count) || 0;
      document.getElementById('valQueue').textContent = isPaused ? 'Đã tạm dừng' : (qCount > 0 ? (qCount + ' video chờ') : '0 video chờ');

      const labels = {pending:'Chờ xử lý', running:'Đang chạy', completed:'Hoàn thành', skipped:'Bỏ qua', failed:'Lỗi', stopped:'Đã dừng'};
      const icons = ['🎧','🤖','🗣️','🎬'];
      const grid = document.querySelector('.steps-grid');
      grid.className = 'steps-grid v2-steps-4';
      grid.setAttribute('role', 'list');
      grid.setAttribute('aria-label', 'Quy trình 4 bước Tool V2');
      const stagesToRender = (state.ui_steps && state.ui_steps.length === 4) ? state.ui_steps : V2_STAGES;
      grid.innerHTML = stagesToRender.map((s, i) => {
        const cls = {completed:'completed', running:'active', failed:'failed', skipped:'skipped', stopped:'stopped'}[s.status] || '';
        const durText = (s.duration_seconds !== undefined && s.duration_seconds !== null)
          ? (s.status === 'completed' ? `✓ ${s.duration_seconds}s` : (s.status === 'running' ? `⏱ ${s.duration_seconds}s` : `${s.duration_seconds}s`))
          : (s.status === 'completed' ? '✓ Xong' : (s.status === 'running' ? '⏱ Đang chạy' : '--'));

        const subtasks = s.subtasks || [];
        const subtasksHtml = subtasks.map((taskText, tIdx) => {
          let bullet = '○';
          let itemCls = '';
          if (s.status === 'completed') {
            bullet = '✓';
            itemCls = 'done';
          } else if (s.status === 'running') {
            bullet = '▸';
            itemCls = 'running';
          }
          return '<div class="v2-subtask-item ' + itemCls + '">' +
                 '<span class="v2-subtask-bullet">' + bullet + '</span>' +
                 '<span>' + escapeHtml(taskText) + '</span></div>';
        }).join('');

        return '<div role="listitem" class="step-item ' + cls + '"' + (s.status === 'running' ? ' aria-current="step"' : '') + '>' +
          '<div class="v2-step-header">' +
            '<div class="v2-step-icon-wrap">' +
              '<span class="v2-step-icon" aria-hidden="true">' + (s.icon || icons[i]) + '</span>' +
              '<span class="v2-step-number">BƯỚC ' + String(i + 1).padStart(2, '0') + '</span>' +
            '</div>' +
            '<span class="v2-step-status">' + (labels[s.status] || 'Chờ xử lý') + '</span>' +
          '</div>' +
          '<div class="v2-step-name">' + escapeHtml(s.label) + '</div>' +
          (s.model ? '<div class="v2-step-model" title="' + escapeHtml(s.model) + '">' + escapeHtml(s.model) + '</div>' : '') +
          '<div class="v2-subtasks-box">' + subtasksHtml + '</div>' +
          '<div class="v2-step-footer">' +
            '<span style="font-size:10.5px;color:var(--text-muted);">Thời gian:</span>' +
            '<div class="v2-step-duration" title="Thời gian chạy thực tế bước ' + (i + 1) + '">' + durText + '</div>' +
          '</div>' +
        '</div>';
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
    override = "const V2_STAGES = " + json.dumps([
        {
            "label": item[1],
            "model": item[2],
            "icon": item[4],
            "subtasks": item[5],
            "status": "pending"
        } for item in UI_STEPS
    ], ensure_ascii=False) + ";\n" + override
    page = page.replace("    // Auto-refresh loops", override + "\n    // Auto-refresh loops")
    page = page.replace("</style>", """
      .steps-grid.v2-steps-4 {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 14px;
        margin-top: 14px;
      }
      .v2-steps-4 .step-item {
        position: relative;
        display: flex;
        flex-direction: column;
        background: linear-gradient(155deg, #141d2a 0%, #0d131d 100%);
        border: 1px solid rgba(255, 255, 255, 0.08);
        border-radius: 12px;
        padding: 14px 14px 12px;
        transition: all 0.25s ease;
        text-align: left;
        min-height: 235px;
        box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
      }
      .v2-steps-4 .step-item:hover {
        border-color: rgba(56, 189, 248, 0.35);
        transform: translateY(-2px);
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
      }
      .v2-steps-4 .v2-step-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 8px;
        width: 100%;
      }
      .v2-steps-4 .v2-step-icon-wrap {
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .v2-steps-4 .v2-step-icon {
        font-size: 22px;
        line-height: 1;
      }
      .v2-steps-4 .v2-step-number {
        font-size: 11px;
        letter-spacing: 1.2px;
        font-weight: 800;
        color: #94a3b8;
        background: rgba(255, 255, 255, 0.05);
        padding: 2px 7px;
        border-radius: 4px;
        border: 1px solid rgba(255, 255, 255, 0.07);
      }
      .v2-steps-4 .v2-step-status {
        font-size: 10.5px;
        line-height: 1.2;
        padding: 3px 9px;
        border-radius: 20px;
        background: #1e293b;
        color: #94a3b8;
        font-weight: 600;
      }
      .v2-steps-4 .v2-step-name {
        font-size: 13.5px;
        line-height: 1.35;
        font-weight: 700;
        color: #f1f5f9;
        margin-bottom: 6px;
      }
      .v2-steps-4 .v2-step-model {
        font-size: 10px;
        line-height: 1.3;
        color: #38bdf8;
        background: rgba(56, 189, 248, 0.1);
        border: 1px solid rgba(56, 189, 248, 0.25);
        border-radius: 5px;
        padding: 3px 8px;
        margin-bottom: 10px;
        font-weight: 600;
        display: inline-block;
        width: fit-content;
      }
      .v2-steps-4 .v2-subtasks-box {
        display: flex;
        flex-direction: column;
        gap: 6px;
        margin-top: 4px;
        margin-bottom: 12px;
        background: rgba(11, 16, 23, 0.6);
        border: 1px solid rgba(255, 255, 255, 0.04);
        border-radius: 8px;
        padding: 8px 10px;
        flex: 1;
      }
      .v2-steps-4 .v2-subtask-item {
        font-size: 11px;
        color: #cbd5e1;
        display: flex;
        align-items: center;
        gap: 6px;
        line-height: 1.3;
      }
      .v2-steps-4 .v2-subtask-bullet {
        font-weight: 800;
        font-size: 11px;
        color: #64748b;
        flex-shrink: 0;
        min-width: 12px;
      }
      .v2-steps-4 .v2-step-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-top: auto;
        padding-top: 8px;
        border-top: 1px solid rgba(255, 255, 255, 0.05);
      }
      .v2-steps-4 .v2-step-duration {
        font-size: 11px;
        font-weight: 700;
        font-variant-numeric: tabular-nums;
        color: #94a3b8;
        padding: 2px 7px;
        border-radius: 4px;
        background: #0f172a80;
      }
      .v2-steps-4 .step-item.active {
        background: linear-gradient(155deg, #162b46 0%, #0f1d30 100%);
        border-color: #38bdf8;
        box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.3), 0 8px 28px rgba(56, 189, 248, 0.2);
      }
      .v2-steps-4 .active .v2-step-name { color: #38bdf8; }
      .v2-steps-4 .active .v2-step-status { color: #38bdf8; background: rgba(56, 189, 248, 0.2); font-weight: 700; animation: pulse 1.5s infinite; }
      .v2-steps-4 .active .v2-step-duration { color: #38bdf8; background: rgba(30, 58, 138, 0.6); }
      .v2-steps-4 .active .v2-subtask-item.running { color: #38bdf8; font-weight: 600; }
      .v2-steps-4 .active .v2-subtask-item.running .v2-subtask-bullet { color: #38bdf8; }
      .v2-steps-4 .step-item.completed {
        background: linear-gradient(155deg, #0e2b26 0%, #0a1f1b 100%);
        border-color: rgba(16, 185, 129, 0.45);
      }
      .v2-steps-4 .completed .v2-step-name { color: #34d399; }
      .v2-steps-4 .completed .v2-step-status { color: #34d399; background: rgba(16, 185, 129, 0.2); }
      .v2-steps-4 .completed .v2-step-duration { color: #34d399; background: rgba(6, 78, 59, 0.5); }
      .v2-steps-4 .completed .v2-subtask-bullet { color: #34d399; }
      .v2-steps-4 .completed .v2-subtask-item { color: #e2e8f0; }
      .v2-steps-4 .step-item.failed { border-color: #f87171; background: #311e29; }
      .v2-steps-4 .failed .v2-step-status { color: #fca5a5; background: #ef444420; }
      @media(max-width:1100px){.steps-grid.v2-steps-4{grid-template-columns:repeat(2,minmax(0,1fr));}}
      @media(max-width:600px){.steps-grid.v2-steps-4{grid-template-columns:1fr;gap:10px;}}
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
