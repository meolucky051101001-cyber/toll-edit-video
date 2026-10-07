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
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Form, HTTPException, Body, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from dashboard_media import media_status, paginate_listing, paginate_output, verify_dashboard_delivery
from environment import read_environment
try:
    from config.paths import AppPaths
except ImportError:
    from backend.config.paths import AppPaths

ROOT = Path(__file__).resolve().parent
ENV = read_environment(ROOT)
PATHS = AppPaths.from_environment(ROOT.parent, ENV)
WORKSPACE = PATHS.workspace

def _resolve_queue_db() -> Path:
    bs = WORKSPACE / "bot_system"
    target = bs / "queue_v2.sqlite3"
    if target.exists() or bs.is_dir():
        return target
    return WORKSPACE / "queue_v2.sqlite3"

INPUT = PATHS.input_dir

def get_input_dir() -> Path:
    cfg = WORKSPACE / "dashboard_input.json"
    if cfg.is_file():
        try:
            d = json.loads(cfg.read_text(encoding="utf-8"))
            p = Path(d.get("path", "")).resolve()
            if p.is_dir():
                return p
        except Exception:
            pass
    return INPUT

_env_output = PATHS.output_dir
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
            "Khớp luồng chống đơ & thư mục xuất theo cấu hình",
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
    ws_str = str(WORKSPACE).lower()
    if 'tool v2' not in ws_str and 'tool_v2' not in ws_str:
        return False

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
    ws_str = str(WORKSPACE).lower()
    if 'tool v2' not in ws_str and 'tool_v2' not in ws_str:
        return False
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
                    if job_id and len(job_id) >= 6 and job_id.lower() not in ("video", "default", "workspace") and job_id.lower() in cmd:
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


def _translation_models_for_job(manifest_path, manifest_data):
    """Read exact successful translation models saved in per-batch checkpoints."""
    models = []
    batch_dir = manifest_path.parent / "artifacts" / "translation" / "batches"
    if batch_dir.is_dir():
        for checkpoint in sorted(batch_dir.glob("*.json"))[-512:]:
            try:
                quality = json.loads(checkpoint.read_text(encoding="utf-8")).get("quality", {})
                found = quality.get("models") or ([quality.get("model")] if quality.get("model") else [])
                if not found and quality.get("provider"):
                    found = ["{} · chưa ghi nhận model".format(quality["provider"].upper())]
                for model in found:
                    if model and model not in models:
                        models.append(str(model))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    if models:
        return models

    # Older jobs keep aggregate translation provenance in this artifact.
    context_path = manifest_path.parent / "artifacts" / "translation" / "context.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
        for batch in context.get("batches", []):
            found = batch.get("models") or ([batch.get("model")] if batch.get("model") else [])
            if not found and batch.get("provider"):
                found = ["{} · chưa ghi nhận model".format(batch["provider"].upper())]
            for model in found:
                if model and model not in models:
                    models.append(str(model))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return models


