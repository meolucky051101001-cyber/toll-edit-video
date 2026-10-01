"""
failed_video_queue_monitor.py - Giám sát video thất bại và tự động đưa vào cuối hàng chờ để edit lại.
Hoạt động liên tục trong nền:
1. Theo dõi log thời gian thực từ telegram.log
2. Phát hiện bất kỳ video nào gặp lỗi (Error processing ...)
3. Tự động kiểm tra và thêm video đó vào cuối hàng chờ (telegram_queue.json)
4. Kích hoạt bot (telegram_queue_trigger.flag) để nạp vào hàng chờ sau cùng
5. Ghi nhận nhật ký giám sát vào failed_videos_monitor.json
"""

import os
import sys
import time
import json
import re
import logging
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
BOT_SYSTEM = WORKSPACE / "bot_system"
LOG_DIR = BOT_SYSTEM / "service_logs"
TELEGRAM_LOG = LOG_DIR / "telegram.log"
QUEUE_FILE = BOT_SYSTEM / "telegram_queue.json"
TRIGGER_FLAG = BOT_SYSTEM / "telegram_queue_trigger.flag"
MONITOR_STATE_FILE = BOT_SYSTEM / "failed_videos_monitor.json"

MAX_RETRIES = 2  # Số lần thử lại tối đa cho mỗi video thất bại

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [FAILED_MONITOR] %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(BOT_SYSTEM / "service_logs" / "failed_video_monitor.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("failed_monitor")


def load_monitor_state() -> dict:
    if MONITOR_STATE_FILE.is_file():
        try:
            return json.loads(MONITOR_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"failures": {}, "handled_offsets": 0}


def save_monitor_state(state: dict):
    try:
        tmp = MONITOR_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, MONITOR_STATE_FILE)
    except Exception as e:
        logger.error(f"Lỗi lưu monitor state: {e}")


def load_queue() -> dict:
    if QUEUE_FILE.is_file():
        try:
            return json.loads(QUEUE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"items": []}


def save_queue_and_trigger(qdata: dict):
    try:
        QUEUE_FILE.write_text(json.dumps(qdata, indent=2, ensure_ascii=False), encoding="utf-8")
        TRIGGER_FLAG.write_text("trigger", encoding="utf-8")
        logger.info("Đã tạo flag kích hoạt telegram_queue_trigger.flag thành công.")
    except Exception as e:
        logger.error(f"Lỗi ghi queue/trigger: {e}")


def requeue_failed_video(target: str, error_msg: str, chat_id: int = 8393150305, voice_mode: str = "auto") -> bool:
    """Đưa video thất bại vào vị trí sau cùng của hàng chờ."""
    state = load_monitor_state()
    failures = state.setdefault("failures", {})

    record = failures.get(target, {
        "target": target,
        "retry_count": 0,
        "history": []
    })

    current_retries = record.get("retry_count", 0)
    if current_retries >= MAX_RETRIES:
        logger.warning(f"Video {target} đã thử lại tối đa {MAX_RETRIES} lần. Không đưa vào hàng chờ nữa.")
        record["status"] = "max_retries_exceeded"
        failures[target] = record
        save_monitor_state(state)
        return False

    qdata = load_queue()
    items = qdata.get("items", [])

    # Kiểm tra xem video đã có sẵn trong hàng chờ chưa
    for it in items:
        if it.get("url") == target or it.get("name") == target:
            logger.info(f"Video {target} hiện đã có trong hàng chờ ở vị trí {it.get('position')}.")
            return False

    new_pos = len(items) + 1
    new_item = {
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "name": target,
        "position": new_pos,
        "source": "Telegram (Auto-Recovery)",
        "telegram_position": new_pos + 10,
        "type": "url" if target.startswith("http") else "video",
        "url": target if target.startswith("http") else "",
        "voice_mode": voice_mode,
        "retry_count": current_retries + 1
    }

    items.append(new_item)
    qdata["items"] = items
    save_queue_and_trigger(qdata)

    # Cập nhật nhật ký
    record["retry_count"] = current_retries + 1
    record["status"] = "requeued_at_end"
    record["last_error"] = error_msg[:500]
    record["last_requeued_at"] = time.time()
    record["history"].append({
        "time": datetime.now().isoformat(),
        "error": error_msg[:300],
        "requeued_position": new_pos,
        "attempt": current_retries + 1
    })
    failures[target] = record
    save_monitor_state(state)

    logger.info(f"🔄 ĐÃ TỰ ĐỘNG ĐƯA VÀO HÀNG CHỜ SAU CÙNG: {target} (Vị trí {new_pos}, lần thử {current_retries + 1}/{MAX_RETRIES})")
    return True


def monitor_loop():
    logger.info("Bắt đầu tiến trình giám sát video thất bại của Tool V1...")
    state = load_monitor_state()

    # Bắt đầu đọc từ vị trí hiện tại của file log nếu file tồn tại
    last_pos = 0
    if TELEGRAM_LOG.is_file():
        file_size = TELEGRAM_LOG.stat().st_size
        # Nếu mới khởi động, quét ngược 50KB gần nhất để không bỏ sót lỗi vừa xảy ra
        last_pos = max(0, file_size - 50000)

    url_error_pattern = re.compile(r"ERROR - Error processing (https?://\S+):\s*(.+)")
    vid_error_pattern = re.compile(r"ERROR - Error processing video:\s*(.+)")

    while True:
        try:
            if not TELEGRAM_LOG.is_file():
                time.sleep(2)
                continue

            current_size = TELEGRAM_LOG.stat().st_size
            if current_size < last_pos:
                # Log đã bị rotate
                last_pos = 0

            if current_size > last_pos:
                with open(TELEGRAM_LOG, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(last_pos)
                    lines = f.readlines()
                    last_pos = f.tell()

                for line in lines:
                    m_url = url_error_pattern.search(line)
                    if m_url:
                        failed_url = m_url.group(1).strip()
                        err_msg = m_url.group(2).strip()
                        logger.warning(f"Phát hiện video URL thất bại: {failed_url} -> {err_msg[:100]}")
                        requeue_failed_video(failed_url, err_msg)
                        continue

                    m_vid = vid_error_pattern.search(line)
                    if m_vid:
                        err_msg = m_vid.group(1).strip()
                        logger.warning(f"Phát hiện video upload thất bại: {err_msg[:100]}")
                        # Đối với video direct upload, sẽ theo dõi thêm
                        continue

        except Exception as e:
            logger.error(f"Lỗi trong monitor loop: {e}", exc_info=True)

        time.sleep(2.0)


if __name__ == "__main__":
    monitor_loop()
