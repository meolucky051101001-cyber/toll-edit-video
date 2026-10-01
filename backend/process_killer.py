"""
Process killer utility for Antigravity Video Dubbing Tool.
Safely terminates heavy background worker subprocesses (ffmpeg, ffprobe, yt-dlp, model workers)
without killing the host servers (main.py, telegram_bot.py, background_service.py).
"""
import os
import logging
import psutil

logger = logging.getLogger("process_killer")

PROTECTED_SCRIPTS = {
    "main.py",
    "telegram_bot.py",
    "dashboard_monitor.py",
    "background_service.py",
}

WORKER_PROCESS_NAMES = {
    "ffmpeg.exe",
    "ffprobe.exe",
    "yt-dlp.exe",
}

WORKER_SCRIPT_KEYWORDS = [
    "separator_worker",
    "v1_separator_worker",
    "demucs",
]

def terminate_worker_processes() -> list:
    """
    Terminates worker subprocesses across the tool.
    Returns list of terminated PIDs.
    """
    killed = []
    current_pid = os.getpid()

    # 1. Kill all direct descendants of current process
    try:
        current_proc = psutil.Process(current_pid)
        for child in current_proc.children(recursive=True):
            try:
                child.kill()
                killed.append(child.pid)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Scan system processes for orphan/sibling worker processes
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = p.info['pid']
            if pid == current_pid or pid in killed:
                continue

            name = (p.info['name'] or '').lower()
            cmdline_list = p.info['cmdline'] or []
            cmdline = ' '.join(cmdline_list).lower()

            # Skip protected server processes
            if any(prot in cmdline for prot in PROTECTED_SCRIPTS):
                continue

            # Kill ffmpeg / ffprobe / yt-dlp associated with tool
            if name in WORKER_PROCESS_NAMES:
                if any(w in cmdline for w in ['tool v1', 'tool v2', 'workspace', 'banve', 'v1-ocr', 'bs_roformer', 'temp']):
                    try:
                        p.kill()
                        killed.append(pid)
                        logger.info(f"Terminated worker process {name} (PID: {pid})")
                    except Exception:
                        pass

            # Kill python model workers
            elif 'python' in name:
                if any(kw in cmdline for kw in WORKER_SCRIPT_KEYWORDS):
                    try:
                        p.kill()
                        killed.append(pid)
                        logger.info(f"Terminated python worker (PID: {pid}): {cmdline[:80]}")
                    except Exception:
                        pass
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except Exception as e:
            logger.debug(f"Error checking process {p}: {e}")

    return killed

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    killed = terminate_worker_processes()
    print(f"Killed worker processes: {killed}")