def read_status():
    candidates = sorted(WORKSPACE.glob("*/pipeline_v2/job_manifest.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    paused = is_v2_paused()
    bot_alive = is_v2_bot_running()
    batch_alive = is_v2_batch_running()
    
    # Query pending jobs from SQLite queue
    db_path = _resolve_queue_db()
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

    cfg_model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
    result = {"stages": stage_list, "ui_steps": ui_step_list, "step_durations": {},
              "configured_gemini_model": cfg_model,
              "active_translation_model": cfg_model,
              "translation_models": [cfg_model],
              "percent": 0, "video_name": "", "elapsed_seconds": 0,
              "eta_seconds": None,
              "message": "Chưa có tác vụ nào đang chạy.",
              "status": "stopped" if paused else ("running" if batch_alive else "idle"),
              "is_paused": paused,
              "batch_running": batch_alive,
              "active": batch_alive,
              "queue_count": q_count,
              "queue_index": 0,
              "queue_total": 0,
              "pause_state": "paused" if (WORKSPACE / "control" / "video.pause").is_file() else "running",
              "video_status": "idle",
              "last_completed": None,
              "step": 0,
              "step_name": "Sẵn sàng xử lý video"}
    if paused:
        result["active"] = False
        result["message"] = "Tool V2 đang tắt; đang hiển thị trạng thái đã lưu."

    # Kiểm tra log tiến độ xử lý batch nếu batch_processor đang chạy và chưa có manifest
    if batch_alive and not paused and not candidates:
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
                        result["step_name"] = "Bước 1: Đang trích xuất & tách âm thanh (BS-RoFormer GPU)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 2/4" in line:
                        result["step"] = 4
                        result["percent"] = 55
                        result["step_name"] = "Bước 4: Nhận diện giọng nói & Dịch thuật AI (Whisper + Gemini)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 3/4" in line:
                        result["step"] = 5
                        result["percent"] = 75
                        result["step_name"] = "Bước 5: Lồng tiếng AI & Hòa âm trong trẻo..."
                        result["message"] = result["step_name"]
                        break
                    elif "Bước 4/4" in line:
                        result["step"] = 6
                        result["percent"] = 90
                        result["step_name"] = "Bước 6: Đang Render video thành phẩm (NVENC GPU)..."
                        result["message"] = result["step_name"]
                        break
                    elif "Hoàn thành video" in line:
                        result["step"] = 8
                        result["percent"] = 99
                        result["step_name"] = "Log ghi nhận hoàn thành; chưa có manifest để xác minh thành phẩm."
                        result["message"] = result["step_name"]
                        result["video_status"] = "unverified"
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
    vname = result["video_name"]
    verification = {"valid": False, "pending": False, "reason": "Chưa xác minh thành phẩm."}
    try:
        verification = verify_dashboard_delivery(path)
    except Exception as exc:
        verification["reason"] = "Không xác minh được thành phẩm: " + str(exc)
    delivered = verification["valid"]
    failed = [s["label"] for s in result["stages"] if s["status"] == "failed"]
    running = [s["label"] for s in result["stages"] if s["status"] == "running"]
    delivery_invalid = records.get("deliver", {}).get("status") == "completed" and not delivered and not verification["pending"]
    if delivery_invalid:
        failed.append("Xác minh thành phẩm")
    if failed:
        delivered = False
        result["last_error"] = verification["reason"] if delivery_invalid else "Lỗi tại: " + ", ".join(failed)
    
    # Active if worker/batch/bot is alive and task not completed/failed/paused
    active = not paused and not delivered and not failed and (
        batch_alive or worker_alive or (bot_alive and bool(running))
    )
    result["active"] = active
    result["batch_running"] = batch_alive

    if failed:
        result["status"] = "error"
        result["percent"] = min(99, int(finished / len(STAGES) * 100))
        result["message"] = result["last_error"]
        result["step"] = current_step or 1
    elif paused:
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

    # Tính toán thời gian thực tế từng bước (step_durations) cho quy trình 8 bước
    step_durations = {}
    stage_to_step = {
        "1": ["extract_audio"],
        "2": ["demucs"],
        "3": ["transcribe"],
        "4": ["ocr"],
        "3.5": ["ocr"],
        "5": ["translate"],
        "6": ["tts", "rvc"],
        "7": ["subtitles", "mix_legacy", "mix_v2"],
        "8": ["render", "qc", "deliver"],
    }
    for sk_step, sub_keys in stage_to_step.items():
        sub_durs = []
        for sk in sub_keys:
            st_data = records.get(sk, {})
            st_s = st_data.get("started_at")
            st_f = st_data.get("finished_at")
            if st_s and st_f:
                try:
                    t0 = datetime.fromisoformat(st_s.replace("Z", "+00:00"))
                    t1 = datetime.fromisoformat(st_f.replace("Z", "+00:00"))
                    sub_durs.append(max(0.0, (t1 - t0).total_seconds()))
                except Exception:
                    pass
            elif st_s and st_data.get("status") == "running":
                try:
                    t0 = datetime.fromisoformat(st_s.replace("Z", "+00:00"))
                    sub_durs.append(max(0.0, (now - t0).total_seconds()))
                except Exception:
                    pass
        if sub_durs:
            step_durations[sk_step] = round(sum(sub_durs), 1)
    result["step_durations"] = step_durations
    result["total_steps"] = 8

    if records.get("deliver", {}).get("status") in ("running", "completed") or records.get("render", {}).get("status") in ("running", "completed") or records.get("qc", {}).get("status") in ("running", "completed"):
        result["step"] = 8
    elif records.get("mix_v2", {}).get("status") in ("running", "completed") or records.get("mix_legacy", {}).get("status") in ("running", "completed"):
        result["step"] = 7
    elif records.get("tts", {}).get("status") in ("running", "completed") or records.get("rvc", {}).get("status") in ("running", "completed") or records.get("subtitles", {}).get("status") in ("running", "completed"):
        result["step"] = 6
    elif records.get("translate", {}).get("status") in ("running", "completed") or records.get("timing", {}).get("status") in ("running", "completed"):
        result["step"] = 5
    elif records.get("ocr", {}).get("status") in ("running", "completed"):
        result["step"] = 4
    elif records.get("transcribe", {}).get("status") in ("running", "completed"):
        result["step"] = 3
    elif records.get("demucs", {}).get("status") in ("running", "completed"):
        result["step"] = 2
    elif records.get("extract_audio", {}).get("status") in ("running", "completed"):
        result["step"] = 1

    step_stage_keys = {1:["input","extract_audio"], 2:["demucs"], 3:["transcribe"], 4:["ocr"],
                       5:["translate","timing"], 6:["tts","rvc"], 7:["subtitles","mix_legacy","mix_v2"],
                       8:["render","qc","deliver"]}
    for step_id, keys in step_stage_keys.items():
        if any(records.get(key, {}).get("status") == "failed" for key in keys):
            result["step"] = step_id
            break
    result["step_name"] = result["message"]
    result["job_id"] = job_id
    result["start_time"] = data.get("created_at")
    if failed:
        result["video_status"] = "error"
    elif delivered:
        result["video_status"] = "completed"
    elif active:
        result["video_status"] = "running"
    else:
        result["video_status"] = "idle"

    # Video hoàn thành gần nhất
    try:
        if OUTPUT.is_dir():
            recent_outputs = sorted(
                [f for f in OUTPUT.iterdir() if f.is_file() and f.suffix.lower() in MEDIA],
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )
            if recent_outputs:
                result["last_completed"] = {"video_name": recent_outputs[0].name}
    except Exception:
        pass

    # Tổng hàng đợi và vị trí xử lý
    try:
        inp = get_input_dir()
        phoi_count = len([f for f in inp.iterdir() if f.is_file() and f.suffix.lower() in MEDIA and not f.name.startswith("Dubbed_")]) if inp.is_dir() else 0
        banve_count = len([f for f in OUTPUT.iterdir() if f.is_file() and f.suffix.lower() in MEDIA]) if OUTPUT.is_dir() else 0
        result["queue_total"] = phoi_count + banve_count
    except Exception:
        pass

    # Cấu hình & trạng thái model dịch thuật
    cfg_model = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
    result["configured_gemini_model"] = cfg_model
    meta_models = (data.get("metadata", {}) or {}).get("models", {})
    gem_m = meta_models.get("gemini") or cfg_model
    result["translation_models"] = [gem_m] if gem_m else [cfg_model]
    result["active_translation_model"] = gem_m or cfg_model

    return result

@app.get("/api/status")
async def status():
    try:
        result = await asyncio.to_thread(read_status)
        result["output_dir"] = str(OUTPUT)
        result["tool"] = "v2"
        return result
    except (OSError, ValueError, TypeError):
        raise HTTPException(503, "Không đọc được manifest V2; sẽ thử lại.")

def listing(root, kind="input", limit=None, offset=0, search="", status="all"):
    files = []
    try:
        from render_history import get_all_render_durations, format_duration
        durations = get_all_render_durations(root)
    except Exception:
        durations = {}
        format_duration = lambda s: "--"

    is_input_folder = kind == "input"
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
                if p.name == active_video and is_active:
                    status_val = "running"
                    label = f"Đang chạy ({pct}%)"
                elif p.name == active_video and cur_status.get("status") == "error":
                    status_val = "error"
                    label = "Xử lý lỗi"
                else:
                    # Check if there is a verified job manifest for this input video
                    is_verified = False
                    try:
                        from pipeline_v2.delivery_verification import verify_delivered_product
                        possible_dirs = [WORKSPACE / f"batch_{stem}"]
                        possible_dirs.extend(list(WORKSPACE.glob(f"batch_*_{stem}")))
                        for j_dir in possible_dirs:
                            if j_dir.is_dir() and (j_dir / "job_manifest.json").is_file():
                                if verify_dashboard_delivery(j_dir, expected_source_path=p)["valid"]:
                                    is_verified = True
                                    break
                    except Exception:
                        is_verified = False

                    if is_verified:
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
    result = paginate_listing({"files": files, "total_count": len(files),
            "exists": root.is_dir(),
            "total_size_mb": round(sum(f["size_mb"] for f in files), 2),
            "path": str(root)}, limit if is_input_folder else None, offset if is_input_folder else 0, search if is_input_folder else "", status if is_input_folder else "all")
    if not is_input_folder:
        return paginate_output(result, root, limit, offset, search, status)
    return result

@app.get("/api/phoi")
async def inputs(limit: Optional[int] = None, offset: int = 0, search: str = "", status: str = "all"):
    return await asyncio.to_thread(listing, get_input_dir(), "input", limit, offset, search, status)

@app.get("/api/banve")
async def outputs(limit: Optional[int] = None, offset: int = 0, search: str = "", status: str = "all"):
    return await asyncio.to_thread(listing, OUTPUT, "output", limit, offset, search, status)

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
    return get_input_dir() if key == "phoi" else OUTPUT

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

    current_input = get_input_dir()
    # Kiểm tra thư mục đầu vào có file video cần xử lý không
    video_files = [
        f for f in current_input.glob("*")
        if f.suffix.lower() in MEDIA and not f.name.startswith("Dubbed_") and f.is_file()
    ] if current_input.is_dir() else []

    if not video_files:
        return JSONResponse(
            status_code=200,
            content={"status": "completed", "message": f"Toàn bộ video trong thư mục {current_input} đều đã được hoàn thành và chuyển sang processed!"}
        )

    python_exe = ROOT / "venv" / "Scripts" / "python.exe"
    if not python_exe.exists():
        python_exe = Path(sys.executable)

    script = ROOT / "batch_processor.py"
    logs_dir = WORKSPACE / "service_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    from voice_selection import is_dual_voice_enabled
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    if is_dual_voice_enabled():
        env["ENABLE_AUTO_GENDER"] = "true"
    else:
        env["ENABLE_AUTO_GENDER"] = "false"
    try:
        log_fp = open(log_file, "ab")
        BATCH_PROCESS = subprocess.Popen(
            [
                str(python_exe),
                "-u",
                str(script),
                "--input", str(current_input),
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

@app.get("/api/preflight")
async def api_preflight():
    from preflight_checker import run_full_preflight
    return await asyncio.to_thread(
        run_full_preflight,
        tool="v2",
        input_dir=get_input_dir(),
        output_dir=OUTPUT,
        workspace_dir=WORKSPACE,
        backend_dir=ROOT,
    )

@app.get("/api/task-history")
async def api_task_history():
    from history_service import get_task_history
    return await asyncio.to_thread(
        get_task_history,
        tool_name="v2",
        output_dir=OUTPUT,
        workspace_dir=WORKSPACE,
    )

@app.get("/api/qc-report")
async def api_qc_report(target: Optional[str] = None):
    from qc_service import get_qc_report
    report = await asyncio.to_thread(get_qc_report, WORKSPACE, target)
    if not report:
        raise HTTPException(status_code=404, detail="Không tìm thấy báo cáo QC Gate nào.")
    return report

@app.get("/api/qc-reports")
async def api_qc_reports():
    from qc_service import list_qc_reports
    return await asyncio.to_thread(list_qc_reports, WORKSPACE)

@app.post("/api/retry-video")
async def api_retry_video(request: Request):
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

    filename = ""
    try:
        data = await request.json()
        filename = data.get("filename", "").strip()
    except Exception:
        pass
    if not filename:
        try:
            form = await request.form()
            filename = form.get("filename", "").strip()
        except Exception:
            pass

    if not filename:
        raise HTTPException(status_code=400, detail="Thiếu tên video cần chạy lại (filename).")

    current_input = get_input_dir()
    video_path = current_input / filename
    
    if not video_path.is_file():
        processed_file = current_input / "processed" / filename
        if processed_file.is_file():
            try:
                import shutil
                shutil.copy2(processed_file, video_path)
            except Exception:
                video_path = processed_file
        else:
            raise HTTPException(status_code=404, detail=f"Không tìm thấy file video {filename} trong thư mục phôi.")

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
                "--video", str(video_path),
                "--output", str(OUTPUT),
            ],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log_fp.close()
        try:
            batch_ctrl = WORKSPACE / "control" / "batch.json"
            batch_ctrl.parent.mkdir(parents=True, exist_ok=True)
            batch_ctrl.write_text(json.dumps({"pid": BATCH_PROCESS.pid, "video": filename, "at": time.time()}), encoding="utf-8")
        except Exception:
            pass
        return JSONResponse(
            status_code=200,
            content={"status": "started", "video": filename, "message": f"Đã bắt đầu xử lý lại video '{filename}'!"}
        )
    except Exception as e:
        logger_name = globals().get("logger")
        if logger_name:
            logger_name.error(f"Lỗi khởi động retry video: {e}")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})

@app.get("/api/stream/{key}/{filename}")
async def stream(key: str, filename: str):
    root = folder(key).resolve()
    target = (root / filename).resolve()
    if target.parent != root or target.suffix.lower() not in MEDIA or not target.is_file():
        raise HTTPException(404, "Video không tồn tại")
    return FileResponse(target)

FOLDER_PICKER_LOCK = asyncio.Lock()
BATCH_INPUT_LOCK = asyncio.Lock()

def _check_input_request(request: Request):
    if request.headers.get("X-Dashboard-Input") != "1" or request.headers.get("origin") not in (None, "http://127.0.0.1:8089", "http://localhost:8089", "http://127.0.0.1:8088", "http://localhost:8088"):
        raise HTTPException(403, "Yêu cầu không hợp lệ")
    if is_v2_batch_running():
        raise HTTPException(409, "Chờ video đang xử lý hoàn tất trước khi đổi nguồn hoặc thêm video.")

@app.post("/api/choose-input-folder")
async def api_choose_input_folder(request: Request):
    _check_input_request(request)
    if FOLDER_PICKER_LOCK.locked():
        raise HTTPException(409, "Hộp chọn thư mục đang mở. Hãy chọn hoặc bấm Hủy trong cửa sổ đó.")
    async with FOLDER_PICKER_LOCK:
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(ROOT / "choose_video_folder.py"), str(get_input_dir()),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=180)
            if process.returncode:
                raise HTTPException(503, "Không mở được hộp chọn thư mục Windows. Hãy kiểm tra phiên desktop đang đăng nhập.")
            return json.loads(stdout.decode("utf-8"))
        except asyncio.TimeoutError:
            raise HTTPException(408, "Hết thời gian chọn thư mục. Bấm Chọn thư mục để thử lại.")
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

