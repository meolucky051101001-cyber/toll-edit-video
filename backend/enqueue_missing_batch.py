# -*- coding: utf-8 -*-
import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

queue_file = Path(r"C:\tool v1\workspace\bot_system\telegram_queue.json")
chat_id = 8393150305

items = [
    {
        "position": 1,
        "telegram_position": 6,
        "name": "http://xhslink.com/o/54UdlcJtSrv",
        "url": "http://xhslink.com/o/54UdlcJtSrv",
        "type": "url",
        "source": "Telegram",
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "voice_mode": "auto"
    },
    {
        "position": 2,
        "telegram_position": 7,
        "name": "https://v.douyin.com/HERZHNx2P8U/",
        "url": "https://v.douyin.com/HERZHNx2P8U/",
        "type": "url",
        "source": "Telegram",
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "voice_mode": "auto"
    },
    {
        "position": 3,
        "telegram_position": 8,
        "name": "https://v.douyin.com/A4K2rz74dVg/",
        "url": "https://v.douyin.com/A4K2rz74dVg/",
        "type": "url",
        "source": "Telegram",
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "voice_mode": "auto"
    },
    {
        "position": 4,
        "telegram_position": 9,
        "name": "https://v.douyin.com/CbtpWIqfC6k/",
        "url": "https://v.douyin.com/CbtpWIqfC6k/",
        "type": "url",
        "source": "Telegram",
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "voice_mode": "auto"
    },
    {
        "position": 5,
        "telegram_position": 10,
        "name": "https://v.douyin.com/JZC5CP_8rMk/",
        "url": "https://v.douyin.com/JZC5CP_8rMk/",
        "type": "url",
        "source": "Telegram",
        "chat_id": chat_id,
        "file_id": "",
        "filename": "",
        "voice_mode": "auto"
    }
]

data = {
    "items": items,
    "pid": 0,
    "updated_at": time.time()
}

queue_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Đã ghi 5 video còn thiếu vào {queue_file}")

# Clear any stop flags
sys.path.insert(0, r"C:\tool v1\backend")
import job_tracker
import shared_state
job_tracker.clear_stop_request()
shared_state.stop_requested = False
print("Đã xóa mọi cờ stop, hệ thống sẵn sàng xử lý hàng đợi!")
