# -*- coding: utf-8 -*-
"""F13 — Trung tâm thông báo & cảnh báo của Dashboard (Tool V1).

Ring-buffer 100 mục trong workspace/control/notifications.json (atomic write, tmp theo PID).
Hook từ job_tracker (video lỗi / hoàn tất / batch kết thúc) và worker_settings (VRAM Guard).
Tùy chọn đẩy Telegram qua HTTP (mặc định TẮT) — không sửa telegram_bot.py.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("notification_service")

ROOT = Path(__file__).resolve().parent
WORKSPACE = Path(os.getenv("AUTODUB_WORKSPACE", str(ROOT.parent / "workspace")))
CONTROL_DIR = WORKSPACE / "control"
NOTIF_FILE = CONTROL_DIR / "notifications.json"
SETTINGS_FILE = CONTROL_DIR / "notification_settings.json"
MAX_NOTIFICATIONS = 100
LEVELS = ("info", "success", "warning", "error")


def _load_list() -> List[Dict[str, Any]]:
    try:
        if NOTIF_FILE.is_file():
            data = json.loads(NOTIF_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
    except Exception as exc:
        logger.warning("notifications.json hỏng, tạo lại: %s", exc)
    return []


def _save_list(items: List[Dict[str, Any]]) -> None:
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = NOTIF_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(items[:MAX_NOTIFICATIONS], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(NOTIF_FILE)
    except Exception as exc:
        logger.error("Không thể ghi notifications.json: %s", exc)


def get_settings() -> Dict[str, Any]:
    try:
        if SETTINGS_FILE.is_file():
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {"telegram_enabled": False, "chat_id": ""}


def save_settings(telegram_enabled: bool, chat_id: str = "") -> Dict[str, Any]:
    data = {"telegram_enabled": bool(telegram_enabled), "chat_id": str(chat_id or "").strip()}
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception as exc:
        logger.error("Không thể ghi notification_settings.json: %s", exc)
    return data


def _send_telegram(level: str, title: str, body: str) -> None:
    """Đẩy thông báo qua Telegram Bot API (fire-and-forget, cần bật trong settings)."""
    cfg = get_settings()
    if not cfg.get("telegram_enabled") or not cfg.get("chat_id"):
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    try:
        import urllib.parse
        import urllib.request

        emoji = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "🔴"}.get(level, "ℹ️")
        text = f"{emoji} [Tool V1] {title}" + (f"\n{body}" if body else "")
        url = (
            "https://api.telegram.org/bot" + token
            + "/sendMessage?chat_id=" + urllib.parse.quote(str(cfg["chat_id"]))
            + "&text=" + urllib.parse.quote(text[:4000])
        )
        with urllib.request.urlopen(url, timeout=8):
            pass
    except Exception as exc:
        logger.debug("Telegram push thất bại: %s", exc)


def push(level: str, title: str, body: str = "", source: str = "") -> Optional[Dict[str, Any]]:
    """Thêm một thông báo mới nhất lên đầu ring-buffer (an toàn khi gọi từ bất kỳ tiến trình nào)."""
    level = level if level in LEVELS else "info"
    item = {
        "id": f"n_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}",
        "ts": time.time(),
        "level": level,
        "title": str(title or "")[:200],
        "body": str(body or "")[:1000],
        "source": str(source or "")[:80],
        "read": False,
    }
    items = _load_list()
    items.insert(0, item)
    _save_list(items)
    try:
        import threading

        threading.Thread(target=_send_telegram, args=(level, item["title"], item["body"]), daemon=True).start()
    except Exception:
        pass
    return item


def list_notifications(unread_only: bool = False, limit: int = 50) -> List[Dict[str, Any]]:
    items = _load_list()
    if unread_only:
        items = [x for x in items if not x.get("read")]
    return items[: max(1, min(int(limit), MAX_NOTIFICATIONS))]


def unread_count() -> int:
    return sum(1 for x in _load_list() if not x.get("read"))


def mark_read(ids: Optional[List[str]] = None, mark_all: bool = False) -> int:
    items = _load_list()
    wanted = set(ids or [])
    changed = 0
    for x in items:
        if mark_all or x.get("id") in wanted:
            if not x.get("read"):
                x["read"] = True
                changed += 1
    if changed:
        _save_list(items)
    return changed