@app.post("/api/input-folder")
async def api_input_folder(request: Request):
    async with BATCH_INPUT_LOCK:
        _check_input_request(request)
        data = await request.json()
        value = data.get("path", "") if isinstance(data, dict) else ""
        if not isinstance(value, str) or not value.strip():
            raise HTTPException(400, "Nhập đường dẫn thư mục video.")
        path = Path(value.strip().strip('"'))
        if not path.is_absolute() or not path.is_dir():
            raise HTTPException(400, "Thư mục không tồn tại. Hãy nhập đường dẫn đầy đủ.")
        path = path.resolve()
        if path == OUTPUT.resolve():
            raise HTTPException(400, "Hãy chọn thư mục nguồn khác thư mục video hoàn thành.")
        dest = WORKSPACE / "dashboard_input.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp")
        tmp.write_text(json.dumps({"path": str(path)}), encoding="utf-8")
        os.replace(tmp, dest)
        return {"path": str(path)}

@app.post("/api/input-video")
async def api_input_video(request: Request):
    from urllib.parse import unquote
    import uuid
    async with BATCH_INPUT_LOCK:
        _check_input_request(request)
        name = unquote(request.headers.get("X-Video-Name", ""))
        if not name or Path(name).name != name or any(c in name for c in '/\\:') or Path(name).suffix.lower() not in MEDIA:
            raise HTTPException(400, "Chỉ nhận file video MP4, MKV, MOV, AVI, WEBM, FLV, M4V.")
        if name.startswith("Dubbed_"):
            name = "Source_" + name
        root = get_input_dir().resolve()
        if not root.is_dir():
            raise HTTPException(400, "Thư mục nguồn không còn tồn tại. Hãy chọn lại.")
        tmp = root / (".upload-" + uuid.uuid4().hex + ".part")
        target = root / name
        total = 0
        try:
            with tmp.open("xb") as out:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > 4 * 1024**3:
                        raise HTTPException(413, "Mỗi video tối đa 4 GB.")
                    await asyncio.to_thread(out.write, chunk)
            if not total:
                raise HTTPException(400, "File video rỗng.")
            if target.exists():
                target = root / (Path(name).stem + "_" + uuid.uuid4().hex[:8] + Path(name).suffix)
            tmp.rename(target)
            return {"name": target.name, "size": total}
        finally:
            tmp.unlink(missing_ok=True)

