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
                # Even if heartbeat timestamp is slightly old, direct O(1) PID check avoids scanning all OS processes
                if pid:
                    import psutil
                    if psutil.pid_exists(pid):
                        proc = psutil.Process(pid)
                        cmd = " ".join(proc.cmdline() or [])
                        if "telegram_bot.py" in cmd:
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
    batch_ctrl = WORKSPACE / "control" / "batch.json"
    if batch_ctrl.is_file():
        try:
            d = json.loads(batch_ctrl.read_text(encoding="utf-8"))
            pid = int(d.get("pid") or 0)
            if pid:
                import psutil
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    cmd = " ".join(proc.cmdline() or [])
                    if "batch_processor.py" in cmd:
                        return True
        except Exception:
            pass
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

def is_v2_worker_running(workspace=None, job_id=None):
    """Kiểm tra xem có bất kỳ tiến trình xử lý video nào của Tool V2 đang chạy không."""
    ws = str(workspace or WORKSPACE).lower()
    try:
        import psutil
        v2_keywords = (
            "render_douyin_v2.py",
            "render_video_phoi.py",
            "render_local_video.py",
            "batch_processor.py",
            "telegram_bot.py",
            "pipeline_v2",
            "gpu_worker",
        )
        for p in psutil.process_iter(['name']):
            if 'python' not in (p.info.get('name') or '').lower():
                continue
            try:
                cmd_parts = p.cmdline() or []
                cmd = " ".join(cmd_parts).lower()
                if any(part == '-c' for part in cmd_parts):
                    continue
                if any(kw in cmd for kw in v2_keywords):
                    if job_id and job_id.lower() in cmd:
                        return True
                    if ws in cmd:
                        return True
                    if ('tool v2' in ws or 'tool_v2' in ws) and ('tool v2' in cmd or 'tool_v2' in cmd or 'workspace' in cmd):
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False

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
            try:
                row = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()
                q_count = row[0] if row else 0
            finally:
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
              "eta_seconds": None,
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

    # Kiểm tra log tiến độ xử lý batch nếu batch_processor đang chạy và chưa có manifest
    if batch_alive and not candidates:
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
    job_id = data.get("job_id") or path.parent.parent.name
    worker_alive = is_v2_worker_running(workspace=WORKSPACE, job_id=job_id)

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
            elif st_started and st_data.get("status") == "running":
                try:
                    t0 = datetime.fromisoformat(st_started.replace("Z", "+00:00"))
                    sub_durations.append(max(0.0, (now - t0).total_seconds()))
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
    
    # Active if worker/batch/bot is alive and task not completed/failed/paused
    active = not paused and not delivered and not failed and (
        batch_alive or worker_alive or (bot_alive and bool(running))
    )
    result["active"] = active
    result["batch_running"] = batch_alive

    if paused:
        result["status"] = "stopped"
        result["percent"] = 100 if delivered else min(99, int(finished / len(STAGES) * 100))
        result["message"] = ("Đã xuất thành phẩm trước đó. Tool V2 hiện đang TẮT (VRAM đã giải phóng)." if delivered else
                             "Tool V2 hiện đang TẮT. Toàn bộ tiến trình bot và bộ nhớ VRAM đã được giải phóng.")
        result["step"] = current_step or 0
    else:
        # Progress calculation
        progress_count = finished + (0.5 if running else 0)
        pct = 100 if delivered else min(99, max(5 if active else 0, int((progress_count / len(STAGES)) * 100)))
        result["percent"] = pct

        if failed:
            result["status"] = "error"
            result["message"] = "Lỗi tại: " + ", ".join(failed)
            result["step"] = current_step or 1
        elif delivered:
            result["status"] = "completed"
            result["message"] = "Đã xuất thành phẩm."
            result["step"] = 4
        elif active:
            result["status"] = "running"
            result["message"] = ("Đang xử lý hàng loạt video..." if batch_alive else ("Đang thực thi: " + (", ".join(running) if running else "Đang thực hiện quy trình V2...")))
            result["step"] = current_step or 1
        else:
            result["status"] = "recorded"
            result["message"] = ("Bước ghi nhận: " + ", ".join(running) if running else "Chưa hoàn tất.") + " Trạng thái lưu trên đĩa; chưa xác minh tiến trình còn chạy."
            result["step"] = current_step or 1

    # Elapsed seconds calculation
    try:
        started = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
        if active:
            result["elapsed_seconds"] = max(0, int((now - started).total_seconds()))
        else:
            ended = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
            result["elapsed_seconds"] = max(0, int((ended - started).total_seconds()))
    except (ValueError, KeyError, TypeError):
        result["elapsed_seconds"] = 0

    # ETA (Estimated remaining time in seconds)
    if delivered or result["percent"] >= 100:
        result["eta_seconds"] = 0
    elif active and result["elapsed_seconds"] > 0:
        eff_pct = max(result["percent"], 5)
        rem_dyn = max(5, int((result["elapsed_seconds"] / (eff_pct / 100.0)) - result["elapsed_seconds"]))
        src_dur = data.get("metadata", {}).get("source_duration_seconds")
        if src_dur and result["elapsed_seconds"] < 45:
            est_from_src = max(30, int(src_dur * 0.3))
            alpha = min(1.0, result["elapsed_seconds"] / 45.0)
            result["eta_seconds"] = int((1.0 - alpha) * max(10, est_from_src - result["elapsed_seconds"]) + alpha * rem_dyn)
        else:
            result["eta_seconds"] = rem_dyn
    else:
        result["eta_seconds"] = None

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
    return {"status": "ok", "message": "Đã mở thư mục thành công."}

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
        log_fp.close()  # Parent không cần FD nữa, child đã kế thừa
        try:
            batch_ctrl = WORKSPACE / "control" / "batch.json"
            batch_ctrl.parent.mkdir(parents=True, exist_ok=True)
            batch_ctrl.write_text(json.dumps({"pid": BATCH_PROCESS.pid, "at": time.time()}), encoding="utf-8")
        except Exception:
            pass
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

    # Direct check via batch.json before scanning OS
    batch_ctrl = WORKSPACE / "control" / "batch.json"
    if batch_ctrl.is_file():
        try:
            d = json.loads(batch_ctrl.read_text(encoding="utf-8"))
            pid = int(d.get("pid") or 0)
            if pid:
                import psutil
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    for child in proc.children(recursive=True):
                        try:
                            child.kill()
                        except Exception:
                            pass
                    proc.kill()
                    stopped = True
            batch_ctrl.unlink(missing_ok=True)
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
            try:
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
            finally:
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
    # Replace paths separately in HTML and JavaScript so backslashes remain valid.
    head, script = page.split("<script>", 1)
    head = head.replace(r"D:\video phôi", html.escape(str(INPUT))).replace(r"D:\video tool v2", html.escape(str(OUTPUT)))
    script = script.replace(r"D:\\video phôi", str(INPUT).replace("\\", "\\\\")).replace(r"D:\\video tool v2", str(OUTPUT).replace("\\", "\\\\"))
    return head + "<script>" + script

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
