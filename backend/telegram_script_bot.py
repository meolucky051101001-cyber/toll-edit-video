"""Telegram bot riêng cho Studio Kịch Bản (Tool V1) — prompt -> kịch bản -> video Doodle 9:16.

Khác biệt với telegram_bot.py (bot AutoDub):
- Token riêng: TELEGRAM_SCRIPT_BOT_TOKEN (không đụng BOT_TOKEN của AutoDub).
- Mọi thao tác render đi qua HTTP API của dashboard (127.0.0.1:8088) nên dùng
  chung SCRIPT_PROCESS_LOCK: web và bot KHÔNG bao giờ render song song.
- Mọi lỗi (mạng, AI, gửi video...) đều được báo về chat, không làm chết bot.

Cách chạy chuẩn (đã đăng ký Task Scheduler "AutoDubV1-scriptbot"):
    venv\\Scripts\\pythonw.exe background_service.py --service scriptbot
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from environment import load_environment  # noqa: E402

load_environment(_BACKEND_DIR)

import requests  # noqa: E402
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger("telegram_script_bot")

# ===== CẤU HÌNH =====
API_BASE = os.getenv("TELEGRAM_SCRIPT_API_BASE", "http://127.0.0.1:8088").rstrip("/")
DEFAULT_VOICE = "BV075_streaming"  # Thanh Niên Tự Tin (CapCut) — mặc định mới từ 18/10/2026
DEFAULT_AI_ENGINE = os.getenv("TELEGRAM_SCRIPT_AI_ENGINE", "muse").strip() or "muse"
MAX_PROMPT_CHARS = 5000
POLL_INTERVAL_SECONDS = 4.0
RENDER_TIMEOUT_SECONDS = 90 * 60
PREVIEW_MAX_CHARS = 3800

ENGINES: Dict[str, Dict[str, Any]] = {
    # 18/10/2026: đã bỏ "Bàn Tay Vẽ Thật" — chỉ còn phong cách animation (không tay vẽ)
    "kinetic": {"label": "⚡ Kinetic Reveal", "use_hand_draw": False, "mode": "kinetic"},
    "remotion": {"label": "🎬 Remotion (Beta)", "use_hand_draw": False, "mode": "remotion"},
}
ENGINE_ALIASES = {
    # Alias cũ của "bàn tay vẽ" tự chuyển sang Kinetic Reveal
    "bantay": "kinetic", "ban tay": "kinetic", "bàn tay": "kinetic", "bàn tay vẽ thật": "kinetic",
    "3band": "kinetic", "hand": "kinetic",
    "kinetic": "kinetic", "reveal": "kinetic",
    "remotion": "remotion", "remotionbeta": "remotion",
}
VOICE_NAME_FALLBACK = {
    "BV075_streaming": "Thanh Niên Tự Tin (Nam Trẻ)",
    "multi_male_felipe_uranus_bigtts": "Nam Trầm (Chuyên Gia)",
    "vi-VN-HoaiMyNeural": "Hoài My (Edge TTS)",
    "vi-VN-NamMinhNeural": "Nam Minh (Edge TTS)",
}

# ===== TRẠNG THÁI TRONG BỘ NHỚ =====
CHAT_SETTINGS: Dict[int, Dict[str, Any]] = {}
PENDING_SCRIPTS: Dict[int, Dict[str, Any]] = {}
ACTIVE_RENDERS: Dict[int, str] = {}
_VOICE_CACHE: Dict[str, Any] = {"at": 0.0, "items": []}


# ===== HÀM THUẦN (dễ test, không cần mạng) =====
def parse_allowlist(raw: str) -> Set[int]:
    """Tách mọi cụm số nguyên trong chuỗi: '1, 2;3 x 4' -> {1, 2, 3, 4}. Rỗng = mở."""
    return {int(token) for token in re.findall(r"-?\d+", raw or "")}


def engine_key(value: str) -> str:
    key = (value or "").strip().lower()
    if key in ENGINES:
        return key
    return ENGINE_ALIASES.get(key, "kinetic")


def engine_params(engine: str) -> Tuple[bool, str]:
    """Trả về (use_hand_draw, whiteboard_mode) đúng như UI Studio gửi."""
    conf = ENGINES.get(engine_key(engine), ENGINES["kinetic"])
    return bool(conf["use_hand_draw"]), str(conf["mode"])


def engine_label(engine: str) -> str:
    return str(ENGINES.get(engine_key(engine), ENGINES["kinetic"])["label"])


def one_line(text: Any, limit: int) -> str:
    """Gộp khoảng trắng về 1 dòng rồi cắt ngắn (dùng cho tiêu đề, nhãn, bước)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def clip(text: Any, limit: int) -> str:
    """Cắt ngắn văn bản nhiều dòng, giữ nguyên xuống dòng."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def format_script_preview(script: Dict[str, Any], engine: str, voice_nm: str) -> str:
    """Tóm tắt kịch bản để gửi Telegram (giới hạn < 4096 ký tự)."""
    title = one_line(script.get("title") or "(không có tiêu đề)", 160)
    accent = one_line(script.get("keyword_accent") or "", 60)
    scenes = script.get("scenes") or []
    lines: List[str] = [f"🎬 {title}"]
    if accent:
        lines.append(f"🏷️ Nhấn mạnh: {accent}")
    lines.append(f"🎞️ {len(scenes)} cảnh · {engine_label(engine)} · 🔊 {voice_nm}")
    lines.append("")
    for i, sc in enumerate(scenes, 1):
        if not isinstance(sc, dict):
            continue
        label = one_line(sc.get("doodle_label") or sc.get("label") or sc.get("visual_type") or "", 50)
        narr = one_line(sc.get("narration") or "", 160)
        lines.append(f"{i}. [{label}]" if label else f"{i}.")
        if narr:
            lines.append(f"    ↳ {narr}")
    text = "\n".join(lines)
    if len(text) > PREVIEW_MAX_CHARS:
        text = clip(text, PREVIEW_MAX_CHARS - 40) + "\n\n… (đã rút gọn — xem đầy đủ trên web)"
    return text


# ===== HTTP (chạy trong thread, không chặn event loop) =====
def _control_token() -> str:
    token = os.getenv("AUTODUB_LOCAL_TOKEN", "").strip()
    if token:
        return token
    root = Path(__file__).resolve().parent.parent
    for token_path in (
        root / "workspace" / "bot_system" / ".dashboard_control_token",
        root / "workspace" / ".dashboard_control_token",
        root / "workspace_backup_safety" / ".dashboard_control_token",
        Path(__file__).resolve().parent / ".dashboard_control_token",
    ):
        if token_path.is_file():
            try:
                return token_path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return ""


def _api_headers() -> Dict[str, str]:
    token = _control_token()
    return {"X-Local-Control-Token": token} if token else {}


def api_get(path: str, timeout: float = 15.0) -> Dict[str, Any]:
    resp = requests.get(f"{API_BASE}{path}", headers=_api_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, payload: Dict[str, Any], timeout: float = 60.0) -> Tuple[int, Dict[str, Any]]:
    resp = requests.post(
        f"{API_BASE}{path}", json=payload, headers=_api_headers(), timeout=timeout
    )
    try:
        data = resp.json()
    except Exception:
        data = {"status": "error", "message": resp.text[:300]}
    return resp.status_code, data


def get_voices_cached(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if not force and _VOICE_CACHE["items"] and now - _VOICE_CACHE["at"] < 3600:
        return _VOICE_CACHE["items"]
    try:
        data = api_get("/api/script/voices", timeout=10)
        items = data.get("voices") or []
        if items:
            _VOICE_CACHE["at"] = now
            _VOICE_CACHE["items"] = items
    except Exception as exc:
        logger.warning("Không lấy được danh sách giọng đọc: %s", exc)
    return _VOICE_CACHE["items"]


def voice_name(voice_id: str, voices: Optional[List[Dict[str, Any]]] = None) -> str:
    items = voices if voices is not None else get_voices_cached()
    for v in items:
        if str(v.get("id")) == voice_id:
            return str(v.get("name") or voice_id)
    return VOICE_NAME_FALLBACK.get(voice_id, voice_id)


# ===== TRẠNG THÁI / GIAO DIỆN =====
def get_settings(chat_id: int) -> Dict[str, Any]:
    return CHAT_SETTINGS.setdefault(
        chat_id,
        {"engine": "kinetic", "scene_count": 12, "voice": DEFAULT_VOICE},
    )


def settings_text(settings: Dict[str, Any]) -> str:
    return (
        "⚙️ CÀI ĐẶT STUDIO KỊCH BẢN\n"
        f"• Phong cách: {engine_label(settings['engine'])}\n"
        f"• Số cảnh: {settings['scene_count']}\n"
        f"• Giọng đọc: {voice_name(str(settings['voice']))}\n\n"
        "Bấm nút để đổi (✅ = đang chọn). Giọng & phong cách áp dụng ngay khi bấm Dựng video."
    )


def preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎬 Dựng video ngay", callback_data="act:render")],
            [
                InlineKeyboardButton("🔁 Tạo kịch bản khác", callback_data="act:regen"),
                InlineKeyboardButton("⚙️ Cài đặt", callback_data="act:settings"),
            ],
        ]
    )


def settings_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    engine_row: List[InlineKeyboardButton] = []
    for key in ("kinetic", "remotion"):
        label = ENGINES[key]["label"]
        if key == engine_key(str(settings["engine"])):
            label = "✅ " + label
        engine_row.append(InlineKeyboardButton(label, callback_data=f"eng:{key}"))
    rows.append(engine_row)

    scene_row = [
        InlineKeyboardButton(
            ("✅ " if n == settings["scene_count"] else "") + str(n),
            callback_data=f"scenes:{n}",
        )
        for n in (4, 6, 8, 10, 12, 16)
    ]
    rows.append(scene_row)

    pair: List[InlineKeyboardButton] = []
    for v in get_voices_cached()[:8]:
        vid = str(v.get("id") or "")
        if not vid:
            continue
        label = str(v.get("name") or vid)
        if vid == str(settings["voice"]):
            label = "✅ " + label
        pair.append(InlineKeyboardButton(label, callback_data=f"voice:{vid}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(rows)


# ===== TIỆN ÍCH TELEGRAM =====
async def safe_edit(message, text: str, reply_markup=None) -> None:
    body = clip(text, 4096)
    try:
        await message.edit_text(body, reply_markup=reply_markup)
    except Exception:
        try:
            await message.reply_text(body, reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("Không gửi được tin nhắn: %s", exc)


def is_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def deny(update: Update) -> None:
    if update.message is not None:
        await update.message.reply_text(
            "⛔ Bot này chỉ phục vụ chủ sở hữu.\n"
            f"🆔 Chat ID của bạn: {update.effective_chat.id}"
        )


HELP_TEXT = (
    "🎬 STUDIO KỊCH BẢN — BOT TELEGRAM\n\n"
    "1️⃣ Gửi 1 câu prompt, ví dụ:\n"
    "   “3 sự thật kinh hoàng về giấc ngủ”\n"
    "2️⃣ Mình viết kịch bản phân cảnh 3-Band để bạn duyệt\n"
    "3️⃣ Bấm “🎬 Dựng video ngay” → nhận video 9:16 tại đây\n\n"
    "⚠️ Dựng video tốn GPU — vui lòng chờ vài phút.\n"
    "Web bản đầy đủ: http://127.0.0.1:8088/kich-ban"
)


# ===== LUỒNG CHÍNH =====
async def generate_and_show_script(
    chat_id: int,
    prompt: str,
    status_message,
    settings: Dict[str, Any],
) -> None:
    engine = str(settings.get("engine") or "kinetic")
    voice = str(settings.get("voice") or DEFAULT_VOICE)
    payload = {
        "prompt": clip(prompt, MAX_PROMPT_CHARS),
        "scene_count": int(settings.get("scene_count") or 12),
        "ai_engine": DEFAULT_AI_ENGINE,
    }
    try:
        http_status, data = await asyncio.to_thread(
            api_post, "/api/script/ai-generate-script", payload, 900
        )
        if http_status != 200 or data.get("status") != "success":
            reason = one_line(data.get("message") or f"HTTP {http_status}", 400)
            await safe_edit(status_message, f"❌ Không sinh được kịch bản:\n{reason}")
            return
        script = data.get("data") or {}
        if not script.get("scenes"):
            await safe_edit(status_message, "❌ AI trả về kịch bản rỗng. Bạn thử mô tả rõ hơn nhé!")
            return
        PENDING_SCRIPTS[chat_id] = {"script": script, "prompt": prompt}
        text = format_script_preview(script, engine, voice_name(voice))
        await safe_edit(status_message, text, reply_markup=preview_keyboard())
    except Exception as exc:
        logger.exception("Sinh kịch bản thất bại")
        await safe_edit(status_message, f"❌ Lỗi khi gọi AI: {one_line(exc, 300)}")


async def poll_render(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    task_id: str,
    status_message,
    caption: str = "",
) -> None:
    started = time.time()
    last_text = ""
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            if time.time() - started > RENDER_TIMEOUT_SECONDS:
                await safe_edit(status_message, "⌛ Quá 90 phút chưa xong — bạn kiểm tra trên web giúp mình nhé!")
                return
            try:
                data = await asyncio.to_thread(
                    api_get, f"/api/script/pipeline/progress/{task_id}", 15
                )
            except Exception as exc:
                logger.warning("Lỗi đọc tiến độ: %s", exc)
                continue

            status = data.get("status")
            if status in (None, "unknown", "success"):
                continue
            if status == "completed":
                await deliver_video(context, chat_id, data.get("result") or {}, status_message, caption)
                return
            if status == "error":
                reason = one_line(data.get("error") or data.get("step_name") or "Lỗi không rõ", 400)
                await safe_edit(status_message, f"❌ Render thất bại:\n{reason}")
                return

            percent = int(data.get("percent") or 0)
            step = one_line(data.get("step_name") or "Đang dựng...", 200)
            text = f"🎬 Đang dựng... {percent}%\n{step}"
            if text != last_text:
                last_text = text
                await safe_edit(status_message, text)
    finally:
        ACTIVE_RENDERS.pop(chat_id, None)


async def deliver_video(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    result: Dict[str, Any],
    status_message,
    caption: str = "",
) -> None:
    video_path = str(result.get("video_path") or "")
    if not video_path or not Path(video_path).is_file():
        await safe_edit(
            status_message,
            "✅ Render xong nhưng không tìm thấy file video."
            + (f"\n📁 {video_path}" if video_path else ""),
        )
        return
    path = Path(video_path)
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        size_mb = 0.0
    saved_path = str(result.get("copied_to") or "")
    saved_line = f"\n💾 Đã lưu ổ D: {saved_path}" if saved_path else ""
    await safe_edit(status_message, f"✅ Video xong! Đang gửi lên Telegram... ({size_mb:.1f} MB)")
    caption_text = clip(caption or path.stem, 200)
    try:
        await context.bot.send_video(
            chat_id=chat_id,
            video=InputFile(str(path)),
            caption=caption_text,
            supports_streaming=True,
        )
        await safe_edit(status_message, f"✅ Đã gửi video: {path.name}\n📁 {path}{saved_line}")
    except Exception as exc:
        logger.warning("send_video thất bại: %s — thử gửi dạng file", exc)
        try:
            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(str(path)),
                caption=caption_text,
            )
            await safe_edit(status_message, f"✅ Đã gửi (dạng file): {path.name}{saved_line}")
        except Exception as exc2:
            logger.error("send_document thất bại: %s", exc2)
            await safe_edit(
                status_message,
                f"✅ Render xong nhưng gửi Telegram lỗi: {one_line(exc2, 200)}\n📁 {path}",
            )


async def start_render(context: ContextTypes.DEFAULT_TYPE, chat_id: int, query) -> None:
    if chat_id in ACTIVE_RENDERS:
        await context.bot.send_message(
            chat_id, "⏳ Bạn đang có 1 video dựng dở — chờ xong rồi dựng tiếp nhé!"
        )
        return
    pending = PENDING_SCRIPTS.get(chat_id)
    if not pending:
        await context.bot.send_message(chat_id, "✏️ Hãy gửi prompt để mình viết kịch bản trước nhé!")
        return

    script = pending.get("script") or {}
    settings = get_settings(chat_id)
    engine = str(settings.get("engine") or "kinetic")
    voice = str(settings.get("voice") or DEFAULT_VOICE)
    use_hand_draw, mode = engine_params(engine)
    task_id = f"tgbot_{chat_id}_{int(time.time())}"
    payload = {
        "title": script.get("title") or "VIDEO KỊCH BẢN",
        "keyword_accent": script.get("keyword_accent") or "",
        "scenes": script.get("scenes") or [],
        "voice": voice,
        "task_id": task_id,
        "use_hand_draw": use_hand_draw,
        "whiteboard_mode": mode,
    }
    try:
        http_status, data = await asyncio.to_thread(
            api_post, "/api/script/render-doodle-scenes", payload, 60
        )
    except Exception as exc:
        await context.bot.send_message(chat_id, f"❌ Không gọi được hệ thống render: {one_line(exc, 300)}")
        return

    if http_status == 423:
        await context.bot.send_message(
            chat_id,
            "🚫 Hệ thống đang dựng một video khác (có thể từ web). Chờ chút rồi bấm lại nhé!",
        )
        return
    if http_status != 200 or data.get("status") != "processing":
        reason = one_line(data.get("message") or f"HTTP {http_status}", 400)
        await context.bot.send_message(chat_id, f"❌ Không khởi động được render:\n{reason}")
        return

    ACTIVE_RENDERS[chat_id] = task_id
    status_message = await context.bot.send_message(
        chat_id, f"🎬 Đang dựng video ({engine_label(engine)})... 0%"
    )
    caption = (
        f"🎬 {script.get('title') or 'VIDEO KỊCH BẢN'}\n"
        f"{len(script.get('scenes') or [])} cảnh · {engine_label(engine)} · 🔊 {voice_name(voice)}"
    )
    context.application.create_task(poll_render(context, chat_id, task_id, status_message, caption))


# ===== HANDLERS =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    await update.message.reply_text(HELP_TEXT + f"\n\n🆔 Chat ID của bạn: {chat_id}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    settings = get_settings(chat_id)
    await update.message.reply_text(settings_text(settings), reply_markup=settings_keyboard(settings))


async def cmd_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    settings = get_settings(chat_id)
    args = context.args or []
    if args:
        settings["engine"] = engine_key(" ".join(args))
        await update.message.reply_text(
            f"✅ Phong cách: {engine_label(settings['engine'])}",
            reply_markup=settings_keyboard(settings),
        )
        return
    await update.message.reply_text(settings_text(settings), reply_markup=settings_keyboard(settings))


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    settings = get_settings(chat_id)
    args = context.args or []
    if args:
        candidate = " ".join(args).strip()
        voices = await asyncio.to_thread(get_voices_cached)
        matched = next((v for v in voices if str(v.get("id")) == candidate or str(v.get("name")) == candidate), None)
        if matched:
            settings["voice"] = str(matched.get("id"))
            await update.message.reply_text(
                f"✅ Giọng đọc: {matched.get('name')}",
                reply_markup=settings_keyboard(settings),
            )
        else:
            await update.message.reply_text(
                "❓ Không khớp giọng nào. Bấm nút trong bảng cài đặt để chọn:",
                reply_markup=settings_keyboard(settings),
            )
        return
    await update.message.reply_text(settings_text(settings), reply_markup=settings_keyboard(settings))


async def cmd_scenes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    settings = get_settings(chat_id)
    args = context.args or []
    if args and args[0].isdigit():
        settings["scene_count"] = max(2, min(30, int(args[0])))
        await update.message.reply_text(
            f"✅ Số cảnh: {settings['scene_count']} (áp dụng cho prompt tiếp theo)",
            reply_markup=settings_keyboard(settings),
        )
        return
    await update.message.reply_text(settings_text(settings), reply_markup=settings_keyboard(settings))


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"🆔 Chat ID của bạn: {update.effective_chat.id}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    try:
        await asyncio.to_thread(api_post, "/api/script/stop", {}, 30)
        await update.message.reply_text("🛑 Đã gửi lệnh dừng tiến trình render hiện tại (nếu có).")
    except Exception as exc:
        await update.message.reply_text(f"❌ Không gửi được lệnh dừng: {one_line(exc, 200)}")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    prompt = (update.message.text or "").strip()
    if not prompt:
        return
    settings = get_settings(chat_id)
    status_message = await update.message.reply_text(
        "🧠 AI đang viết kịch bản phân cảnh... (thường 10–60 giây)"
    )
    context.application.create_task(generate_and_show_script(chat_id, prompt, status_message, settings))


async def on_non_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not is_allowed(chat_id):
        await deny(update)
        return
    await update.message.reply_text(
        "✏️ Mình chỉ hiểu tin nhắn văn bản — bạn gửi câu mô tả video nhé!"
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.message is None:
        return
    chat_id = query.message.chat_id
    if not is_allowed(chat_id):
        await query.answer("⛔ Không có quyền.", show_alert=True)
        return
    await query.answer()
    data = query.data or ""
    settings = get_settings(chat_id)

    if data.startswith("eng:"):
        settings["engine"] = engine_key(data[4:])
        await safe_edit(query.message, settings_text(settings), reply_markup=settings_keyboard(settings))
    elif data.startswith("scenes:"):
        value = data[7:]
        if value.isdigit():
            settings["scene_count"] = max(2, min(30, int(value)))
        await safe_edit(query.message, settings_text(settings), reply_markup=settings_keyboard(settings))
    elif data.startswith("voice:"):
        settings["voice"] = data[6:]
        await safe_edit(query.message, settings_text(settings), reply_markup=settings_keyboard(settings))
    elif data == "act:settings":
        await safe_edit(query.message, settings_text(settings), reply_markup=settings_keyboard(settings))
    elif data == "act:regen":
        pending = PENDING_SCRIPTS.get(chat_id)
        if not pending:
            await context.bot.send_message(chat_id, "✏️ Hãy gửi prompt mới nhé!")
            return
        await safe_edit(query.message, "🧠 Đang viết lại kịch bản...")
        await generate_and_show_script(chat_id, str(pending.get("prompt") or ""), query.message, settings)
    elif data == "act:render":
        await start_render(context, chat_id, query)


async def _post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands(
            [
                ("start", "Hướng dẫn sử dụng"),
                ("settings", "Cài đặt phong cách, số cảnh, giọng đọc"),
                ("stop", "Dừng render đang chạy"),
                ("chatid", "Lấy Chat ID của bạn"),
            ]
        )
    except Exception as exc:
        logger.warning("set_my_commands thất bại: %s", exc)


def get_bot_token() -> str:
    value = os.getenv("TELEGRAM_SCRIPT_BOT_TOKEN", "").strip()
    if value.upper().startswith(("YOUR_", "PASTE_")):
        return ""
    return value


ALLOWED_CHAT_IDS = parse_allowlist(os.getenv("TELEGRAM_SCRIPT_ALLOWED_CHAT_IDS", ""))


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = getattr(context, "error", None)
    err_name = type(err).__name__ if err is not None else "UnknownError"
    err_text = str(err or "")
    if any(k in err_name for k in ("NetworkError", "ReadError", "ConnectError", "TimedOut", "RemoteProtocolError")) or "httpx." in err_text:
        logger.warning("Kết nối Telegram Script Bot tạm gián đoạn (tự động thử lại): %s: %s", err_name, err_text)
        return
    logger.error("Lỗi xử lý sự kiện trên Telegram Script Bot (%s): %s", err_name, err_text, exc_info=err)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx ghi log INFO sẽ chèn cả token vào URL mỗi vòng getUpdates — hạ xuống WARNING cho sạch & kín log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")

    token = get_bot_token()
    if not token:
        logger.error("Thiếu TELEGRAM_SCRIPT_BOT_TOKEN trong backend/.env — bot không thể chạy.")
        raise SystemExit(2)
    autodub_token = os.getenv("BOT_TOKEN", "").strip()
    if autodub_token and token == autodub_token:
        logger.error("TELEGRAM_SCRIPT_BOT_TOKEN đang trùng BOT_TOKEN của bot AutoDub — dừng để tránh xung đột.")
        raise SystemExit(3)

    logger.info(
        "Khởi động bot Studio Kịch Bản — API %s — allowlist: %s",
        API_BASE,
        ", ".join(str(i) for i in sorted(ALLOWED_CHAT_IDS)) or "mở (mọi người)",
    )
    app = Application.builder().token(token).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("engine", cmd_engine))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("scenes", cmd_scenes))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_handler(MessageHandler(~filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_non_text))
    app.add_error_handler(_on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