@app.post("/api/pause-video")
async def api_pause_video(request: Request):
    ctrl = WORKSPACE / "control"
    ctrl.mkdir(parents=True, exist_ok=True)
    flag = ctrl / "video.pause"
    import uuid
    if not flag.exists():
        flag.write_text(uuid.uuid4().hex, encoding="utf-8")
    return {"message": "Sẽ tạm dừng sau bước đang chạy."}

@app.post("/api/resume-video")
async def api_resume_video(request: Request):
    ctrl = WORKSPACE / "control"
    (ctrl / "video.pause").unlink(missing_ok=True)
    (ctrl / "video.pause.ack").unlink(missing_ok=True)
    return {"message": "Đã tiếp tục xử lý video."}


from audio_settings import get_audio_settings, save_audio_settings

@app.get("/api/audio-settings")
async def api_get_audio_settings():
    return get_audio_settings()

@app.post("/api/audio-settings")
async def api_save_audio_settings(payload: dict = Body(...)):
    bgm = payload.get("bgm_volume_db", -2.0)
    dub = payload.get("dubbing_volume_db", 1.0)
    sep = payload.get("separation_mode")
    duck = payload.get("ducking_mode")
    return save_audio_settings(bgm, dub, separation_mode=sep, ducking_mode=duck)


