"""
cloud_telegram_bot.py - Dedicated Telegram Bot cho Tool V1 Cloud GPU.

Hoạt động song song và hoàn toàn độc lập với bot Local (backend/telegram_bot.py):
- Nhận trực tiếp link video (Douyin, TikTok, Xiaohongshu, YouTube, Facebook...) hoặc file video gửi/forward.
- Tự động khóa quyền sử dụng (auto-pair) cho tài khoản Telegram đầu tiên nhắn tin để bảo vệ quỹ CU.
- Kiểm tra hạn mức CU hôm nay trước khi tải/cấp phát GPU; đồng bộ trực tiếp vào CloudJobStore (hiển thị live trên Dashboard 8089).
- Xuất video thành phẩm vào D:\\ToolV1_Cloud (hoặc AUTODUB_CLOUD_OUTPUT_DIR) và gửi trả lại ngay trên Telegram.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import glob
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

from .cloud_config import BACKEND_DIR, WORKSPACE_DIR, get_cloud_output_dir
from .cloud_contract import (
    admit_and_enqueue_job,
    get_cloud_status_snapshot,
    get_error_remediation,
    probe_video_metadata,
    request_job_cancellation,
    run_provider_action_nonblocking,
)
from .cloud_job_store import CloudJobStore
from .cloud_orchestrator import CloudOrchestrator
from .cloud_security import (
    ALLOWED_VOICES,
    CloudValidationError,
    get_default_allowed_roots,
    redact_secrets,
    resolve_cloud_bot_token,
    validate_gpu_type,
    validate_voice,
)
from .cloud_worker import CloudWorker
from .colab_bridge import (
    ColabProviderAdapter,
    abort_session_processes,
    stop_all_sessions,
)

# Ngăn httpx/httpcore in URL chứa Bot Token ở mức INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("cloud.telegram_bot")

TELEGRAM_BOT_DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MB (giới hạn Bot API chuẩn)
TELEGRAM_SEND_MAX_BYTES = int(49.5 * 1024 * 1024)
DISALLOWED_PIPELINE_TPUS = frozenset({"V5E1", "V6E1"})

VOICE_ALIASES: Dict[str, str] = {
    "auto": "auto",
    "tudong": "auto",
    "tựđộng": "auto",
    "v1": "auto",
    "mai": "BV562_streaming",
    "nu": "BV562_streaming",
    "nữ": "BV562_streaming",
    "bv562": "BV562_streaming",
    "bv562_streaming": "BV562_streaming",
    "nam": "BV075_streaming",
    "bv075": "BV075_streaming",
    "bv075_streaming": "BV075_streaming",
    "tuoivui": "BV421_vivn_streaming",
    "vui": "BV421_vivn_streaming",
    "bv421": "BV421_vivn_streaming",
    "bv421_vivn_streaming": "BV421_vivn_streaming",
    "hoaimy": "vi-VN-HoaiMyNeural",
    "vi-vn-hoaimyneural": "vi-VN-HoaiMyNeural",
    "namminh": "vi-VN-NamMinhNeural",
    "vi-vn-namminhneural": "vi-VN-NamMinhNeural",
}

VOICE_LABELS: Dict[str, str] = {
    "auto": "Tự động Nam/Nữ chuẩn V1 (Mai / Thanh Niên)",
    "BV562_streaming": "CapCut · Mai (Nữ chuẩn V1)",
    "BV075_streaming": "CapCut · Thanh Niên Tự Tin (Nam)",
    "BV421_vivn_streaming": "CapCut · Cô Gái Tươi Vui",
    "vi-VN-HoaiMyNeural": "Edge · Hoài My (Nữ)",
    "vi-VN-NamMinhNeural": "Edge · Nam Minh (Nam)",
}


def extract_video_urls(text: str) -> List[str]:
    """Trích xuất danh sách URL http/https sạch, không trùng lặp từ tin nhắn."""
    if not text:
        return []
    try:
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))
        from url_utils import extract_http_urls

        return list(extract_http_urls(text))
    except Exception:
        raw_matches = re.findall(r'https?://[^\s<>"]+', text)
        cleaned_list: List[str] = []
        for item in raw_matches:
            cleaned = re.sub(r'[\s.,;:!?\)>"\']+$', "", item.strip())
            if cleaned.startswith(("http://", "https://")) and cleaned not in cleaned_list:
                cleaned_list.append(cleaned)
        return cleaned_list


def resolve_voice_choice(raw_value: str) -> str:
    """Chấp nhận cả tên gợi nhớ (auto, mai, nam, tuoivui, hoaimy, namminh) lẫn mã chuẩn."""
    cleaned = str(raw_value or "").strip()
    mapped = VOICE_ALIASES.get(cleaned.lower(), cleaned)
    if mapped not in ALLOWED_VOICES:
        raise CloudValidationError(
            "Giọng đọc không hợp lệ. Chọn: `auto`, `mai`, `nam`, `tuoivui`, `hoaimy`, `namminh`.",
            code="UNSUPPORTED_VOICE",
        )
    return validate_voice(mapped)


def validate_cloud_bot_gpu(raw_value: str) -> str:
    """Kiểm tra GPU hợp lệ cho pipeline video CUDA (chặn chọn nhầm TPU)."""
    gpu = validate_gpu_type(raw_value)
    if gpu in DISALLOWED_PIPELINE_TPUS:
        raise CloudValidationError(
            f"Pipeline lồng tiếng cần NVIDIA CUDA nên không dùng TPU `{gpu}`. Hãy chọn `T4`, `L4`, `A100` hoặc `H100`.",
            code="UNSUPPORTED_GPU",
        )
    return gpu


def _safe_stem(raw_name: str, fallback: str = "cloud_video") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(raw_name or "")).strip(" ._")
    return (cleaned[:70] or fallback).strip(" ._") or fallback


class CloudBotStateFiles:
    """Quản lý lưu trữ nguyên tử cho owner lock và tuỳ chọn GPU/giọng đọc của Bot Cloud."""

    def __init__(self, workspace_dir: Optional[Path] = None):
        base = Path(workspace_dir or os.getenv("AUTODUB_WORKSPACE", str(WORKSPACE_DIR)))
        self.state_dir = base / "cloud_desktop"
        self.owner_file = self.state_dir / "cloud_bot_owner.json"
        self.prefs_file = self.state_dir / "cloud_bot_prefs.json"
        self.downloads_dir = self.state_dir / "telegram_downloads"
        self._lock = threading.Lock()

    def _write_json_atomic(self, target: Path, payload: Dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)

    def _env_allowed_users(self) -> Set[int]:
        raw = os.getenv("CLOUD_BOT_ALLOWED_USER_IDS", "").strip()
        if not raw:
            return set()
        allowed: Set[int] = set()
        for part in re.split(r"[\s,;]+", raw):
            if part.strip().lstrip("-").isdigit():
                allowed.add(int(part.strip()))
        return allowed

    def authorize_user(self, user_id: Optional[int], username: Optional[str] = None) -> Tuple[bool, bool]:
        """
        Kiểm tra quyền dùng Bot Cloud.
        Trả về `(allowed, newly_paired)`:
        - Nếu có `CLOUD_BOT_ALLOWED_USER_IDS`: chỉ cho phép ID trong danh sách.
        - Nếu chưa có owner file: tự động khoá cứng cho người dùng đầu tiên nhắn tin.
        """
        if user_id is None:
            return False, False
        uid = int(user_id)
        env_allowed = self._env_allowed_users()
        if env_allowed:
            return (uid in env_allowed), False

        with self._lock:
            if self.owner_file.is_file():
                try:
                    data = json.loads(self.owner_file.read_text(encoding="utf-8"))
                    owner_id = int(data.get("owner_user_id"))
                    return (uid == owner_id), False
                except Exception:
                    return False, False

            self._write_json_atomic(
                self.owner_file,
                {
                    "owner_user_id": uid,
                    "username": str(username or ""),
                    "paired_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            return True, True

    def get_prefs(self) -> Dict[str, Any]:
        defaults = {
            "gpu_type": "T4",
            "voice": "auto",
            "allow_no_speech": False,
        }
        with self._lock:
            if not self.prefs_file.is_file():
                return dict(defaults)
            try:
                loaded = json.loads(self.prefs_file.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict):
                    return dict(defaults)
                gpu = str(loaded.get("gpu_type") or "T4").upper()
                if gpu in DISALLOWED_PIPELINE_TPUS:
                    gpu = "T4"
                voice = str(loaded.get("voice") or "auto")
                if voice not in ALLOWED_VOICES:
                    voice = "auto"
                return {
                    "gpu_type": gpu,
                    "voice": voice,
                    "allow_no_speech": bool(loaded.get("allow_no_speech", False)),
                }
            except Exception:
                return dict(defaults)

    def update_prefs(self, **updates: Any) -> Dict[str, Any]:
        current = self.get_prefs()
        current.update(updates)
        with self._lock:
            self._write_json_atomic(self.prefs_file, current)
        return current


@dataclass
class CloudBotQueuedItem:
    item_id: str
    kind: str  # "url" | "telegram_video"
    chat_id: int
    source_label: str
    url: str = ""
    file_id: str = ""
    file_name: str = ""
    gpu_type: str = "T4"
    voice: str = "auto"
    allow_no_speech: bool = False
    status_message: Any = None
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)


async def _safe_edit_message(message: Any, text: str, *, parse_mode: Optional[str] = "Markdown") -> None:
    if message is None:
        return
    safe_txt = redact_secrets(text)
    for attempt in range(2):
        try:
            await message.edit_text(safe_txt, parse_mode=parse_mode)
            return
        except Exception as exc:
            if "Message is not modified" in str(exc):
                return
            if parse_mode is not None:
                try:
                    await message.edit_text(safe_txt)
                    return
                except Exception:
                    pass
            if attempt == 0:
                await asyncio.sleep(0.8)


async def _deliver_video_to_telegram(
    bot: Any,
    chat_id: int,
    final_video: Path,
    caption: str,
    status_msg: Any,
) -> bool:
    """Gửi file thành phẩm lên khung chat Telegram (tự chia phần không nén nếu > 49.5MB)."""
    file_size = final_video.stat().st_size
    if file_size <= TELEGRAM_SEND_MAX_BYTES:
        with open(final_video, "rb") as vf:
            await bot.send_video(
                chat_id=chat_id,
                video=vf,
                caption=caption,
                supports_streaming=True,
                read_timeout=600,
                write_timeout=600,
                connect_timeout=120,
            )
        return True

    await _safe_edit_message(
        status_msg,
        f"📤 Video thành phẩm nặng `{file_size // (1024 * 1024)} MB` (đã lưu nguyên bản tại `{final_video.parent}`).\n"
        "Đang tách phần nguyên gốc (không nén giảm nét) để gửi qua Telegram...",
    )
    probe = probe_video_metadata(final_video)
    duration = float(probe.get("duration_seconds") or 0.0)
    if duration <= 0:
        return False

    num_chunks = max(2, math.ceil(file_size / TELEGRAM_SEND_MAX_BYTES))
    chunk_time = max(10.0, duration / num_chunks)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0

    with tempfile.TemporaryDirectory(prefix="cloudbot_split_") as split_dir:
        pattern = str(Path(split_dir) / "part_%03d.mp4")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(final_video),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            f"{chunk_time:.2f}",
            "-reset_timestamps",
            "1",
            pattern,
        ]
        await asyncio.to_thread(
            subprocess.run,
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        parts = sorted(glob.glob(str(Path(split_dir) / "part_*.mp4")))
        if not parts:
            return False
        for idx, part_path in enumerate(parts, 1):
            part_caption = f"{caption}\n\n(Phần {idx}/{len(parts)})" if idx == 1 else f"🎬 Phần {idx}/{len(parts)}"
            with open(part_path, "rb") as vf:
                await bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=part_caption,
                    supports_streaming=True,
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=120,
                )
    return True


class CloudTelegramBotService:
    """Dịch vụ chạy nền quản lý Telegram Bot riêng cho Cloud GPU."""

    def __init__(
        self,
        *,
        workspace_dir: Optional[Path] = None,
        store: Optional[CloudJobStore] = None,
        downloader_fn: Optional[Callable[[str, str, str], Tuple[bool, str, str, str]]] = None,
        adapter_factory: Optional[Callable[[], Any]] = None,
    ):
        self.workspace_dir = Path(workspace_dir or os.getenv("AUTODUB_WORKSPACE", str(WORKSPACE_DIR)))
        self.state_files = CloudBotStateFiles(self.workspace_dir)
        self.store = store or CloudJobStore(
            db_path=self.workspace_dir / "cloud_jobs.db",
            rollout_controlled=False,
        )
        self._downloader_fn = downloader_fn
        self._adapter_factory = adapter_factory or ColabProviderAdapter
        self._cloud_worker = CloudWorker(
            poll_interval=2.0,
            worker_id=f"cloud-telebot-{uuid.uuid4().hex[:6]}",
            heartbeat_interval=3.0,
            store=self.store,
            orchestrator=CloudOrchestrator(
                self.store,
                adapter=self._adapter_factory(),
                allowed_roots=[self.workspace_dir, get_cloud_output_dir(), *get_default_allowed_roots()],
            ),
        )
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_async_ev: Optional[asyncio.Event] = None
        self._queue: Optional[asyncio.Queue[Optional[CloudBotQueuedItem]]] = None
        self._pending_items: List[CloudBotQueuedItem] = []
        self._active_item: Optional[CloudBotQueuedItem] = None
        self._items_lock = threading.Lock()
        self._lock_handle: Optional[Any] = None
        self._running = False
        self._bot_username: Optional[str] = None
        self._last_error: Optional[str] = None
        if hasattr(self._cloud_worker.orchestrator, "has_pending_work_callback"):
            self._cloud_worker.orchestrator.has_pending_work_callback = self.has_waiting_items_for_gpu

    def has_waiting_items_for_gpu(self, gpu_type: str) -> bool:
        gpu_norm = str(gpu_type or "").strip().upper()
        with self._items_lock:
            return any(
                not item.cancelled and str(item.gpu_type or "").strip().upper() == gpu_norm
                for item in self._pending_items
            )

    @property
    def worker(self) -> CloudWorker:
        return self._cloud_worker

    def is_busy(self) -> bool:
        with self._items_lock:
            has_items = self._active_item is not None or any(not item.cancelled for item in self._pending_items)
        return has_items or (self._cloud_worker.get_current_job() is not None)

    def get_status(self) -> Dict[str, Any]:
        token_present = bool(resolve_cloud_bot_token())
        with self._items_lock:
            queued_len = sum(1 for item in self._pending_items if not item.cancelled)
        return {
            "configured": token_present,
            "running": bool(self._running and self._thread and self._thread.is_alive()),
            "bot_username": self._bot_username,
            "queued_count": queued_len,
            "output_dir": str(get_cloud_output_dir()),
            "last_error": self._last_error,
        }

    def _acquire_singleton_lock(self) -> bool:
        lock_path = self.state_files.state_dir / "cloud_bot.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_handle = fh
            return True
        except OSError:
            fh.close()
            return False

    def _release_singleton_lock(self) -> None:
        fh = self._lock_handle
        self._lock_handle = None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                fh.close()
            except OSError:
                pass

    def start_if_configured(self) -> bool:
        """Khởi động bot trên luồng nền nếu đã có CLOUD_BOT_TOKEN; không chặn Dashboard."""
        token = resolve_cloud_bot_token()
        if not token:
            logger.info("Chưa cấu hình CLOUD_BOT_TOKEN; bỏ qua khởi động Telegram Bot Cloud.")
            return False
        if self._thread and self._thread.is_alive():
            return True
        if not self._acquire_singleton_lock():
            logger.info("Một tiến trình Cloud Telegram Bot khác đang giữ khóa; bỏ qua bản trùng.")
            return False

        self._last_error = None
        self._thread = threading.Thread(
            target=self._run_bot_thread,
            args=(token,),
            name="CloudTelegramBotThread",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 8.0) -> bool:
        """Dừng polling Telegram Bot an toàn và giải phóng khóa."""
        loop = self._loop
        stop_ev = self._stop_async_ev
        if loop is not None and stop_ev is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_ev.set)
            except RuntimeError:
                pass
        if self._cloud_worker.get_current_job() is not None:
            self._cloud_worker.cancel_current_job(reason="Cloud Node đang dừng dịch vụ.")
        rel_fn = getattr(self._cloud_worker.orchestrator, "release_warm_sessions", None)
        if callable(rel_fn):
            try:
                rel_fn()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.5, float(timeout)))
        alive = bool(thread and thread.is_alive())
        if not alive:
            self._running = False
            self._release_singleton_lock()
        return not alive

    # -------------------- Xử lý nghiệp vụ Telegram --------------------

    async def _check_owner_or_reply(self, update: Any) -> bool:
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        user = getattr(update, "effective_user", None)
        user_id = getattr(user, "id", None)
        username = getattr(user, "username", None)
        allowed, newly_paired = self.state_files.authorize_user(user_id, username)
        if not allowed:
            if message is not None:
                await message.reply_text("⛔ Bot Cloud riêng tư — tài khoản Telegram này không có quyền sử dụng.")
            return False
        if newly_paired and message is not None:
            await message.reply_text(
                f"🔐 Đã khóa quyền sở hữu Bot Cloud cho tài khoản của bạn (`ID: {user_id}`).",
                parse_mode="Markdown",
            )
        return True

    async def cmd_start(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        prefs = self.state_files.get_prefs()
        budget = await asyncio.to_thread(self.store.get_cu_budget_info)
        voice_label = VOICE_LABELS.get(prefs["voice"], prefs["voice"])
        out_dir = get_cloud_output_dir()
        msg = (
            "☁️ *TOOL V1 — CLOUD GPU BOT*\n"
            "_Chạy độc lập song song với Bot Local_\n\n"
            "📥 *Cách dùng:* Gửi thẳng link video (Douyin, TikTok, Xiaohongshu, YouTube, FB...) "
            "hoặc gửi/forward file `.mp4` (≤ 20MB) vào đây là tự chạy.\n\n"
            "⚙️ *Cấu hình hiện tại:*\n"
            f"• GPU: `NVIDIA {prefs['gpu_type']}`\n"
            f"• Giọng đọc: `{voice_label}`\n"
            f"• Hạn mức hôm nay: `{budget['spent_today']:.2f} / {budget['budget_daily']:.2f} CU`\n"
            f"• Lưu tại máy: `{out_dir}`\n\n"
            "🛠 *Lệnh điều khiển nhanh:*\n"
            "• `/status` — Xem số dư CU Colab & hàng đợi\n"
            "• `/gpu T4` hoặc `/gpu L4` — Đổi loại GPU\n"
            "• `/voice auto|mai|nam|tuoivui|hoaimy|namminh` — Đổi giọng\n"
            "• `/budget <số_CU>` — Đổi trần bảo vệ CU/ngày\n"
            "• `/stop` — Huỷ job & thu hồi ngay máy ảo Colab"
        )
        await update.effective_message.reply_text(msg, parse_mode="Markdown")

    async def cmd_status(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        prefs = self.state_files.get_prefs()
        snap = await asyncio.to_thread(
            get_cloud_status_snapshot,
            self.store,
            worker=self._cloud_worker,
            force_refresh=True,
            wait_timeout_seconds=5.0,
        )
        usage = snap.get("usage") or {}
        pcache = snap.get("provider_cache") or {}
        if pcache.get("balance_available") and not pcache.get("balance_stale") and usage.get("ok"):
            try:
                await asyncio.to_thread(self.store.record_provider_usage_snapshot, usage)
            except Exception:
                pass
        budget = await asyncio.to_thread(self.store.get_cu_budget_info)
        if pcache.get("balance_available"):
            bal_txt = f"`{float(usage.get('balance', 0.0)):.2f} CU`"
            if pcache.get("balance_stale"):
                bal_txt += " _(dữ liệu cũ)_"
        else:
            bal_txt = "`--` _(chưa có dữ liệu mới)_"

        rate = float(usage.get("usage_rate") or 0.0)
        vms = int(usage.get("active_assignments") or 0)
        active_job = snap.get("active_job")
        with self._items_lock:
            waiting = sum(1 for item in self._pending_items if not item.cancelled)

        active_line = (
            f"🎬 Đang chạy: `{active_job['video_name']}` (`{active_job.get('percent', 0)}%` — {active_job.get('step_name', '')})"
            if active_job
            else "💤 Hiện không có video nào đang chạy."
        )
        text = (
            "☁️ *TRẠNG THÁI CLOUD GPU*\n\n"
            f"💰 *CU còn lại (Colab):* {bal_txt}\n"
            f"⚡ *Tốc độ tiêu thụ:* `{rate:.2f} CU/h` · Máy ảo mở: `{vms}`\n"
            f"🛡️ *CU hôm nay:* `{budget.get('spent_today', 0.0):.2f} / {budget.get('budget_daily', 10.0):.2f} CU`\n"
            f"🚀 *Cấu hình Bot:* `NVIDIA {prefs['gpu_type']}` · `{VOICE_LABELS.get(prefs['voice'], prefs['voice'])}`\n"
            f"📂 *Thư mục xuất:* `{get_cloud_output_dir()}`\n"
            f"📋 *Hàng chờ Bot:* `{waiting}` video\n\n"
            f"{active_line}"
        )
        await update.effective_message.reply_text(text, parse_mode="Markdown")

    async def cmd_gpu(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            prefs = self.state_files.get_prefs()
            await update.effective_message.reply_text(
                f"🚀 GPU hiện tại: `NVIDIA {prefs['gpu_type']}`.\n"
                "Đổi bằng lệnh: `/gpu T4`, `/gpu L4`, `/gpu A100` hoặc `/gpu H100`.",
                parse_mode="Markdown",
            )
            return
        try:
            gpu = validate_cloud_bot_gpu(args[0])
        except CloudValidationError as exc:
            await update.effective_message.reply_text(f"❌ {exc}", parse_mode="Markdown")
            return
        self.state_files.update_prefs(gpu_type=gpu)
        await update.effective_message.reply_text(
            f"✅ Đã chuyển GPU mặc định của Bot Cloud sang `NVIDIA {gpu}`.",
            parse_mode="Markdown",
        )

    async def cmd_voice(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            prefs = self.state_files.get_prefs()
            options_txt = "\n".join(
                [
                    "• `/voice auto` — Tự động Nam/Nữ chuẩn V1 (Mặc định)",
                    "• `/voice mai` — CapCut Mai (Nữ chuẩn V1)",
                    "• `/voice nam` — CapCut Thanh Niên Tự Tin (Nam)",
                    "• `/voice tuoivui` — CapCut Cô Gái Tươi Vui",
                    "• `/voice hoaimy` — Edge Hoài My (Nữ)",
                    "• `/voice namminh` — Edge Nam Minh (Nam)",
                ]
            )
            await update.effective_message.reply_text(
                f"🎙 Giọng hiện tại: `{VOICE_LABELS.get(prefs['voice'], prefs['voice'])}`\n\n{options_txt}",
                parse_mode="Markdown",
            )
            return
        try:
            voice = resolve_voice_choice(args[0])
        except CloudValidationError as exc:
            await update.effective_message.reply_text(f"❌ {exc}", parse_mode="Markdown")
            return
        self.state_files.update_prefs(voice=voice)
        await update.effective_message.reply_text(
            f"✅ Đã đổi giọng đọc sang `{VOICE_LABELS.get(voice, voice)}`.",
            parse_mode="Markdown",
        )

    async def cmd_budget(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            b = await asyncio.to_thread(self.store.get_cu_budget_info)
            await update.effective_message.reply_text(
                f"🛡️ Hạn mức CU hôm nay: `{b['spent_today']:.2f} / {b['budget_daily']:.2f} CU` "
                f"(còn lại `{b['remaining_today']:.2f} CU`).\n"
                "Đổi hạn mức: `/budget <số_CU>` (ví dụ `/budget 15`).",
                parse_mode="Markdown",
            )
            return
        try:
            val = float(args[0].strip())
            if not math.isfinite(val) or val <= 0:
                raise ValueError("Hạn mức phải lớn hơn 0.")
            await asyncio.to_thread(self.store.set_cu_budget_daily, val)
            b = await asyncio.to_thread(self.store.get_cu_budget_info)
            await update.effective_message.reply_text(
                f"💾 Đã lưu hạn mức hôm nay: `{b['budget_daily']:.2f} CU/ngày` "
                f"(còn lại `{b['remaining_today']:.2f} CU`).",
                parse_mode="Markdown",
            )
        except Exception as exc:
            await update.effective_message.reply_text(
                f"❌ Giá trị không hợp lệ ({redact_secrets(str(exc))}). Ví dụ: `/budget 10`.",
                parse_mode="Markdown",
            )

    async def cmd_nospeech(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        args = getattr(context, "args", None) or []
        if not args or args[0].lower() not in ("on", "off", "1", "0"):
            prefs = self.state_files.get_prefs()
            state_txt = "BẬT" if prefs["allow_no_speech"] else "TẮT"
            await update.effective_message.reply_text(
                f"🎵 Cho phép xuất video không lời thoại hiện đang: `{state_txt}`.\n"
                "Dùng `/nospeech on` hoặc `/nospeech off` để đổi.",
                parse_mode="Markdown",
            )
            return
        enabled = args[0].lower() in ("on", "1")
        self.state_files.update_prefs(allow_no_speech=enabled)
        await update.effective_message.reply_text(
            f"✅ Đã {'BẬT' if enabled else 'TẮT'} chế độ giữ nhạc nền khi video không có lời thoại.",
            parse_mode="Markdown",
        )

    async def cmd_stop(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        cancelled_pending = 0
        with self._items_lock:
            for item in self._pending_items:
                if not item.cancelled:
                    item.cancelled = True
                    cancelled_pending += 1
            if self._active_item is not None:
                self._active_item.cancelled = True

        def _cancel_db_and_worker():
            cur = self._cloud_worker.get_current_job()
            if cur is not None:
                request_job_cancellation(
                    self.store,
                    cur.job_id,
                    worker=self._cloud_worker,
                    adapter=self._adapter_factory(),
                    abort_fn=abort_session_processes,
                    reason="Huỷ từ lệnh /stop trên Bot Cloud.",
                )
            for j in self.store.list_jobs(limit=25):
                if j.state not in ("completed", "cancelled", "failed", "error", "stopped"):
                    self.store.request_cancel(j.job_id, reason="Huỷ từ lệnh /stop trên Bot Cloud.")
            rel_fn = getattr(self._cloud_worker.orchestrator, "release_warm_sessions", None)
            if callable(rel_fn):
                rel_fn()

        await asyncio.to_thread(_cancel_db_and_worker)
        action = await asyncio.to_thread(
            run_provider_action_nonblocking,
            stop_all_sessions,
            timeout_seconds=1.5,
        )
        if action.get("timed_out"):
            note = "Đang gửi lệnh thu hồi máy ảo Colab (kiểm tra lại sau ít giây với `/status`)."
        elif action.get("completed") and action["result"][0]:
            note = "Đã xác nhận dừng toàn bộ máy ảo Colab."
        else:
            note = "Đã ghi nhận yêu cầu dừng job & thu hồi session."

        await update.effective_message.reply_text(
            f"🛑 Đã huỷ `{cancelled_pending}` video đang chờ. {note}",
            parse_mode="Markdown",
        )

    async def handle_text_message(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        message = update.effective_message
        text = (message.text or "").strip()
        urls = extract_video_urls(text)
        if not urls:
            await message.reply_text(
                "❓ Hãy dán link video (`https://...` Douyin, TikTok, Xiaohongshu, YouTube, FB...) "
                "hoặc gửi file video `.mp4` (≤ 20MB) để chạy trên Cloud GPU.\n"
                "Gõ `/status` để xem số dư CU hoặc `/help` để xem các lệnh.",
                parse_mode="Markdown",
            )
            return

        prefs = self.state_files.get_prefs()
        for url in urls:
            await self._enqueue_item(
                message=message,
                kind="url",
                source_label=url,
                url=url,
                prefs=prefs,
            )

    async def handle_video_message(self, update: Any, context: Any) -> None:
        if not await self._check_owner_or_reply(update):
            return
        message = update.effective_message
        video_obj = getattr(message, "video", None) or getattr(message, "document", None)
        if video_obj is None:
            return
        file_size = int(getattr(video_obj, "file_size", 0) or 0)
        if file_size > TELEGRAM_BOT_DOWNLOAD_LIMIT_BYTES:
            mb = file_size / (1024 * 1024)
            await message.reply_text(
                f"⚠️ File nặng `{mb:.1f} MB` vượt giới hạn tải trực tiếp 20MB của Telegram Bot API.\n"
                "👉 Cách nhanh nhất: gửi **link gốc** của video vào đây, hoặc chép file vào `D:\\workspace` rồi bấm chạy trên Cloud Dashboard.",
                parse_mode="Markdown",
            )
            return

        orig_name = getattr(video_obj, "file_name", None) or f"telegram_video_{uuid.uuid4().hex[:6]}.mp4"
        if not Path(orig_name).suffix:
            orig_name += ".mp4"
        prefs = self.state_files.get_prefs()
        await self._enqueue_item(
            message=message,
            kind="telegram_video",
            source_label=orig_name,
            file_id=str(video_obj.file_id),
            file_name=orig_name,
            prefs=prefs,
        )

    async def _enqueue_item(
        self,
        *,
        message: Any,
        kind: str,
        source_label: str,
        prefs: Dict[str, Any],
        url: str = "",
        file_id: str = "",
        file_name: str = "",
    ) -> CloudBotQueuedItem:
        item = CloudBotQueuedItem(
            item_id=uuid.uuid4().hex[:12],
            kind=kind,
            chat_id=int(message.chat_id),
            source_label=source_label,
            url=url,
            file_id=file_id,
            file_name=file_name,
            gpu_type=prefs["gpu_type"],
            voice=prefs["voice"],
            allow_no_speech=bool(prefs["allow_no_speech"]),
        )
        with self._items_lock:
            self._pending_items.append(item)
            position = sum(1 for x in self._pending_items if not x.cancelled) + (
                1 if self._active_item is not None else 0
            )
        short_src = source_label if len(source_label) <= 68 else source_label[:65] + "..."
        item.status_message = await message.reply_text(
            f"☁️ *Đã nhận vào hàng đợi Cloud GPU (`{item.gpu_type}`)* — vị trí `#{position}`\n"
            f"📎 Nguồn: `{short_src}`",
            parse_mode="Markdown",
        )
        if self._queue is not None:
            await self._queue.put(item)
        return item

    def _download_social_url_sync(self, url: str, dest_dir: Path, tag: str) -> Tuple[Path, str]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if self._downloader_fn is not None:
            ok, path_str, title, err = self._downloader_fn(url, str(dest_dir), tag)
        else:
            if str(BACKEND_DIR) not in sys.path:
                sys.path.insert(0, str(BACKEND_DIR))
            from social_downloader import download_social_video

            ok, path_str, title, err = download_social_video(url, str(dest_dir), tag)
        if not ok or not path_str:
            raise RuntimeError(err or "Không tải được video từ đường link đã gửi.")
        downloaded = Path(path_str)
        if not downloaded.is_file() or downloaded.stat().st_size <= 0:
            raise RuntimeError("File video tải về bị rỗng hoặc không tồn tại.")
        meta = probe_video_metadata(downloaded)
        if not meta.get("header_valid"):
            raise RuntimeError("Đường link không trả về tệp video hợp lệ (có thể là bài ảnh hoặc link lỗi).")

        clean_title = _safe_stem(title or downloaded.stem, fallback=f"clip_{tag}")
        ext = downloaded.suffix.lower() or ".mp4"
        named_path = dest_dir / f"{clean_title}_{tag}{ext}"
        if named_path.resolve() != downloaded.resolve():
            os.replace(downloaded, named_path)
        return named_path, clean_title

    async def _download_telegram_video(self, bot: Any, item: CloudBotQueuedItem, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_stem(Path(item.file_name).stem, fallback=f"tg_{item.item_id}")
        ext = Path(item.file_name).suffix.lower() or ".mp4"
        target = dest_dir / f"{stem}_{item.item_id}{ext}"
        partial = target.with_suffix(".partial" + ext)
        try:
            tg_file = await bot.get_file(item.file_id, read_timeout=60, connect_timeout=60)
            await tg_file.download_to_drive(
                str(partial),
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
            )
            meta = await asyncio.to_thread(probe_video_metadata, partial)
            if not meta.get("header_valid"):
                raise RuntimeError("Tệp tải lên không phải định dạng video hợp lệ.")
            os.replace(partial, target)
            return target
        finally:
            partial.unlink(missing_ok=True)

    def _reconcile_orphans_sync(self) -> List[Dict[str, Any]]:
        """
        Tự động thu hồi session Colab mồ côi từ tiến trình cũ bị quá hạn lease.
        Không bao giờ chạm tới provider nếu SQLite sạch hoặc đang có job khoẻ mạnh.
        """
        now = time.time()
        active = self.store.get_active_job()
        has_expired_active = bool(
            active is not None
            and (active.lease_expires_at is None or float(active.lease_expires_at) < now)
        )
        pending_events = self.store.list_pending_cleanup_events()
        has_orphan_cleanup = False
        if pending_events:
            if active is None or has_expired_active:
                has_orphan_cleanup = True
            else:
                has_orphan_cleanup = any(ev.get("job_id") != active.job_id for ev in pending_events)

        if not has_expired_active and not has_orphan_cleanup:
            return []

        adapter = self._adapter_factory()
        results = self.store.reconcile_expired_leases(adapter=adapter, now=now)
        if self.store.get_active_job() is None and self.store.list_pending_cleanup_events():
            results.extend(self.store.recover(adapter=adapter, only_expired_leases=False, now=time.time()))
        return results

    def _refresh_provider_usage_if_stale_sync(self) -> Optional[Dict[str, Any]]:
        """
        Tự động làm mới snapshot số dư Colab trong SQLite khi bản ghi trước đó đã quá 300s.
        Không gọi provider nếu chưa từng có snapshot hoặc snapshot vẫn còn mới.
        """
        snap = self.store.get_provider_usage_snapshot()
        if snap is None or (snap.get("ok") and not snap.get("is_stale")):
            return None
        adapter = self._adapter_factory()
        get_usage_fn = getattr(adapter, "get_usage", None)
        if not callable(get_usage_fn):
            return None
        usage = get_usage_fn()
        if isinstance(usage, dict):
            self.store.record_provider_usage_snapshot(usage)
            return usage
        return None

    async def _process_item(self, bot: Any, item: CloudBotQueuedItem) -> None:
        if item.cancelled:
            await _safe_edit_message(item.status_message, "🛑 Đã huỷ video khỏi hàng đợi theo yêu cầu.")
            return

        started = time.monotonic()
        local_source_path: Optional[Path] = None
        loop = asyncio.get_running_loop()

        try:
            # 0. Đối soát các phiên quá hạn lease & làm mới số dư Colab nếu snapshot cũ > 300s
            try:
                await asyncio.to_thread(self._reconcile_orphans_sync)
            except Exception as rec_exc:
                logger.warning("Đối soát session mồ côi trước job gặp cảnh báo: %s", redact_secrets(str(rec_exc)))

            fresh_usage: Optional[Dict[str, Any]] = None
            try:
                fresh_usage = await asyncio.to_thread(self._refresh_provider_usage_if_stale_sync)
            except Exception as usage_exc:
                logger.warning("Làm mới số dư Colab trước job gặp cảnh báo: %s", redact_secrets(str(usage_exc)))

            # 1. Kiểm tra ngân sách CU hôm nay trước khi tải video (0 CU nếu vượt trần)
            decision = await asyncio.to_thread(
                self.store.check_admission_allowed,
                cu_estimated=0.5,
                gpu_type=item.gpu_type,
                enforce_budget=True,
                provider_usage=fresh_usage,
            )
            if not decision.get("allowed", True):
                reason = redact_secrets(str(decision.get("reason") or "Đã chạm hạn mức CU hôm nay."))
                code = str(decision.get("code") or "")
                hint = (
                    "💡 Chưa đọc được số dư mới từ Google Colab (WSL). Gõ `/status` sau vài giây rồi gửi lại link nhé."
                    if code == "BUDGET_UNCERTAIN"
                    else "💡 Gõ `/budget <số_CU>` nếu bạn muốn nâng hạn mức hôm nay."
                )
                await _safe_edit_message(
                    item.status_message,
                    f"🛡️ *Tạm chặn để bảo vệ CU:*\n{reason}\n\n{hint}",
                )
                return

            # 2. Tải nguồn video cục bộ (0 CU trên Colab)
            downloads_dir = self.state_files.downloads_dir
            if item.kind == "url":
                await _safe_edit_message(
                    item.status_message,
                    f"📥 Đang tải video gốc không logo...\n`{item.url[:80]}`",
                )
                local_source_path, _ = await asyncio.to_thread(
                    self._download_social_url_sync,
                    item.url,
                    downloads_dir,
                    item.item_id,
                )
            else:
                await _safe_edit_message(
                    item.status_message,
                    f"📥 Đang tải file video `{item.file_name}` từ Telegram...",
                )
                local_source_path = await self._download_telegram_video(bot, item, downloads_dir)

            if item.cancelled:
                await _safe_edit_message(item.status_message, "🛑 Đã huỷ trước khi cấp phát GPU.")
                return

            # 3. Đưa vào CloudJobStore & claim nguyên tử (làm mới lại nếu bước tải video kéo dài > 300s)
            claim_usage: Optional[Dict[str, Any]] = None
            try:
                claim_usage = await asyncio.to_thread(self._refresh_provider_usage_if_stale_sync)
            except Exception as usage_exc2:
                logger.warning("Làm mới số dư Colab trước claim gặp cảnh báo: %s", redact_secrets(str(usage_exc2)))

            allowed_roots = [self.workspace_dir, get_cloud_output_dir(), *get_default_allowed_roots()]
            self._cloud_worker.orchestrator.allowed_roots = allowed_roots
            cloud_job, _ = await asyncio.to_thread(
                admit_and_enqueue_job,
                self.store,
                video_path=str(local_source_path),
                source_channel="cloud_telegram_bot",
                source_job_id=item.item_id,
                gpu_type=item.gpu_type,
                task_type="full_pipeline",
                voice=item.voice,
                enforce_budget=True,
                provider_usage=claim_usage or fresh_usage,
                claim_immediately=True,
                owner_id=self._cloud_worker.worker_id,
                idempotency_key=f"cloudbot:{item.item_id}",
                allowed_roots=allowed_roots,
                options={
                    "execution_mode": "cloud",
                    "allow_no_speech": item.allow_no_speech,
                    "source_label": item.source_label[:200],
                },
            )

            last_edit_at = [0.0]
            last_pct = [-1]

            def _on_progress(percent: int, step_name: str) -> None:
                now_m = time.monotonic()
                if percent != 100 and (now_m - last_edit_at[0]) < 2.5 and abs(percent - last_pct[0]) < 10:
                    return
                last_edit_at[0] = now_m
                last_pct[0] = percent
                msg_text = (
                    f"☁️ *[Cloud GPU {cloud_job.gpu_type}]* · `{percent}%`\n"
                    f"👉 {redact_secrets(step_name)}"
                )
                loop.call_soon_threadsafe(
                    lambda: asyncio.create_task(_safe_edit_message(item.status_message, msg_text))
                )

            success = await asyncio.to_thread(
                self._cloud_worker.process_claimed_job,
                cloud_job,
                _on_progress,
            )
            updated = await asyncio.to_thread(self.store.get_job, cloud_job.job_id)

            if not success or updated is None or updated.state != "completed":
                if updated and updated.state in ("cancelled", "stopped", "cancel_requested", "cancelling"):
                    await _safe_edit_message(
                        item.status_message,
                        f"🛑 Đã huỷ job `{cloud_job.job_id}` và thu hồi máy ảo Colab.",
                    )
                    return
                err_detail = redact_secrets(
                    (updated.error if updated else None) or "Pipeline Cloud GPU không hoàn tất."
                )
                remediation = get_error_remediation(updated.error_code if updated else None)
                hint = f"\n💡 {remediation}" if remediation else ""
                await _safe_edit_message(
                    item.status_message,
                    f"❌ *Không hoàn tất trên Cloud GPU:*\n{err_detail[:400]}{hint}",
                )
                return

            result_file = Path(updated.result_path) if updated.result_path else None
            if result_file is None or not result_file.is_file() or result_file.stat().st_size <= 0:
                await _safe_edit_message(
                    item.status_message,
                    "❌ Job báo hoàn tất nhưng không tìm thấy file thành phẩm trên ổ đĩa.",
                )
                return

            elapsed = int(max(1.0, time.monotonic() - started))
            time_str = f"{elapsed // 60}p {elapsed % 60}s" if elapsed >= 60 else f"{elapsed}s"
            cu_val = float(updated.cu_actual or 0.0)
            caption = (
                f"🎬 Đã lồng tiếng Việt (Cloud GPU {updated.gpu_type})\n"
                f"⏱ {time_str} · ⚡ ~{cu_val:.3f} CU\n"
                f"💾 Lưu tại: {result_file}"
            )
            await _safe_edit_message(
                item.status_message,
                f"📤 Đang gửi video thành phẩm lên Telegram...\n💾 Đã lưu: `{result_file}`",
            )
            sent_ok = False
            try:
                sent_ok = await _deliver_video_to_telegram(
                    bot,
                    item.chat_id,
                    result_file,
                    caption,
                    item.status_message,
                )
            except Exception as send_exc:
                logger.warning("Không gửi được file video qua Telegram: %s", redact_secrets(str(send_exc)))

            done_summary = (
                f"✅ *Hoàn tất Cloud GPU ({updated.gpu_type})!*\n"
                f"⏱ Thời gian: `{time_str}` · ⚡ Tiêu thụ: `~{cu_val:.3f} CU`\n"
                f"💾 Đã lưu vào: `{result_file}`"
            )
            if not sent_ok:
                done_summary += "\n_(Video lớn hoặc mạng chậm nên chưa đính kèm trực tiếp lên chat; bạn mở file tại thư mục trên nhé.)_"
            await _safe_edit_message(item.status_message, done_summary)

        except CloudValidationError as val_exc:
            await _safe_edit_message(
                item.status_message,
                f"⚠️ *Từ chối nhận job:* {redact_secrets(str(val_exc))}",
            )
        except Exception as exc:
            logger.exception("Lỗi xử lý job trên Cloud Telegram Bot")
            await _safe_edit_message(
                item.status_message,
                f"❌ *Lỗi xử lý video:* {redact_secrets(str(exc))[:400]}",
            )
        finally:
            if local_source_path is not None:
                try:
                    local_source_path.unlink(missing_ok=True)
                except OSError:
                    pass
            idle_rel_fn = getattr(self._cloud_worker.orchestrator, "release_idle_warm_sessions_if_no_work", None)
            if callable(idle_rel_fn):
                try:
                    await asyncio.to_thread(idle_rel_fn)
                except Exception:
                    pass

    async def _queue_consumer_loop(self, app: Any, stop_ev: asyncio.Event) -> None:
        assert self._queue is not None
        try:
            recovered = await asyncio.to_thread(self._reconcile_orphans_sync)
            if recovered:
                logger.info("Đã tự động thu hồi %d phiên/job mồ côi khi khởi động Bot Cloud.", len(recovered))
        except Exception as rec_exc:
            logger.warning("Đối soát khởi động Bot Cloud gặp cảnh báo: %s", redact_secrets(str(rec_exc)))

        while not stop_ev.is_set():
            get_task = asyncio.create_task(self._queue.get())
            stop_task = asyncio.create_task(stop_ev.wait())
            done, pending = await asyncio.wait(
                {get_task, stop_task},
                timeout=30.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                try:
                    await p
                except asyncio.CancelledError:
                    pass
            if stop_task in done and stop_ev.is_set():
                break
            if get_task not in done:
                try:
                    await asyncio.to_thread(self._reconcile_orphans_sync)
                except Exception as rec_exc:
                    logger.warning("Đối soát định kỳ Bot Cloud gặp cảnh báo: %s", redact_secrets(str(rec_exc)))
                continue
            item = get_task.result()
            if item is None:
                break
            with self._items_lock:
                if item in self._pending_items:
                    self._pending_items.remove(item)
                self._active_item = item
            try:
                await self._process_item(app.bot, item)
            finally:
                with self._items_lock:
                    self._active_item = None
                self._queue.task_done()

    def _run_bot_thread(self, token: str) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve_async(token))
        except Exception as exc:
            safe_err = redact_secrets(str(exc))
            self._last_error = safe_err
            logger.error("Cloud Telegram Bot dừng do lỗi: %s", safe_err)
        finally:
            self._running = False
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None
            self._release_singleton_lock()

    async def _serve_async(self, token: str) -> None:
        from telegram import Update
        from telegram.ext import Application, CommandHandler, MessageHandler, filters
        from telegram.request import HTTPXRequest

        stop_ev = asyncio.Event()
        self._stop_async_ev = stop_ev
        self._queue = asyncio.Queue()

        backoff = 3.0
        while not stop_ev.is_set():
            app = None
            consumer_task: Optional[asyncio.Task] = None
            try:
                request = HTTPXRequest(
                    connect_timeout=30.0,
                    read_timeout=120.0,
                    write_timeout=120.0,
                    pool_timeout=60.0,
                )
                app = Application.builder().token(token).request(request).build()
                app.add_handler(CommandHandler(["start", "help"], self.cmd_start))
                app.add_handler(CommandHandler(["status", "cloud_status"], self.cmd_status))
                app.add_handler(CommandHandler("gpu", self.cmd_gpu))
                app.add_handler(CommandHandler("voice", self.cmd_voice))
                app.add_handler(CommandHandler(["budget", "limit"], self.cmd_budget))
                app.add_handler(CommandHandler("nospeech", self.cmd_nospeech))
                app.add_handler(CommandHandler(["stop", "cancel"], self.cmd_stop))
                app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, self.handle_video_message))
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_message))

                async def _on_error(update: object, context: Any) -> None:
                    err = getattr(context, "error", None)
                    logger.warning("Cloud Telegram Bot sự kiện mạng/API: %s", redact_secrets(str(err)))

                app.add_error_handler(_on_error)

                await app.initialize()
                me = await app.bot.get_me()
                self._bot_username = getattr(me, "username", None)
                await app.start()
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=False,
                )
                self._running = True
                self._last_error = None
                backoff = 3.0
                logger.info(
                    "Cloud Telegram Bot (@%s) đã kết nối và sẵn sàng nhận video -> %s",
                    self._bot_username or "unknown",
                    get_cloud_output_dir(),
                )
                consumer_task = asyncio.create_task(self._queue_consumer_loop(app, stop_ev))
                await stop_ev.wait()
                break
            except Exception as exc:
                self._running = False
                safe_err = redact_secrets(str(exc))
                self._last_error = safe_err
                logger.warning(
                    "Cloud Telegram Bot tạm gián đoạn (%s); thử kết nối lại sau %.0fs...",
                    safe_err,
                    backoff,
                )
                try:
                    await asyncio.wait_for(stop_ev.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2.0, 30.0)
            finally:
                self._running = False
                if consumer_task is not None:
                    consumer_task.cancel()
                    try:
                        await consumer_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if app is not None:
                    try:
                        if getattr(app, "updater", None) and app.updater.running:
                            await app.updater.stop()
                    except Exception:
                        pass
                    try:
                        if app.running:
                            await app.stop()
                    except Exception:
                        pass
                    try:
                        await app.shutdown()
                    except Exception:
                        pass


_CLOUD_BOT_SINGLETON: Optional[CloudTelegramBotService] = None
_CLOUD_BOT_LOCK = threading.Lock()


def get_cloud_telegram_bot() -> CloudTelegramBotService:
    global _CLOUD_BOT_SINGLETON
    with _CLOUD_BOT_LOCK:
        if _CLOUD_BOT_SINGLETON is None:
            _CLOUD_BOT_SINGLETON = CloudTelegramBotService()
        return _CLOUD_BOT_SINGLETON


def peek_cloud_telegram_bot() -> Optional[CloudTelegramBotService]:
    with _CLOUD_BOT_LOCK:
        return _CLOUD_BOT_SINGLETON


def peek_cloud_telegram_bot_worker() -> Optional[CloudWorker]:
    svc = peek_cloud_telegram_bot()
    if svc is None:
        return None
    if svc.worker.get_current_job() is not None or svc.worker.is_running():
        return svc.worker
    return None


def get_cloud_telegram_bot_status() -> Dict[str, Any]:
    svc = peek_cloud_telegram_bot()
    if svc is not None:
        return svc.get_status()
    return {
        "configured": bool(resolve_cloud_bot_token()),
        "running": False,
        "bot_username": None,
        "queued_count": 0,
        "output_dir": str(get_cloud_output_dir()),
        "last_error": None,
    }
