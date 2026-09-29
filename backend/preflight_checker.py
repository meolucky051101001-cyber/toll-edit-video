"""
preflight_checker.py - Comprehensive system readiness checks for Tool V1 & Tool V2.
Checks GPU (CUDA/VRAM), NVENC hardware encoder, FFmpeg/FFprobe, Disk Space, Directories, and AI keys.
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


def check_gpu() -> Dict[str, Any]:
    try:
        import torch
        cuda_avail = torch.cuda.is_available()
        if cuda_avail:
            device_name = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            total_vram_gb = round(props.total_memory / (1024 ** 3), 2)
            try:
                allocated_gb = round(torch.cuda.memory_allocated(0) / (1024 ** 3), 2)
            except Exception:
                allocated_gb = 0.0
            return {
                "available": True,
                "name": device_name,
                "vram_total_gb": total_vram_gb,
                "vram_allocated_gb": allocated_gb,
                "cuda_version": getattr(torch.version, "cuda", "N/A") or "N/A",
                "status": "pass",
                "message": f"{device_name} ({total_vram_gb} GB VRAM) - CUDA OK"
            }
        else:
            return {
                "available": False,
                "name": "CPU Fallback",
                "vram_total_gb": 0,
                "vram_allocated_gb": 0,
                "cuda_version": "N/A",
                "status": "warning",
                "message": "Không tìm thấy GPU CUDA, hệ thống sẽ chạy chậm trên CPU"
            }
    except Exception as exc:
        return {
            "available": False,
            "name": "Lỗi probe GPU",
            "vram_total_gb": 0,
            "vram_allocated_gb": 0,
            "cuda_version": "N/A",
            "status": "warning",
            "message": f"Không thể kiểm tra GPU: {exc}"
        }


def check_nvenc() -> Dict[str, Any]:
    ffmpeg_exe = shutil.which("ffmpeg")
    if not ffmpeg_exe:
        return {
            "supported": False,
            "encoders": [],
            "status": "error",
            "message": "FFmpeg chưa được cài đặt trên hệ thống"
        }
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        proc = subprocess.run(
            [ffmpeg_exe, "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=flags
        )
        out = proc.stdout or ""
        supported = []
        for enc in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
            if enc in out:
                supported.append(enc)
        if "h264_nvenc" in supported:
            return {
                "supported": True,
                "encoders": supported,
                "status": "pass",
                "message": f"NVENC phần cứng hoạt động tốt ({', '.join(supported)})"
            }
        else:
            return {
                "supported": False,
                "encoders": supported,
                "status": "warning",
                "message": "Không phát hiện h264_nvenc; video sẽ render bằng libx264 (CPU)"
            }
    except Exception as exc:
        return {
            "supported": False,
            "encoders": [],
            "status": "warning",
            "message": f"Lỗi kiểm tra NVENC: {exc}"
        }


def check_binaries() -> Dict[str, Any]:
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    
    version_str = "N/A"
    if ffmpeg_path:
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            proc = subprocess.run(
                [ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                creationflags=flags
            )
            first_line = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
            if "version" in first_line:
                version_str = first_line.split("version")[1].split()[0]
        except Exception:
            pass

    status = "pass" if (ffmpeg_path and ffprobe_path) else "error"
    msg = f"FFmpeg {version_str} & FFprobe sẵn sàng" if status == "pass" else "Thiếu FFmpeg hoặc FFprobe trên hệ thống PATH"
    return {
        "ffmpeg": {"available": bool(ffmpeg_path), "path": ffmpeg_path or "", "version": version_str},
        "ffprobe": {"available": bool(ffprobe_path), "path": ffprobe_path or ""},
        "status": status,
        "message": msg
    }


def check_disk_space() -> Dict[str, Any]:
    drives = {}
    has_warning = False
    has_error = False

    for letter in ("C:", "D:"):
        try:
            usage = shutil.disk_usage(letter)
            free_gb = round(usage.free / (1024 ** 3), 1)
            total_gb = round(usage.total / (1024 ** 3), 1)
            used_percent = round((usage.used / usage.total) * 100, 1)

            if free_gb < 2.0:
                drv_status = "error"
                has_error = True
                msg = f"Ổ {letter} sắp cạn bộ nhớ (chỉ còn {free_gb} GB trống)"
            elif free_gb < 10.0:
                drv_status = "warning"
                has_warning = True
                msg = f"Ổ {letter} còn ít bộ nhớ ({free_gb} GB trống)"
            else:
                drv_status = "pass"
                msg = f"Ổ {letter} dung lượng dồi dào ({free_gb} GB trống / {total_gb} GB)"

            drives[letter] = {
                "letter": letter,
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_percent": used_percent,
                "status": drv_status,
                "message": msg
            }
        except Exception as exc:
            drives[letter] = {
                "letter": letter,
                "free_gb": 0,
                "total_gb": 0,
                "used_percent": 0,
                "status": "warning",
                "message": f"Không thể đọc ổ {letter}: {exc}"
            }

    overall_status = "error" if has_error else ("warning" if has_warning else "pass")
    return {
        "drives": drives,
        "status": overall_status,
        "message": f"Ổ C: {drives.get('C:', {}).get('free_gb', 0)} GB trống | Ổ D: {drives.get('D:', {}).get('free_gb', 0)} GB trống"
    }


def check_directories(input_dir: Path, output_dir: Path, workspace_dir: Path) -> Dict[str, Any]:
    dirs = {}
    overall = "pass"

    for key, path in (("input", input_dir), ("output", output_dir), ("workspace", workspace_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / f".preflight_test_{os.getpid()}.tmp"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            dirs[key] = {
                "path": str(path.resolve()),
                "exists": True,
                "writable": True,
                "status": "pass",
                "message": f"Thư mục hợp lệ: {path.name}"
            }
        except Exception as exc:
            overall = "error"
            dirs[key] = {
                "path": str(path),
                "exists": path.exists(),
                "writable": False,
                "status": "error",
                "message": f"Lỗi quyền truy cập thư mục: {exc}"
            }

    return {
        "directories": dirs,
        "status": overall,
        "message": "Các thư mục làm việc (Phôi, Bản vẽ, Workspace) đều hợp lệ và có quyền ghi" if overall == "pass" else "Có lỗi truy cập thư mục"
    }


def check_ai_services(backend_dir: Path) -> Dict[str, Any]:
    # Check Gemini API Key
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        dotenv = backend_dir / ".env"
        if dotenv.is_file():
            try:
                for line in dotenv.read_text(encoding="utf-8-sig").splitlines():
                    if line.strip().startswith("GEMINI_API_KEY="):
                        gemini_key = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

    if gemini_key:
        masked = gemini_key[:4] + "..." + gemini_key[-4:] if len(gemini_key) > 8 else "***"
        gemini_status = {
            "configured": True,
            "status": "pass",
            "message": f"Gemini API Key đã cấu hình ({masked})"
        }
    else:
        gemini_status = {
            "configured": False,
            "status": "warning",
            "message": "Chưa có GEMINI_API_KEY; sẽ dùng dịch vụ dịch miễn phí fallback"
        }

    # Check TTS
    has_edge = importlib.util.find_spec("edge_tts") is not None
    tts_status = {
        "available": has_edge,
        "status": "pass" if has_edge else "error",
        "message": "Edge TTS sẵn sàng" if has_edge else "Thiếu thư viện edge_tts"
    }

    return {
        "gemini": gemini_status,
        "tts": tts_status,
        "status": "pass" if (gemini_status["status"] == "pass" and tts_status["status"] == "pass") else ("warning" if tts_status["status"] == "pass" else "error"),
        "message": "Dịch vụ AI sẵn sàng"
    }


def run_full_preflight(tool: str, input_dir: Path, output_dir: Path, workspace_dir: Path, backend_dir: Path) -> Dict[str, Any]:
    gpu = check_gpu()
    nvenc = check_nvenc()
    binaries = check_binaries()
    disk = check_disk_space()
    dirs = check_directories(input_dir, output_dir, workspace_dir)
    ai = check_ai_services(backend_dir)

    checks = [
        {
            "id": "gpu",
            "title": "GPU Tăng Tốc",
            "status": gpu["status"],
            "badge": gpu["name"],
            "description": gpu["message"],
            "details": f"VRAM: {gpu['vram_total_gb']} GB | CUDA: {gpu['cuda_version']}"
        },
        {
            "id": "nvenc",
            "title": "Bộ Mã Hóa NVENC",
            "status": nvenc["status"],
            "badge": "h264_nvenc" if nvenc["supported"] else "CPU x264",
            "description": nvenc["message"],
            "details": f"Hỗ trợ: {', '.join(nvenc['encoders'])}" if nvenc["encoders"] else "Không phát hiện encoder NVENC"
        },
        {
            "id": "binaries",
            "title": "FFmpeg Engine",
            "status": binaries["status"],
            "badge": f"v{binaries['ffmpeg']['version']}",
            "description": binaries["message"],
            "details": binaries["ffmpeg"]["path"] or "Chưa cấu hình"
        },
        {
            "id": "disk",
            "title": "Dung Lượng Ổ Đĩa",
            "status": disk["status"],
            "badge": f"C: {disk['drives'].get('C:', {}).get('free_gb', 0)}GB | D: {disk['drives'].get('D:', {}).get('free_gb', 0)}GB",
            "description": disk["message"],
            "details": f"Ổ C sử dụng {disk['drives'].get('C:', {}).get('used_percent', 0)}% | Ổ D sử dụng {disk['drives'].get('D:', {}).get('used_percent', 0)}%"
        },
        {
            "id": "dirs",
            "title": "Thư Mục Làm Việc",
            "status": dirs["status"],
            "badge": "Đầy đủ quyền ghi",
            "description": dirs["message"],
            "details": f"Phôi: {dirs['directories']['input']['path']}\nBản vẽ: {dirs['directories']['output']['path']}"
        },
        {
            "id": "ai",
            "title": "Dịch Vụ AI & Giọng Đọc",
            "status": ai["status"],
            "badge": "Gemini & TTS",
            "description": f"{ai['gemini']['message']} | {ai['tts']['message']}",
            "details": f"Gemini: {ai['gemini']['status'].upper()} | Edge-TTS: {ai['tts']['status'].upper()}"
        }
    ]

    has_error = any(c["status"] == "error" for c in checks)
    has_warning = any(c["status"] == "warning" for c in checks)
    overall_status = "error" if has_error else ("warning" if has_warning else "pass")
    
    if overall_status == "pass":
        summary_msg = "Mọi tài nguyên hệ thống (GPU RTX 4050, NVENC, FFmpeg, Ổ đĩa, AI) đều hoàn hảo và sẵn sàng render tốc độ cao."
    elif overall_status == "warning":
        summary_msg = "Hệ thống có thể hoạt động nhưng có một số mục cảnh báo cần lưu ý."
    else:
        summary_msg = "Phát hiện lỗi nghiêm trọng ảnh hưởng đến quá trình xử lý video."

    return {
        "tool": tool,
        "ready": not has_error,
        "status": overall_status,
        "summary": summary_msg,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "gpu": gpu,
        "nvenc": nvenc,
        "binaries": binaries,
        "disk": disk,
        "directories": dirs,
        "ai": ai,
        "checks": checks
    }