import voice_selection

@app.get("/api/voice-auto")
async def api_get_voice_auto():
    """Lấy trạng thái và cấu hình chế độ tự động nhận diện giọng nói & Dual Voice (Tool V2)."""
    cfg = voice_selection.get_auto_voice_config()
    catalog = voice_selection.catalog()
    manual_voice = voice_selection.selected()

    female_voice = next((v for v in catalog if v.get("id") == cfg["female_voice_id"]), None)
    male_voice = next((v for v in catalog if v.get("id") == cfg["male_voice_id"]), None)

    effective_mode = "dual" if (cfg["enabled"] and cfg.get("dual_voice", False)) else ("auto_single" if cfg["enabled"] else "manual")
    return {
        "enabled": cfg["enabled"],
        "mode": "auto" if cfg["enabled"] else "manual",
        "effective_mode": effective_mode,
        "dual_voice": bool(cfg.get("dual_voice", False)),  # LUÔN TẮT MẶC ĐỊNH
        "female_voice_id": cfg["female_voice_id"],
        "male_voice_id": cfg["male_voice_id"],
        "female_voice_label": female_voice["label"] if female_voice else cfg["female_voice_id"],
        "male_voice_label": male_voice["label"] if male_voice else cfg["male_voice_id"],
        "manual_voice": manual_voice,
        "voices": catalog,
    }


@app.post("/api/voice-auto")
async def api_set_voice_auto(payload: dict = Body(...)):
    """Bật / Tắt và cấu hình giọng tự động nhận diện giọng nói & Dual Voice (Tool V2)."""
    current_cfg = voice_selection.get_auto_voice_config()
    catalog = voice_selection.catalog()
    valid_voice_ids = {v["id"] for v in catalog}

    def _parse_bool_val(val, default):
        if val is None:
            return default
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        s = str(val).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        return default

    target_enabled = _parse_bool_val(payload.get("enabled"), current_cfg["enabled"]) if "enabled" in payload else current_cfg["enabled"]
    target_dual = _parse_bool_val(payload.get("dual_voice"), current_cfg.get("dual_voice", False)) if "dual_voice" in payload else current_cfg.get("dual_voice", False)

    if "mode" in payload:
        mode_val = str(payload["mode"]).strip().lower()
        if mode_val in ("manual", "single_manual"):
            target_enabled = False
            target_dual = False
        elif mode_val in ("auto", "auto_single", "single_auto"):
            target_enabled = True
            target_dual = False
        elif mode_val in ("dual", "dual_voice"):
            target_enabled = True
            target_dual = True

    raw_female = payload.get("female_voice_id") if "female_voice_id" in payload else payload.get("female_voice")
    raw_male = payload.get("male_voice_id") if "male_voice_id" in payload else payload.get("male_voice")

    target_female_id = current_cfg["female_voice_id"]
    if raw_female is not None:
        raw_f_str = str(raw_female).strip()
        matched_f = next((v for v in catalog if v.get("id") == raw_f_str or v.get("param") == raw_f_str), None)
        if matched_f:
            target_female_id = matched_f["id"]
        elif raw_f_str in valid_voice_ids:
            target_female_id = raw_f_str
        else:
            return {
                "status": "error",
                "message": f"Giọng nữ '{raw_female}' không hợp lệ hoặc không tồn tại trong danh mục.",
                "enabled": current_cfg["enabled"],
                "dual_voice": bool(current_cfg.get("dual_voice", False)),
                "female_voice_id": current_cfg["female_voice_id"],
                "male_voice_id": current_cfg["male_voice_id"],
            }

    target_male_id = current_cfg["male_voice_id"]
    if raw_male is not None:
        raw_m_str = str(raw_male).strip()
        matched_m = next((v for v in catalog if v.get("id") == raw_m_str or v.get("param") == raw_m_str), None)
        if matched_m:
            target_male_id = matched_m["id"]
        elif raw_m_str in valid_voice_ids:
            target_male_id = raw_m_str
        else:
            return {
                "status": "error",
                "message": f"Giọng nam '{raw_male}' không hợp lệ hoặc không tồn tại trong danh mục.",
                "enabled": current_cfg["enabled"],
                "dual_voice": bool(current_cfg.get("dual_voice", False)),
                "female_voice_id": current_cfg["female_voice_id"],
                "male_voice_id": current_cfg["male_voice_id"],
            }

    raw_manual = payload.get("manual_voice_id") if "manual_voice_id" in payload else payload.get("voice_id")
    if raw_manual is not None:
        raw_man_str = str(raw_manual).strip()
        matched_man = next((v for v in catalog if v.get("id") == raw_man_str or v.get("param") == raw_man_str), None)
        target_man_id = matched_man["id"] if matched_man else (raw_man_str if raw_man_str in valid_voice_ids else None)
        if target_man_id:
            try:
                voice_selection.save(target_man_id)
            except Exception as _ve:
                logger.warning(f"Không thể lưu manual voice: {_ve}")

    cfg = voice_selection.set_auto_voice_config(
        enabled=target_enabled,
        female_voice_id=target_female_id,
        male_voice_id=target_male_id,
        dual_voice=target_dual,
        updated_by="dashboard"
    )

    manual_voice = voice_selection.selected()
    female_voice = next((v for v in catalog if v.get("id") == cfg["female_voice_id"]), None)
    male_voice = next((v for v in catalog if v.get("id") == cfg["male_voice_id"]), None)

    msg = "Đã cập nhật cấu hình giọng"
    if "mode" in payload:
        if mode_val == "manual":
            msg = "Đã chuyển sang chế độ 1 giọng thủ công"
        elif mode_val in ("auto", "auto_single", "single_auto"):
            msg = "Đã chuyển sang chế độ Tự chọn 1 giọng (Auto Single)"
        elif mode_val in ("dual", "dual_voice"):
            msg = "Đã chuyển sang chế độ Phân vai Nam & Nữ (Dual Voice)"
    elif "dual_voice" in payload and len(payload) == 1:
        msg = "Đã BẬT phân vai Nam & Nữ trong cùng video" if target_dual else "Đã TẮT phân vai Nam/Nữ (Mặc định 1 giọng cả video)"
    elif "enabled" in payload and len(payload) == 1:
        msg = "Đã BẬT tự động nhận diện giọng đầu video" if target_enabled else "Đã TẮT tự động nhận diện (Dùng giọng thủ công)"

    effective_mode = "dual" if (cfg["enabled"] and cfg.get("dual_voice", False)) else ("auto_single" if cfg["enabled"] else "manual")
    return {
        "status": "ok",
        "enabled": cfg["enabled"],
        "mode": "auto" if cfg["enabled"] else "manual",
        "effective_mode": effective_mode,
        "dual_voice": bool(cfg.get("dual_voice", False)),
        "female_voice_id": cfg["female_voice_id"],
        "male_voice_id": cfg["male_voice_id"],
        "female_voice_label": female_voice["label"] if female_voice else cfg["female_voice_id"],
        "male_voice_label": male_voice["label"] if male_voice else cfg["male_voice_id"],
        "manual_voice": manual_voice,
        "voices": catalog,
        "message": msg,
    }


def read_queue():
    paused = is_v2_paused()
    bot_alive = is_v2_bot_running()
    alive = bot_alive and not paused
    items = []

    db_path = _resolve_queue_db()
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
                        "id": r["id"],
                        "position": idx,
                        "name": name,
                        "url": payload.get("url") or "",
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

    for q_candidate in [WORKSPACE / "telegram_queue.json"]:
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


@app.post("/api/queue/process")
async def api_process_queue():
    """Kích hoạt xử lý lại danh sách video đang đợi trong hàng chờ V2."""
    queue_candidates = [
        WORKSPACE / "telegram_queue.json",
    ]
    queue_file = None
    for qc in queue_candidates:
        if qc.is_file():
            queue_file = qc
            break

    db_path = _resolve_queue_db()
    has_db_items = False
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                count = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0]
                if count > 0:
                    has_db_items = True
            finally:
                conn.close()
        except Exception:
            pass

    if not queue_file and not has_db_items:
        return {"status": "empty", "message": "Không tìm thấy hàng chờ video hoặc hàng chờ đang trống."}

    # 1. Gỡ cờ pause nếu Tool V2 đang bị tạm dừng
    for pause_candidate in [WORKSPACE / "control" / "v2.pause"]:
        try:
            pause_candidate.unlink(missing_ok=True)
        except Exception:
            pass

    # 2. Ghi cờ kích hoạt telegram_queue_trigger.flag
    for trigger_candidate in [WORKSPACE / "telegram_queue_trigger.flag"]:
        try:
            trigger_candidate.parent.mkdir(parents=True, exist_ok=True)
            trigger_candidate.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    # 3. Kiểm tra xem telegram_bot.py của v2 có đang chạy không
    alive = False
    import psutil
    for p in psutil.process_iter(['pid', 'cmdline']):
        try:
            cmdline = p.info.get('cmdline') or []
            if any('telegram_bot.py' in str(arg) for arg in cmdline) and any(('tool v2' in str(arg) or 'v2' in str(arg).lower()) for arg in cmdline):
                alive = True
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not alive:
        python = ROOT / "venv" / "Scripts" / "python.exe"
        if not python.exists():
            python = Path(sys.executable)
        import subprocess
        bg_service = ROOT / "background_service.py"
        if bg_service.exists():
            subprocess.Popen(
                [str(python), str(bg_service), "--service", "telegram"],
                cwd=str(ROOT),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            return {
                "status": "started",
                "message": "Đã khởi động Bot V2 và kích hoạt xử lý hàng chờ!"
            }

    return {
        "status": "processing",
        "message": "Đã gửi lệnh xử lý video trong hàng chờ V2!"
    }


@app.post("/api/queue/clear")
async def api_clear_queue():
    """Xóa sạch hàng chờ V2."""
    for clear_flag in [WORKSPACE / "telegram_queue_clear.flag"]:
        try:
            clear_flag.parent.mkdir(parents=True, exist_ok=True)
            clear_flag.write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
    for q_file in [WORKSPACE / "telegram_queue.json"]:
        try:
            if q_file.is_file():
                q_file.write_text(json.dumps({"items": [], "updated_at": time.time()}), encoding="utf-8")
        except Exception:
            pass
    return {"status": "cleared", "message": "Đã xóa toàn bộ hàng chờ V2."}


@app.post("/api/queue/move")
async def api_move_queue_item(request: Request):
    """Thay đổi thứ tự ưu tiên của video trong hàng chờ V2 (đẩy lên / xuống / lên đầu)."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Dữ liệu JSON không hợp lệ")

    index = payload.get("index")
    action = payload.get("action", "up")  # 'up', 'down', 'top'

    if index is None or not isinstance(index, int):
        raise HTTPException(400, "Vị trí index không hợp lệ")

    db_path = _resolve_queue_db()
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, dedupe, payload, updated, error, source_fingerprint, checkpoint_stage, retry_count "
                    "FROM jobs WHERE state = 'queued' ORDER BY id ASC"
                ).fetchall()
                n = len(rows)
                if n <= 1 or index < 0 or index >= n:
                    q_data = read_queue()
                    return {"status": "unchanged", "items": q_data.get("items", []), "message": "Không thể thay đổi vị trí"}

                row_list = [dict(r) for r in rows]
                target_ids = [r["id"] for r in row_list]

                moved_name = ""
                try:
                    p = json.loads(row_list[index]["payload"])
                    moved_name = str(p.get("filename") or p.get("url") or "")
                except Exception:
                    pass
                if not moved_name:
                    moved_name = f"Video #{row_list[index]['id']}"
                if len(moved_name) > 35:
                    moved_name = moved_name[:35] + "..."

                if action == "up" and index > 0:
                    row_list[index - 1], row_list[index] = row_list[index], row_list[index - 1]
                    msg = f"Đã đẩy video #{index + 1} lên vị trí #{index}!"
                elif action == "top" and index > 0:
                    target = row_list.pop(index)
                    row_list.insert(0, target)
                    msg = "Đã đưa video lên đầu hàng chờ (#1)!"
                elif action == "down" and index < n - 1:
                    row_list[index], row_list[index + 1] = row_list[index + 1], row_list[index]
                    msg = f"Đã chuyển video #{index + 1} xuống vị trí #{index + 2}!"
                else:
                    q_data = read_queue()
                    return {"status": "unchanged", "items": q_data.get("items", []), "message": "Vị trí đã ở giới hạn"}

                with conn:
                    now = time.time()
                    for tid in target_ids:
                        conn.execute(
                            "UPDATE jobs SET dedupe = ? WHERE id = ?",
                            (f"__reorder_tmp_{tid}_{now}", tid)
                        )
                    for new_data, tid in zip(row_list, target_ids):
                        conn.execute(
                            "UPDATE jobs SET dedupe = ?, payload = ?, source_fingerprint = ?, "
                            "checkpoint_stage = ?, retry_count = ?, updated = ? WHERE id = ?",
                            (
                                new_data["dedupe"],
                                new_data["payload"],
                                new_data.get("source_fingerprint"),
                                new_data.get("checkpoint_stage"),
                                new_data.get("retry_count", 0),
                                now,
                                tid
                            )
                        )
                q_data = read_queue()
                return {"status": "success", "items": q_data.get("items", []), "message": msg}
            finally:
                conn.close()
        except Exception as e:
            print(f"Lỗi api_move_queue_item (SQLite): {e}", flush=True)
            raise HTTPException(500, f"Lỗi di chuyển video V2: {str(e)}")

    for q_candidate in [WORKSPACE / "telegram_queue.json"]:
        if q_candidate.is_file():
            try:
                data = json.loads(q_candidate.read_text(encoding="utf-8"))
                items = data.get("items", [])
                n = len(items)
                if n <= 1 or index < 0 or index >= n:
                    return {"status": "unchanged", "items": items, "message": "Không thể thay đổi vị trí"}

                if action == "up" and index > 0:
                    items[index - 1], items[index] = items[index], items[index - 1]
                    msg = f"Đã đẩy video #{index + 1} lên vị trí #{index}!"
                elif action == "top" and index > 0:
                    target = items.pop(index)
                    items.insert(0, target)
                    msg = "Đã đưa video lên đầu hàng chờ (#1)!"
                elif action == "down" and index < n - 1:
                    items[index], items[index + 1] = items[index + 1], items[index]
                    msg = f"Đã chuyển video #{index + 1} xuống vị trí #{index + 2}!"
                else:
                    return {"status": "unchanged", "items": items, "message": "Vị trí đã ở giới hạn"}

                for i, it in enumerate(items, 1):
                    it["position"] = i
                data["items"] = items
                data["updated_at"] = time.time()
                q_candidate.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return {"status": "success", "items": items, "message": msg}
            except Exception as e:
                raise HTTPException(500, f"Lỗi di chuyển video JSON: {str(e)}")

    return {"status": "unchanged", "items": [], "message": "Hàng chờ trống"}


@app.post("/api/queue/delete")
async def api_delete_queue_item(request: Request):
    """Xóa một video khỏi hàng chờ V2 theo index."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Dữ liệu JSON không hợp lệ")

    index = payload.get("index")
    if index is None or not isinstance(index, int):
        raise HTTPException(400, "Vị trí index không hợp lệ")

    db_path = _resolve_queue_db()
    if db_path.is_file():
        try:
            import sqlite3
            conn = sqlite3.connect(db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, payload FROM jobs WHERE state = 'queued' ORDER BY id ASC"
                ).fetchall()
                n = len(rows)
                if index < 0 or index >= n:
                    q_data = read_queue()
                    return {"status": "unchanged", "items": q_data.get("items", []), "message": "Không tìm thấy video cần xóa"}

                target_row = rows[index]
                target_id = target_row["id"]
                removed_name = ""
                try:
                    p = json.loads(target_row["payload"])
                    removed_name = str(p.get("filename") or p.get("url") or "")
                except Exception:
                    pass
                if not removed_name:
                    removed_name = f"Video #{target_id}"
                if len(removed_name) > 35:
                    removed_name = removed_name[:35] + "..."

                with conn:
                    conn.execute(
                        "UPDATE jobs SET state = 'cancelled', error = 'deleted_by_user', updated = ? WHERE id = ?",
                        (time.time(), target_id)
                    )

                q_data = read_queue()
                return {
                    "status": "success",
                    "items": q_data.get("items", []),
                    "message": f'Đã xóa video "{removed_name}" khỏi hàng chờ!'
                }
            finally:
                conn.close()
        except Exception as e:
            print(f"Lỗi api_delete_queue_item (SQLite): {e}", flush=True)
            raise HTTPException(500, f"Lỗi xóa video V2: {str(e)}")

    for q_candidate in [WORKSPACE / "telegram_queue.json"]:
        if q_candidate.is_file():
            try:
                data = json.loads(q_candidate.read_text(encoding="utf-8"))
                items = data.get("items", [])
                n = len(items)
                if index < 0 or index >= n:
                    return {"status": "unchanged", "items": items, "message": "Không tìm thấy video cần xóa"}
                removed = items.pop(index)
                removed_name = removed.get("name", "video")
                if len(removed_name) > 35:
                    removed_name = removed_name[:35] + "..."
                for i, it in enumerate(items, 1):
                    it["position"] = i
                data["items"] = items
                data["updated_at"] = time.time()
                q_candidate.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return {
                    "status": "success",
                    "items": items,
                    "message": f'Đã xóa video "{removed_name}" khỏi hàng chờ!'
                }
            except Exception as e:
                raise HTTPException(500, f"Lỗi xóa video JSON: {str(e)}")

    return {"status": "unchanged", "items": [], "message": "Hàng chờ trống"}

@app.get("/", response_class=HTMLResponse)
def dashboard():
    page = TEMPLATE.read_text(encoding="utf-8")
    input_dir = get_input_dir()
    # Replace paths separately in HTML and JavaScript so backslashes remain valid.
    head, script = page.split("<script>", 1)
    head = head.replace(r"D:\video phôi", html.escape(str(input_dir))).replace(r"D:\video tool v2", html.escape(str(OUTPUT)))
    script = script.replace(r"D:\\video phôi", str(input_dir).replace("\\", "\\\\")).replace(r"D:\\video tool v2", str(OUTPUT).replace("\\", "\\\\"))
    return head + "<script>" + script

from workflow_api import router as workflow_router
app.include_router(workflow_router)

try:
    from script_api import router as script_router
    app.include_router(script_router)
    print("[Studio Kich Ban] script_router mounted successfully on V2.")
except Exception as _se:
    print(f"[Studio Kich Ban] Failed to mount script_router on V2: {_se}")

try:
    from phase_a_routes import router as phase_a_router
    app.include_router(phase_a_router)
    print("[Phase A] phase_a_router mounted successfully on V2.")
except Exception as _pe:
    print(f"[Phase A] Failed to mount phase_a_router on V2: {_pe}")

try:
    from phase_b_routes import router as phase_b_router
    app.include_router(phase_b_router)
    print("[Phase B] phase_b_router mounted successfully on V2.")
except Exception as _pbe:
    print(f"[Phase B] Failed to mount phase_b_router on V2: {_pbe}")

try:
    from phase_c_routes import router as phase_c_router
    app.include_router(phase_c_router)
    print("[Phase C] phase_c_router mounted successfully on V2.")
except Exception as _pce:
    print(f"[Phase C] Failed to mount phase_c_router on V2: {_pce}")

try:
    from phase_d_routes import router as phase_d_router
    app.include_router(phase_d_router)
    print("[Phase D] phase_d_router mounted successfully on V2.")
except Exception as _pde:
    print(f"[Phase D] Failed to mount phase_d_router on V2: {_pde}")

try:
    from watermark_api import router as watermark_router
    app.include_router(watermark_router)
    print("[Watermark Remover] watermark_router mounted successfully on V2.")
except Exception as _wme:
    print(f"[Watermark Remover] Failed to mount watermark_router on V2: {_wme}")

try:
    from scene_composer_api import router as scene_composer_router
    app.include_router(scene_composer_router)
    print("[Scene Composer] scene_composer_router mounted successfully on V2.")
except Exception as _sce:
    print(f"[Scene Composer] Failed to mount scene_composer_router on V2: {_sce}")



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8089)
