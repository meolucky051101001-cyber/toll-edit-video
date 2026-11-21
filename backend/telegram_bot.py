"""
Telegram Bot - Auto Video Dubbing
Gửi link video → Bot tự động tải, tạo phụ đề, lồng tiếng, gửi lại video.
"""
import errno
import os
import sys
import asyncio
import time
import subprocess
from pathlib import Path

# CẤP CỨU: Chặn VĨNH VIỄN tất cả các cửa sổ terminal (cmd) đen nháy lên do các thư viện bên thứ 3 (Whisper, PyDub, OCR) gọi ngầm ffmpeg.
if os.name == 'nt':
    original_init = subprocess.Popen.__init__
    def patched_init(self, *args, **kwargs):
        if hasattr(subprocess, 'CREATE_NO_WINDOW'):
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        if 'startupinfo' not in kwargs:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs['startupinfo'] = startupinfo
        original_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = patched_init


class BotInstanceAlreadyRunning(RuntimeError):
    """Raised only when the existing instance lock is contended."""


class BotStartupError(RuntimeError):
    """Raised when startup cannot inspect or create its required lock."""


def _startup_failure_message(component, operation, path, exc):
    details = [
        f"component={component}",
        f"operation={operation}",
        f"path={path}",
        f"error_type={type(exc).__name__}",
    ]
    for attr in ("errno", "winerror"):
        value = getattr(exc, attr, None)
        if value is not None:
            details.append(f"{attr}={value}")
    return "Startup failed: " + " ".join(details)


def _emit_startup_diagnostic(message):
    print(message, file=sys.stderr, flush=True)


def _install_bot_startup_fallback():
    startup_logger = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    handler._autodub_startup_stderr = True
    startup_logger.addHandler(handler)
    startup_logger.setLevel(logging.INFO)
    return handler


def _acquire_bot_instance_lock(lock_file_path, *, opener=open, lock_acquirer=None):
    try:
        lock_file = opener(lock_file_path, "w")
    except Exception as exc:
        raise BotStartupError(
            _startup_failure_message("telegram", "open instance lock", lock_file_path, exc)
        ) from exc

    try:
        if lock_acquirer is None:
            import msvcrt
            lock_acquirer = lambda file_obj: msvcrt.locking(
                file_obj.fileno(), msvcrt.LK_NBLCK, 1
            )
        lock_acquirer(lock_file)
    except OSError as exc:
        try:
            lock_file.close()
        except Exception:
            pass
        message = _startup_failure_message(
            "telegram", "acquire instance lock", lock_file_path, exc
        )
        if getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, "winerror", None) in (32, 33):
            raise BotInstanceAlreadyRunning(message) from exc
        raise BotStartupError(message) from exc
    except Exception as exc:
        try:
            lock_file.close()
        except Exception:
            pass
        raise BotStartupError(
            _startup_failure_message("telegram", "acquire instance lock", lock_file_path, exc)
        ) from exc
    return lock_file


import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from core.logging_setup import RedactingFormatter, setup_logging

# Load .env local file
from environment import load_environment
load_environment(Path(__file__).resolve().parent)

# This entrypoint belongs to the dedicated Tool V1 branch. Never let a stale
# machine-level variable route its jobs into Pipeline V2.
os.environ["PIPELINE_MODE"] = "legacy"

# ===== CẤU HÌNH =====
def configured_secret(name):
    value = os.getenv(name, "").strip()
    if value.upper().startswith(("YOUR_", "PASTE_")):
        return ""
    return value


BOT_TOKEN = configured_secret("BOT_TOKEN")
GEMINI_API_KEY = configured_secret("GEMINI_API_KEY")
FPT_API_KEY = configured_secret("FPT_API_KEY")

# Import các module xử lý từ backend
sys.path.insert(0, os.path.dirname(__file__))
from telegram_access import install_access_gate, load_allowed_chat_ids
import job_tracker
from config.paths import is_downloader_save_dir
from pipeline_v2.atomic_io import atomic_copy_file, atomic_write_json

# Đảm bảo console hỗ trợ UTF-8 để không bị lỗi UnicodeEncodeError
import io
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding='utf-8')
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding='utf-8')

from url_utils import extract_http_urls

WORKSPACE = os.path.abspath(
    os.getenv(
        "AUTODUB_WORKSPACE",
        os.path.join(os.path.dirname(__file__), "..", "workspace"),
    )
)
INPUT_DIR = os.path.abspath(os.getenv("AUTODUB_INPUT_DIR", r"D:\video_input"))
OUTPUT_DIR = os.path.abspath(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve"))
os.makedirs(WORKSPACE, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
root_app_log = os.path.abspath(os.path.join(BASE_DIR, "..", "app.log"))
backend_app_log = os.path.abspath(os.path.join(BASE_DIR, "app.log"))
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


def _configure_bot_logging() -> None:
    setup_logging(
        "telegram_bot",
        [backend_app_log, root_app_log],
        force=True,
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("=== AUTO VIDEO DUBBING TOOL V1 STARTED === [PID: %d] [CODEX SINGLE VOICE LOCK ACTIVE]", os.getpid())

async def safe_edit_status(status_msg, text, parse_mode=None, retries=3):
    """
    Cập nhật status message trên Telegram an toàn, chống bị crash tiến trình
    khi mạng Internet bị giật hoặc đứt kết nối tạm thời (httpx.ConnectError).
    """
    from telegram_progress import report
    report(text)
    if not status_msg:
        return
    for attempt in range(retries):
        try:
            await status_msg.edit_text(text, parse_mode=parse_mode)
            return
        except Exception as e:
            if parse_mode:
                try:
                    await status_msg.edit_text(text)
                    return
                except Exception:
                    pass
            logger.warning(f"Lỗi cập nhật status Telegram (Lần {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1.0)


async def run_pipeline_v2_for_telegram(
    video_path, out_dir, final_video, status_msg, delivery_copy_path=None
):
    """Run the opt-in v2 pipeline while legacy remains the default."""

    from pipeline_v2.config import PipelineSettings
    from pipeline_v2.video_pipeline import VideoPipelineRequest, VideoPipelineRunner

    settings = PipelineSettings.from_env()

    async def progress(stage, state):
        await safe_edit_status(
            status_msg,
            "⚙️ Pipeline v2: `{}` — {}".format(stage, state),
            parse_mode="Markdown",
        )

    request = VideoPipelineRequest(
        video_path=Path(video_path),
        job_directory=Path(out_dir),
        output_path=Path(final_video),
        delivery_copy_path=(
            Path(delivery_copy_path) if delivery_copy_path else None
        ),
        settings=settings,
        api_key=GEMINI_API_KEY,
        voice_source="capcut",
        voice_param="BV562_streaming",
        progress=progress,
    )
    return await VideoPipelineRunner(request).run()


try:
    from .telegram_jobs import TelegramJobPaths, build_v2_completion_caption
except ImportError:
    try:
        from telegram_jobs import TelegramJobPaths, build_v2_completion_caption
    except ImportError:
        TelegramJobPaths = None
        build_v2_completion_caption = None


async def process_v2_telegram_job(
    video_path,
    paths,
    title,
    status_msg,
    context=None,
    chat_id=None,
    url_or_filename=None,
):
    """Run and report the shared production V2 path for every Telegram input."""
    started_at = time.time()
    await run_pipeline_v2_for_telegram(
        video_path,
        str(paths.job_directory),
        str(paths.final_video),
        status_msg,
        delivery_copy_path=str(paths.delivery_copy),
    )
    elapsed_seconds = time.time() - started_at
    try:
        from render_history import record_render_duration
        record_render_duration(str(paths.delivery_copy), elapsed_seconds)
    except Exception as exc:
        logger.warning("Không thể ghi nhận render_history: %s", exc)
    final_video = paths.final_video
    if not Path(final_video).is_file() and paths.delivery_copy and Path(paths.delivery_copy).is_file():
        final_video = paths.delivery_copy

    saved_file = paths.delivery_copy if paths.delivery_copy and Path(paths.delivery_copy).is_file() else final_video
    caption = build_v2_completion_caption(
        title=title,
        output_directory=OUTPUT_DIR,
        elapsed_seconds=elapsed_seconds,
        remaining_jobs=global_queue.qsize(),
        saved_file_path=str(saved_file) if saved_file and Path(saved_file).is_file() else "",
    ) if build_v2_completion_caption else f"✅ Hoàn tất: {title} (đã lưu vào máy: {saved_file})"

    upload_to_telegram = os.getenv("AUTODUB_TELEGRAM_UPLOAD_VIDEO", "false").lower() in ("true", "1", "yes")

    if upload_to_telegram and context and chat_id and Path(final_video).is_file():
        await send_video_safely(
            context,
            chat_id,
            str(final_video),
            caption,
            status_msg,
            url_or_filename or title,
        )
    else:
        if status_msg:
            await safe_edit_status(status_msg, caption, parse_mode="Markdown")
        elif context and chat_id:
            try:
                await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="Markdown")
            except Exception as exc:
                logger.warning("Không thể gửi thông báo hoàn tất qua Telegram: %s", exc)


async def run_durable_video(job):
    """Run video job for durable queue adapter."""
    context = job.get('context')
    update = job.get('update')
    job_key = job.get('job_key') or 'queue_job'
    video = job.get('video_path')
    paths = TelegramJobPaths.create(WORKSPACE, OUTPUT_DIR, job_key) if TelegramJobPaths else None
    if paths:
        paths.prepare_directories()
    resolved_chat_id = job.get('chat_id')
    if not resolved_chat_id and update and getattr(update, 'effective_chat', None):
        resolved_chat_id = update.effective_chat.id
    if not resolved_chat_id and update and getattr(update, 'message', None) and getattr(update.message, 'chat_id', None):
        resolved_chat_id = update.message.chat_id
    await process_v2_telegram_job(
        video,
        paths,
        job.get('filename') or (Path(video).name if video else "video"),
        None,
        context=context,
        chat_id=resolved_chat_id,
        url_or_filename=job.get('url') or job.get('filename'),
    )


def publish_legacy_telegram_output(final_video, base_name, video_name, duration_seconds):

    """Atomically publish a rendered Telegram V1 video before validating completion."""
    destination = Path(OUTPUT_DIR) / f"Dubbed_{base_name}.mp4"
    atomic_copy_file(final_video, destination)
    job_tracker.finish_video(video_name, str(destination), duration_seconds)
    return destination

# ===== LỆNH /start =====
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome = (
        "🎬 *Auto Video Dubbing Bot*\n\n"
        "Gửi cho tôi link video từ bất kỳ nền tảng nào:\n"
        "• Xiaohongshu (小红书)\n"
        "• TikTok\n"
        "• YouTube\n"
        "• Douyin\n"
        "• Facebook / Instagram\n\n"
        "Bot sẽ tự động:\n"
        "1️⃣ Tải video sạch không watermark\n"
        "2️⃣ Nhận dạng giọng nói (Faster-Whisper Large-v3 Turbo)\n"
        "3️⃣ Dịch phụ đề sang Tiếng Việt (Gemini 3.8 Flash)\n"
        "4️⃣ Tách giọng & giữ nhạc nền (Demucs htdemucs Fast)\n"
        "5️⃣ Lồng tiếng Tiếng Việt (Microsoft Neural TTS)\n"
        "6️⃣ Xuất video chất lượng cao lưu vào `D:\\banve`\n\n"
        "📌 *Lệnh hỗ trợ:*\n"
        "• `/cloud` - ☁️ Xem trạng thái & điều khiển chế độ Cloud GPU (Colab T4/L4)\n"
        "• `/cloud on` / `/cloud off` - Bật/tắt xử lý tự động trên Cloud GPU\n"
        "• `/cloud <link hoặc file>` - Ép 1 video xử lý trên Cloud GPU ngay lập tức\n"
        "• `/cloud_status` - Tra cứu số dư Compute Units (CU) Colab\n"
        "• `/muse <ý tưởng>` - ⚡ Tạo video Whiteboard/Doodle bằng Meta Muse Spark AI\n"
        "• `/voice_auto [on|off]` - Bật/tắt tự nhận diện giọng nói đầu video (Tool V1)\n"
        "• `/sub_tts [on|off]` - Bật/tắt chế độ đọc sub hình ảnh thành giọng nói (video không lời thoại)\n"
        "• `/llm` - Cấu hình mô hình AI dịch thuật (Google Gemini / OpenAI GPT-4o / DeepSeek V4)\n"
        "• `/batch` - Tự động quét & edit hàng loạt video trong thư mục `D:\\video_input` trên máy\n"
        "• `/batch D:\\thu_muc` - Chỉ định thư mục chứa video cần edit\n"
        "• `/status` - Kiểm tra trạng thái hàng đợi\n"
        "• `/stop` - Dừng khẩn cấp toàn bộ tác vụ"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = getattr(update, "effective_chat", None)
    message = getattr(update, "effective_message", None)
    if message is not None:
        await message.reply_text(f"Chat ID của bạn: {getattr(chat, 'id', None)}")


# ===== LỆNH /sub_tts (Opt-in: OCR sub tiếng Trung trên hình -> TTS) =====
async def cmd_sub_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Bật/tắt chế độ quét phụ đề tiếng Trung gắn cứng trên hình -> dịch & lồng tiếng TTS.
    Cú pháp:
    /sub_tts on   - Bật (áp dụng cho video thêm vào hàng đợi từ bây giờ)
    /sub_tts off  - Tắt
    /sub_tts      - Xem trạng thái hiện tại
    """
    from v1_feature_flags import get_feature_flags, set_feature_flags
    args = context.args
    try:
        enabled = bool(get_feature_flags(WORKSPACE).get("V1_VISUAL_SUB_TO_SPEECH", False))
        if not args:
            state = "🟢 ĐANG BẬT" if enabled else "⚪ ĐANG TẮT"
            await update.message.reply_text(
                f"🎬 *SUB HÌNH ẢNH ➔ GIỌNG NÓI (OCR-to-TTS)*\n\n"
                f"📍 *Trạng thái:* {state}\n\n"
                f"Chế độ dành cho video không có lời thoại (chỉ có chữ tiếng Trung trên hình): "
                f"hệ thống quét OCR phụ đề, tạo file SRT có tọa độ khung che, dịch sang tiếng Việt và lồng tiếng TTS.\n\n"
                f"👉 `/sub_tts on` - Bật\n"
                f"👉 `/sub_tts off` - Tắt\n"
                f"*(Alias: `/ocr_tts on|off`)*\n\n"
                f"📌 Trạng thái được đóng băng theo từng job ngay lúc video vào hàng đợi. "
                f"Video đang chạy/đã xếp hàng giữ nguyên cấu hình lúc gửi.",
                parse_mode="Markdown"
            )
            return

        subcmd = args[0].strip().lower()
        if subcmd in ("on", "1", "enable", "bat", "bật"):
            set_feature_flags({"V1_VISUAL_SUB_TO_SPEECH": True}, WORKSPACE)
            await update.message.reply_text(
                "✅ *ĐÃ BẬT CHẾ ĐỘ SUB HÌNH ẢNH ➔ GIỌNG NÓI!*\n\n"
                "• Video không có lời thoại (hoặc chỉ có nhạc nền) sẽ được quét phụ đề tiếng Trung trên màn hình.\n"
                "• Phụ đề khôi phục giữ đầy đủ tọa độ khung che trắng, được dịch và lồng tiếng TTS.\n\n"
                "*(Áp dụng cho video gửi từ bây giờ. Video đang chạy/trong hàng đợi giữ nguyên chế độ cũ)*",
                parse_mode="Markdown"
            )
        elif subcmd in ("off", "0", "disable", "tat", "tắt"):
            set_feature_flags({"V1_VISUAL_SUB_TO_SPEECH": False}, WORKSPACE)
            await update.message.reply_text(
                "⚪ *ĐÃ TẮT CHẾ ĐỘ SUB HÌNH ẢNH ➔ GIỌNG NÓI!*\n\n"
                "• Tool V1 trở về luồng xử lý video có lời thoại như cũ.\n"
                "• Video không có giọng nói sẽ dừng với lỗi ASR như trước đây.\n\n"
                "*(Áp dụng cho video gửi từ bây giờ. Video đang chạy/trong hàng đợi giữ nguyên chế độ cũ)*",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "❌ Cú pháp không hợp lệ. Vui lòng dùng:\n"
                "• `/sub_tts on` để bật\n"
                "• `/sub_tts off` để tắt\n"
                "• `/sub_tts` để xem trạng thái",
                parse_mode="Markdown"
            )
    except Exception as exc:
        logger.error("Lỗi lưu cấu hình V1_VISUAL_SUB_TO_SPEECH: %s", exc)
        await update.message.reply_text(
            f"❌ Không thể lưu cấu hình Sub Hình Ảnh: {exc}"
        )


# ===== LỆNH /voice_auto (Codex Plan - Chế độ nhận diện giọng đầu video) =====
async def cmd_voice_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Điều khiển chế độ tự động nhận diện giọng nói đầu video (Tool V1).
    Cú pháp:
    /voice_auto on   - Bật nhận diện giọng đầu video
    /voice_auto off  - Tắt nhận diện, dùng giọng đã chọn thủ công
    /voice_auto      - Xem trạng thái hiện tại
    """
    from ai.v1_auto_voice import (
        get_auto_voice_config,
        get_auto_voice_enabled,
        set_auto_voice_enabled,
        get_manual_voice_info,
    )
    import voice_selection
    args = context.args
    manual_info = get_manual_voice_info(WORKSPACE)
    manual_label = manual_info.get("label", "Chưa xác định")

    catalog = voice_selection.catalog()
    cfg = get_auto_voice_config(WORKSPACE)
    female_v = next((v for v in catalog if v.get("id") == cfg.get("female_voice_id")), None)
    male_v = next((v for v in catalog if v.get("id") == cfg.get("male_voice_id")), None)
    female_label = female_v["label"] if female_v else "CapCut · Mai"
    male_label = male_v["label"] if male_v else "Thanh Niên Tự Tin (CapCut)"

    if not args:
        enabled = get_auto_voice_enabled(WORKSPACE)
        mode_str = "🟢 BẬT (Tự động nhận diện)" if enabled else "⚪ TẮT (Dùng giọng thủ công)"
        await update.message.reply_text(
            f"🎙️ *CẤU HÌNH NHẬN DIỆN GIỌNG NÓI (TOOL V1)*\n\n"
            f"📍 *Trạng thái hiện tại:* {mode_str}\n"
            f"👩 *Giọng khi nhận diện Nữ:* `{female_label}`\n"
            f"👨 *Giọng khi nhận diện Nam:* `{male_label}`\n"
            f"🎯 *Giọng thủ công dự phòng:* `{manual_label}`\n\n"
            f"📋 *Quy tắc hoạt động:*\n"
            f"• *Bật (`on`):*\n"
            f"  - Người nói đầu là Nữ ➡️ {female_label}\n"
            f"  - Người nói đầu là Nam ➡️ {male_label}\n"
            f"  - Không xác định rõ ➡️ Giọng thủ công (`{manual_label}`)\n"
            f"• *Tắt (`off`):*\n"
            f"  - Toàn bộ video dùng giọng thủ công (`{manual_label}`)\n"
            f"  - Bỏ qua phân tích âm học F0 (tiết kiệm thời gian)\n\n"
            f"📌 *Phạm vi áp dụng:* Lệnh có hiệu lực với các video gửi SAU lệnh. "
            f"Video đang render hoặc đã trong hàng đợi giữ nguyên chế độ lúc gửi.\n\n"
            f"👉 *Lệnh điều khiển:*\n"
            f"• `/voice_auto on` - Bật tự động nhận diện\n"
            f"• `/voice_auto off` - Tắt nhận diện, dùng giọng thủ công\n"
            f"*(Để đổi giọng Nữ/Nam cụ thể, bạn có thể chọn trực tiếp trên giao diện Dashboard)*",
            parse_mode="Markdown"
        )
        return

    subcmd = args[0].strip().lower()
    user_name = update.effective_user.username or update.effective_user.first_name if update.effective_user else "telegram_user"

    if subcmd in ("on", "1", "enable", "bat", "bật"):
        ok = set_auto_voice_enabled(True, updated_by=user_name, workspace=WORKSPACE)
        if not ok:
            await update.message.reply_text("❌ Lỗi hệ thống: Không thể lưu cấu hình Auto Voice vào ổ đĩa.")
            return
        await update.message.reply_text(
            f"✅ *ĐÃ BẬT CHẾ ĐỘ NHẬN DIỆN GIỌNG NÓI TỰ ĐỘNG!*\n\n"
            f"• Video mới gửi sẽ tự động chọn giọng theo người nói đầu tiên:\n"
            f"  👩 Nữ ➡️ {female_label}\n"
            f"  👨 Nam ➡️ {male_label}\n"
            f"  ❓ Không rõ ➡️ Dùng giọng thủ công (`{manual_label}`)\n\n"
            f"*(Áp dụng cho các video gửi từ bây giờ. Video đang chạy/trong hàng đợi giữ nguyên chế độ cũ)*",
            parse_mode="Markdown"
        )
    elif subcmd in ("off", "0", "disable", "tat", "tắt"):
        ok = set_auto_voice_enabled(False, updated_by=user_name, workspace=WORKSPACE)
        if not ok:
            await update.message.reply_text("❌ Lỗi hệ thống: Không thể lưu cấu hình Auto Voice vào ổ đĩa.")
            return
        await update.message.reply_text(
            f"✅ *ĐÃ TẮT CHẾ ĐỘ NHẬN DIỆN GIỌNG NÓI TỰ ĐỘNG!*\n\n"
            f"• Video mới gửi sẽ dùng cố định giọng thủ công: `{manual_label}`\n"
            f"• Bỏ qua hoàn toàn bước phân tích âm học F0/HNR (thời gian phân tích = 0s).\n\n"
            f"*(Áp dụng cho các video gửi từ bây giờ. Video đang chạy/trong hàng đợi giữ nguyên chế độ cũ)*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Cú pháp không hợp lệ. Vui lòng dùng:\n"
            "• `/voice_auto on` để bật\n"
            "• `/voice_auto off` để tắt\n"
            "• `/voice_auto` để xem trạng thái",
            parse_mode="Markdown"
        )


# ===== LỆNH /muse, /spark, /video (Tạo video Whiteboard/Doodle bằng Meta Muse Spark AI) =====
async def cmd_muse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tự động biên soạn kịch bản và xuất video Whiteboard/Doodle 9:16 bằng Meta Muse Spark AI.
    Cú pháp:
    /muse <ý tưởng hoặc chủ đề>
    /spark <ý tưởng hoặc chủ đề>
    /video <ý tưởng hoặc chủ đề>
    """
    user_prompt = " ".join(context.args).strip() if context.args else ""
    if not user_prompt:
        help_text = (
            "✨ *MUSE SPARK BOT - TỰ ĐỘNG LÀM VIDEO WHITEBOARD 9:16*\n\n"
            "Chỉ cần gửi một câu ý tưởng hoặc chủ đề, bot sẽ tự động làm trọn gói từ A-Z!\n\n"
            "📌 *Cú pháp:*\n"
            "• `/muse <ý tưởng video>`\n"
            "• `/spark <ý tưởng video>`\n"
            "• `/video <ý tưởng video>`\n\n"
            "💡 *Ví dụ câu lệnh:*\n"
            "• `/muse Tại sao cá voi sát thủ không bao giờ tấn công con người?`\n"
            "• `/muse 3 thói quen buổi sáng giúp người giàu nhân đôi tài sản`\n"
            "• `/muse Vì sao loài mèo lại thích chui vào hộp carton?`\n\n"
            "🚀 *Quy trình tự động hóa:*\n"
            "1️⃣ *Meta Muse Spark AI* viết kịch bản 9-Move Arc triệu view chuẩn tâm lý học.\n"
            "2️⃣ Tự động ghép nối Vector Whiteboard / Icons (Iconify 200,000+ icons) khớp từng câu thoại.\n"
            "3️⃣ Thu âm thuyết minh bằng giọng đọc CapCut AI / Edge Neural TTS.\n"
            "4️⃣ Xuất video 9:16 chuẩn TikTok/Reels/Shorts gửi trực tiếp vào Telegram và lưu tại `D:\\banve`."
        )
        await update.message.reply_text(help_text, parse_mode="Markdown")
        return

    chat_id = update.effective_chat.id
    status_msg = await update.message.reply_text(
        f"⚡ *MUSE SPARK BOT ĐÃ TIẾP NHẬN Ý TƯỞNG!*\n\n"
        f"📝 *Chủ đề:* _{user_prompt}_\n\n"
        f"⏳ *Bước 1/3:* Meta Muse Spark đang biên soạn kịch bản 9-Move Arc...",
        parse_mode="Markdown"
    )

    try:
        import shutil
        from ai.prompt_to_script import generate_doodle_script_from_ai
        from ai.doodle_engine import render_doodle_video

        # 1. Sinh kịch bản qua AI (chạy trong thread để không block async loop)
        script_data = await asyncio.to_thread(
            generate_doodle_script_from_ai,
            prompt=user_prompt,
            ai_engine="gemini"
        )

        title = script_data.get("title", "VIDEO GIẢI MÃ BÍ ẨN")
        scenes = script_data.get("scenes", [])
        engine_used = script_data.get("ai_engine_used", "muse-spark-1.3")

        await safe_edit_status(
            status_msg,
            f"🎬 *Đã lên xong kịch bản:* *{title}*\n"
            f"📊 Số phân cảnh: *{len(scenes)} cảnh*\n"
            f"🤖 Engine: `{engine_used}`\n\n"
            f"⏳ *Bước 2/3:* Đang thu âm giọng đọc CapCut AI & vẽ khung hình Whiteboard...",
            parse_mode="Markdown"
        )

        # 2. Render video Doodle (chạy trong thread)
        render_result = await asyncio.to_thread(
            render_doodle_video,
            script_data=script_data,
            voice_id="multi_male_felipe_uranus_bigtts"
        )

        video_path = render_result.get("video_path")
        if not video_path or not os.path.exists(video_path):
            raise RuntimeError("Render video thất bại: không tìm thấy file video đầu ra.")

        duration = render_result.get("duration", 0)
        file_size_mb = render_result.get("file_size_mb", 0)

        # Lưu bản sao vào D:\banve nếu thư mục tồn tại
        banve_dir = Path(r"D:\banve")
        if banve_dir.exists():
            try:
                dest = banve_dir / Path(video_path).name
                shutil.copy2(video_path, dest)
            except Exception as ce:
                logger.warning(f"Không thể sao chép video vào D:\\banve: {ce}")

        await safe_edit_status(
            status_msg,
            f"🎬 *Đã render xong video ({duration:.1f}s)!*\n"
            f"⏳ *Bước 3/3:* Đang tải video lên Telegram...",
            parse_mode="Markdown"
        )

        caption = (
            f"✨ *{title}*\n\n"
            f"⏱ Thời lượng: {duration:.1f}s | 📦 {file_size_mb} MB\n"
            f"🎨 Định dạng: Whiteboard 3-Band (9:16)\n"
            f"🎙 Giọng đọc: CapCut AI (Nam truyền cảm)\n"
            f"🤖 Biên soạn bởi: Meta Muse Spark 1.3"
        )

        upload_to_telegram = os.getenv("AUTODUB_TELEGRAM_UPLOAD_VIDEO", "false").lower() in ("true", "1", "yes")
        if upload_to_telegram:
            await send_video_safely(
                context=context,
                chat_id=chat_id,
                final_video=video_path,
                caption=caption,
                status_msg=status_msg,
                url_or_filename=title
            )
        else:
            await safe_edit_status(
                status_msg,
                f"✅ *Đã render xong video thành công!*\n\n{caption}\n\n📁 *Đã lưu tại:* `{video_path}`",
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Lỗi khi xử lý Muse Spark video: {e}", exc_info=True)
        await safe_edit_status(
            status_msg,
            f"❌ *Lỗi khi tạo video bằng Muse Spark:*\n`{str(e)[:300]}`\n\n"
            f"💡 Bạn có thể thử lại với chủ đề ngắn gọn hơn hoặc thử lại sau vài giây.",
            parse_mode="Markdown"
        )


# ===== LỆNH /status =====
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot đang hoạt động!\n"
        f"📂 Workspace: {WORKSPACE}\n"
        "🎯 Gửi link video để bắt đầu."
    )


# ===== LỆNH /cloud VÀ /cloud_status =====
async def cmd_cloud_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem số dư Compute Units và trạng thái máy ảo Cloud GPU (non-blocking)."""
    try:
        from cloud.cloud_contract import get_cloud_status_snapshot
        from cloud.cloud_job_store import CloudJobStore
        from cloud.cloud_worker import peek_cloud_worker
        from v1_feature_flags import get_feature_flags

        store = CloudJobStore()
        snap = await asyncio.to_thread(
            get_cloud_status_snapshot,
            store,
            worker=peek_cloud_worker(),
            force_refresh=True,
            include_sessions=False,
            wait_timeout_seconds=1.0,
        )
        usage = snap.get("usage") or {}
        cache_meta = snap.get("provider_cache") or {}
        balance = float(usage.get("balance", 0.0) or 0.0)
        rate = float(usage.get("usage_rate", 0.0) or 0.0)
        assignments = int(usage.get("active_assignments", 0) or 0)

        flags = get_feature_flags(WORKSPACE)
        cloud_gpu = flags.get("V1_CLOUD_GPU", "T4")

        jobs = await asyncio.to_thread(store.list_jobs, 5)
        active_job = snap.get("active_job")
        active_str = (
            f"Đang xử lý: `{active_job['video_name']}` ({active_job.get('percent', 0)}%)"
            if active_job
            else "Không có job đang chạy."
        )

        mode_badge = {
            "armed": "Đã mở một lượt",
            "admitted": "Đã sử dụng lượt",
            "paused": "Đang khóa",
        }[snap["rollout"]["phase"]]
        b = snap.get("budget") or {}
        budget_str = (
            f"🛡️ *Hạn mức hôm nay:* `{b['spent_today']:.2f} / {b['budget_daily']:.2f} CU` ({b['percent_used']}%)\n"
            if b
            else ""
        )
        stale_note = ""
        if cache_meta.get("provider_unreachable") or cache_meta.get("stale"):
            age_txt = f"{cache_meta['age_seconds']}s trước" if cache_meta.get("age_seconds") is not None else "chưa có dữ liệu mới"
            stale_note = f"⚠️ *Dữ liệu Colab có thể cũ ({age_txt})*\n"
        cleanup_cnt = int(snap.get("unrecovered_resources_count", 0) or 0)
        cleanup_note = f"🧹 *Tài nguyên chờ thu hồi:* `{cleanup_cnt} session`\n" if cleanup_cnt > 0 else ""

        msg = (
            "☁️ *TRẠNG THÁI CLOUD GPU (GOOGLE COLAB)*\n\n"
            f"📍 *Chế độ Cloud:* {mode_badge}\n"
            f"🚀 *Card GPU chỉ định:* `NVIDIA {cloud_gpu}`\n"
            f"💰 *Số dư Colab CU:* `{balance:.2f} CU` (Tỷ lệ: `{rate:.2f} CU/h`)\n"
            f"{stale_note}"
            f"{cleanup_note}"
            f"{budget_str}"
            f"🖥️ *Máy ảo đang mở:* `{assignments}`\n"
            f"⚙️ *Worker:* `{'Đang chạy' if snap.get('worker_running') else 'Đang chờ'}`\n"
            f"🎯 *Hiện tại:* {active_str}\n\n"
        )
        if jobs:
            msg += f"📋 *Các job gần nhất ({len(jobs)}):*\n"
            for j in jobs[:3]:
                msg += f"• `{j.video_name[:25]}` — *{j.state}* ({j.percent}%)\n"

        msg += (
            "\n💡 *Lệnh điều khiển:*\n"
            "• `/cloud on` — Cách mở một lượt Cloud GPU\n"
            "• `/cloud off` — Tắt chạy Cloud GPU\n"
            "• `/cloud T4` hoặc `/cloud L4` — Đổi card GPU\n"
            "• `/cloud <link_video>` — Ép chạy 1 video trên Cloud"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as exc:
        from cloud.cloud_security import redact_secrets
        await update.message.reply_text(f"❌ Lỗi tra cứu Cloud GPU: {redact_secrets(str(exc))}")


async def cmd_cloud(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Điều khiển và quản lý chế độ Cloud GPU (Google Colab).
    Cú pháp:
    /cloud         - Xem trạng thái & số dư CU
    /cloud on      - Hướng dẫn mở lượt trên Dashboard cục bộ
    /cloud off     - Tắt chế độ Cloud
    /cloud T4 / L4 - Chọn loại GPU Cloud
    /cloud <link hoặc đường dẫn> - Đưa video này chạy trên Cloud GPU
    """
    from v1_feature_flags import get_feature_flags, set_feature_flags

    if not context.args or len(context.args) == 0:
        flags = get_feature_flags(WORKSPACE)
        cloud_gpu = flags.get("V1_CLOUD_GPU", "T4")
        budget_str = ""
        status_text = "Chưa rõ"
        try:
            from cloud.cloud_contract import get_cloud_status_snapshot
            from cloud.cloud_job_store import CloudJobStore
            from cloud.cloud_worker import peek_cloud_worker

            snap = await asyncio.to_thread(
                get_cloud_status_snapshot,
                CloudJobStore(),
                worker=peek_cloud_worker(),
                force_refresh=True,
                include_sessions=False,
                wait_timeout_seconds=1.0,
            )
            usage = snap.get("usage") or {}
            cache_meta = snap.get("provider_cache") or {}
            bal = float(usage.get("balance", 0.0) or 0.0)
            balance_str = f"`{bal:.2f} CU`" + (" *(dữ liệu cũ)*" if cache_meta.get("stale") else "")
            b = snap.get("budget") or {}
            status_text = {
                "armed": "Đã mở một lượt",
                "admitted": "Đã sử dụng lượt",
                "paused": "Đang khóa",
            }[snap["rollout"]["phase"]]
            if b:
                budget_str = f"🛡️ *Hạn mức hôm nay:* `{b['spent_today']:.2f} / {b['budget_daily']:.2f} CU` ({b['percent_used']}%)\n"
        except Exception:
            balance_str = "Chưa rõ"

        msg = (
            "☁️ *CHẾ ĐỘ XỬ LÝ VIDEO TRÊN CLOUD GPU*\n\n"
            f"📍 *Trạng thái:* {status_text}\n"
            f"🚀 *Card GPU:* NVIDIA {cloud_gpu}\n"
            f"💰 *Số dư Colab:* {balance_str}\n"
            f"{budget_str}\n"
            "Video gửi thông thường dùng Local. Để chạy một video trên Cloud, mở một lượt từ Dashboard rồi dùng lệnh bên dưới.\n\n"
            "👉 `/cloud on` — Cách mở một lượt Cloud GPU\n"
            "👉 `/cloud off` — Khóa lượt chạy Cloud\n"
            "👉 `/cloud budget <số_CU>` — Cài đặt hạn mức CU hôm nay\n"
            "👉 `/cloud T4` hoặc `/cloud L4` — Đổi card GPU\n"
            "👉 `/cloud <link hoặc đường dẫn>` — Chạy riêng video này trên Cloud\n"
            "👉 `/cloud_status` — Xem chi tiết máy ảo & số dư CU"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    subcmd = context.args[0].strip().lower()
    if subcmd in ("on", "1", "enable", "bat", "bật"):
        await update.message.reply_text(
            "Trong Cloud GPU Dashboard cục bộ, chọn Mở một lượt Cloud trước khi gửi video.",
        )
        return
    elif subcmd in ("off", "0", "disable", "tat", "tắt"):
        from cloud.cloud_job_store import CloudJobStore

        await asyncio.to_thread(lambda: CloudJobStore().set_rollout(arm=False))
        set_feature_flags({"V1_CLOUD_MODE": False}, WORKSPACE)
        await update.message.reply_text(
            "Đã khóa nhận và khởi chạy job Cloud. Job đang chạy được quản lý theo mã job trên Dashboard."
        )
        return
    elif subcmd in ("t4", "l4", "a100", "g4"):
        target_gpu = subcmd.upper()
        set_feature_flags({"V1_CLOUD_GPU": target_gpu}, WORKSPACE)
        rate = "1.15 CU/h" if target_gpu == "T4" else ("1.94 CU/h" if target_gpu == "L4" else "theo bảng giá Colab")
        await update.message.reply_text(
            f"⚡ *ĐÃ CHUYỂN CLOUD GPU SANG NVIDIA {target_gpu} ({rate})!*\n\n"
            f"Các job Cloud tiếp theo sẽ khởi tạo máy ảo GPU {target_gpu}.",
            parse_mode="Markdown"
        )
        return
    elif subcmd == "gpu" and len(context.args) > 1:
        try:
            from cloud.cloud_security import validate_gpu_type
            target_gpu = validate_gpu_type(context.args[1])
        except Exception as exc:
            await update.message.reply_text(f"❌ {exc}")
            return
        set_feature_flags({"V1_CLOUD_GPU": target_gpu}, WORKSPACE)
        await update.message.reply_text(
            f"⚡ *ĐÃ CHỌN GPU CLOUD:* `NVIDIA {target_gpu}`",
            parse_mode="Markdown"
        )
        return
    elif subcmd in ("stop", "kill", "cancel"):
        try:
            from cloud.cloud_contract import run_provider_action_nonblocking
            from cloud.cloud_job_store import CloudJobStore
            from cloud.colab_bridge import stop_all_sessions

            def cancel_admitted_jobs():
                store = CloudJobStore()
                store.set_rollout(arm=False)
                for cloud_job in store.list_jobs(limit=-1, include_hidden=True):
                    store.request_cancel(cloud_job.job_id, reason="Telegram yêu cầu dừng Cloud")

            # Persist cancellation and close admission before waiting on the
            # provider. Existing owners retain responsibility for verifying and
            # settling their sessions, including allocations still in flight.
            await asyncio.to_thread(cancel_admitted_jobs)
            action_res = await asyncio.to_thread(
                run_provider_action_nonblocking,
                stop_all_sessions,
                timeout_seconds=1.5,
            )
            if action_res["timed_out"]:
                await update.message.reply_text(
                    "⏳ Đã gửi lệnh dừng khẩn cấp toàn bộ máy ảo Colab; provider đang phản hồi chậm, vui lòng kiểm tra lại bằng `/cloud_status`.",
                    parse_mode="Markdown",
                )
            elif not action_res["completed"]:
                raise RuntimeError(str(action_res.get("error") or "Unknown error"))
            else:
                stopped, message = action_res["result"]
                if not stopped:
                    raise RuntimeError(message or "Chưa xác nhận dừng GPU")
                await update.message.reply_text("🛑 Đã thu hồi và dừng toàn bộ máy ảo Colab an toàn!")
        except Exception as exc:
            from cloud.cloud_security import redact_secrets
            await update.message.reply_text(f"❌ Lỗi khi dừng session: {redact_secrets(str(exc))}")
        return
    elif subcmd in ("budget", "limit", "hanmuc", "hạnmức"):
        try:
            from cloud.cloud_job_store import CloudJobStore
            store = CloudJobStore()
            if len(context.args) > 1:
                new_budget = float(context.args[1].strip())
                if new_budget <= 0:
                    raise ValueError("Hạn mức phải lớn hơn 0")
                store.set_cu_budget_daily(new_budget)
                b = store.get_cu_budget_info()
                await update.message.reply_text(
                    f"💾 *ĐÃ CẬP NHẬT HẠN MỨC CU HÔM NAY:*\n\n"
                    f"🛡️ Hạn mức tối đa: `{b['budget_daily']:.2f} CU/ngày`\n"
                    f"📊 Đã dùng hôm nay: `{b['spent_today']:.2f} CU` ({b['percent_used']}%)\n"
                    f"✅ Còn lại: `{b['remaining_today']:.2f} CU`",
                    parse_mode="Markdown"
                )
            else:
                b = store.get_cu_budget_info()
                await update.message.reply_text(
                    f"🛡️ *HẠN MỨC SỬ DỤNG CU HÔM NAY:*\n\n"
                    f"• Hạn mức tối đa: `{b['budget_daily']:.2f} CU/ngày`\n"
                    f"• Đã tiêu thụ hôm nay: `{b['spent_today']:.2f} CU` ({b['percent_used']}%)\n"
                    f"• Còn lại an toàn: `{b['remaining_today']:.2f} CU`\n\n"
                    f"💡 Để đổi hạn mức: `/cloud budget <số_CU>` (ví dụ `/cloud budget 15`)",
                    parse_mode="Markdown"
                )
        except Exception as exc:
            from cloud.cloud_security import redact_secrets
            await update.message.reply_text(f"❌ Lỗi cài đặt hạn mức CU: {redact_secrets(str(exc))}\nCú pháp: `/cloud budget <số_CU>` (ví dụ `/cloud budget 15`)", parse_mode="Markdown")
        return

    # Người dùng gửi kèm link hoặc đường dẫn video: /cloud <target>
    raw_input = " ".join(context.args).strip().strip('"').strip("'")
    from cloud.cloud_job_store import CloudJobStore
    from cloud.cloud_security import CloudValidationError, get_default_allowed_roots, validate_video_input
    allowed_roots = [WORKSPACE, OUTPUT_DIR, *get_default_allowed_roots()]
    try:
        validated_target = validate_video_input(raw_input, allowed_roots=allowed_roots, require_exists=True)
        is_url = validated_target.kind == "url"
        raw_input = validated_target.normalized_value
    except CloudValidationError as exc:
        await update.message.reply_text(
            f"❌ {exc}\n\n"
            "💡 Bạn có thể dán link video (`https://...` TikTok, Douyin, YouTube, Facebook...) hoặc file video hợp lệ trong workspace.",
            parse_mode="Markdown"
        )
        return

    rollout = await asyncio.to_thread(lambda: CloudJobStore().get_rollout())
    if rollout["phase"] != "armed":
        await update.message.reply_text("Hãy mở một lượt Cloud trên Dashboard cục bộ trước khi gửi video.")
        return

    import shared_state
    shared_state.stop_requested = False

    position = _reserve_queue_positions()
    _ensure_video_worker()
    flags = get_feature_flags(WORKSPACE)
    gpu_type = flags.get("V1_CLOUD_GPU", "T4")

    from ai.v1_auto_voice import get_auto_voice_mode
    captured_mode = get_auto_voice_mode(WORKSPACE)

    if is_url:
        job_data = {
            'type': 'url',
            'pos': position,
            'update': update,
            'context': context,
            'url': raw_input,
            'voice_mode': captured_mode,
            'force_cloud': True,
            'cloud_rollout_permit': rollout["permit_id"],
            'cloud_gpu': gpu_type,
        }
    else:
        job_data = {
            'type': 'local_file',
            'pos': position,
            'update': update,
            'context': context,
            'filename': Path(raw_input).name,
            'video_path': raw_input,
            'voice_mode': captured_mode,
            'force_cloud': True,
            'cloud_rollout_permit': rollout["permit_id"],
            'cloud_gpu': gpu_type,
        }

    await global_queue.put(prepare_telegram_job(job_data))
    await update.message.reply_text(
        f"🚀 *ĐÃ THÊM VÀO HÀNG ĐỢI CLOUD GPU ({gpu_type})!*\n\n"
        f"📁 *Nguồn:* `{Path(raw_input).name if not is_url else raw_input[:60]}`\n"
        f"👉 *Vị trí hàng đợi:* #{position}\n"
        "⚡ Hệ thống sẽ xử lý trên Google Colab GPU và tự động lưu video vào `D:\\banve`.",
        parse_mode="Markdown"
    )


# ===== LỆNH /batch =====
async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý hàng loạt video từ thư mục cục bộ (mặc định: D:\\video_input)
    Cú pháp: /batch hoặc /batch D:\\duong_dan_thu_muc
    """
    input_dir = None
    if context.args and len(context.args) > 0:
        input_dir = " ".join(context.args).strip()
    else:
        for candidate in [r"D:\video phôi", r"D:\video phoi", r"D:\video_input"]:
            if os.path.exists(candidate):
                input_dir = candidate
                break
        if not input_dir:
            input_dir = r"D:\video phôi"
        
    output_dir = r"D:\banve"

    if is_downloader_save_dir(input_dir):
        await update.message.reply_text(
            f"⛔ `{input_dir}` là nơi bot tải video lưu về — chỉ tải, không edit.\n"
            "Hãy chọn thư mục khác cho `/batch`.",
            parse_mode="Markdown"
        )
        return

    if not os.path.exists(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        await update.message.reply_text(
            f"📁 *Đã tạo thư mục đầu vào:* `{input_dir}`\n\n"
            f"👉 Bạn hãy copy/thả các file video (.mp4, .mkv, .mov...) cần edit vào thư mục `{input_dir}`, sau đó gõ lại lệnh `/batch` để Bot tự động xử lý lần lượt nhé!",
            parse_mode="Markdown"
        )
        return
        
    from batch_processor import SUPPORTED_EXTENSIONS, process_batch_folder
    video_files = [
        f for f in os.listdir(input_dir)
        if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_")
    ]
    
    if not video_files:
        await update.message.reply_text(
            f"📂 Thư mục `{input_dir}` hiện đang trống!\n\n"
            f"👉 Hãy thả các file video (.mp4, .mkv, .mov...) vào `{input_dir}` rồi gõ lại lệnh `/batch` nhé.",
            parse_mode="Markdown"
        )
        return
        
    status_msg = await update.message.reply_text(
        f"🚀 *Đã tìm thấy {len(video_files)} video trong `{input_dir}`!*\n\n"
        f"🤖 Bot đang bắt đầu xử lý lần lượt từng video (chống giật lag máy)...\n"
        f"💾 Video thành phẩm sẽ được lưu trực tiếp vào: `{output_dir}`",
        parse_mode="Markdown"
    )
    
    async def telegram_progress(msg: str):
        try:
            await safe_edit_status(
                status_msg,
                f"📁 *Batch Processing (`{input_dir}`):*\n\n{msg}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
            
    asyncio.create_task(process_batch_folder(input_dir, output_dir, telegram_progress))

async def cmd_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem hoặc chuyển đổi nhà cung cấp AI dịch thuật (Gemini / OpenAI ChatGPT / DeepSeek)."""
    args = context.args
    current_provider = os.getenv("LLM_PROVIDER", "auto").lower()
    
    gemini_status = "🟢 Đã nạp Key" if os.getenv("GEMINI_API_KEY") else "⚪ Chưa cấu hình"
    openai_status = "🟢 Đã nạp Key" if os.getenv("OPENAI_API_KEY") else "⚪ Chưa cấu hình"
    deepseek_status = "🟢 Đã nạp Key" if os.getenv("DEEPSEEK_API_KEY") else "⚪ Chưa cấu hình"
    
    if not args:
        from ai.v1_gemini_dispatcher import resolve_gemini_models
        from v1_feature_flags import get_feature_flags

        preferred = get_feature_flags(WORKSPACE).get("V1_GEMINI_MODEL") or None
        gemini_models = " → ".join(resolve_gemini_models("translation", preferred=preferred))
        await update.message.reply_text(
            f"🧠 *CẤU HÌNH NHÀ CUNG CẤP AI DỊCH THUẬT (LLM)*\n\n"
            f"📍 *Chế độ ưu tiên hiện tại:* `{current_provider.upper()}`\n\n"
            f"🔹 **Google Gemini:** {gemini_status}\n"
            f"   `{gemini_models}`\n"
            f"🔹 **OpenAI ChatGPT (GPT-4o Vision):** {openai_status}\n"
            f"🔹 **DeepSeek-V4 Series (Văn phong Douyin/TikTok):** {deepseek_status}\n\n"
            f"👉 *Cách đổi mô hình ưu tiên:*\n"
            f"• `/llm auto` - Tự động luân chuyển Gemini ➡️ OpenAI ➡️ DeepSeek (Khuyên dùng)\n"
            f"• `/llm openai` - Ưu tiên OpenAI GPT-4o\n"
            f"• `/llm deepseek` - Ưu tiên DeepSeek V4\n"
            f"• `/llm gemini` - Ưu tiên Google Gemini",
            parse_mode="Markdown"
        )
        return
        
    choice = args[0].lower().strip()
    if choice in ("auto", "gemini", "openai", "deepseek"):
        os.environ["LLM_PROVIDER"] = choice
        await update.message.reply_text(
            f"✅ Đã chuyển mô hình dịch thuật chính sang: *{choice.upper()}*!\n\n"
            f"*(Hệ thống vẫn tự động kích hoạt chế độ Fallback nếu nhà cung cấp này gặp sự cố hoặc hết quota)*",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("❌ Lựa chọn không hợp lệ. Vui lòng chọn: `auto`, `gemini`, `openai`, hoặc `deepseek`.", parse_mode="Markdown")

import shared_state
shared_state.stop_requested = False

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import shared_state
    shared_state.stop_requested = True
    job_tracker.request_stop()
    
    # 1. Hủy ngay lập tức worker task nếu đang chạy
    global worker_task
    if worker_task and not worker_task.done():
        worker_task.cancel()
    
    # 2. Xóa sạch hàng đợi
    await _cancel_download_prefetches()
    global_queue.clear()
    clear_network_retries()
            
    global queue_counter
    queue_counter = 0
            
    await update.message.reply_text("🛑 Đang dừng toàn bộ quá trình tải, bóc tách và render video...")
    
    # 3. Tiêu diệt tất cả các tiến trình con (yt-dlp, ffmpeg, demucs, ffprobe...)
    try:
        import psutil
        current_process = psutil.Process(os.getpid())
        children = current_process.children(recursive=True)
        for child in children:
            try:
                child.kill()
            except Exception:
                pass
        await update.message.reply_text("✅ Đã tiêu diệt xong các tiến trình chạy ngầm.")
    except Exception as e:
        logger.error(f"Error killing children: {e}")

import re

from telegram_queue_monitor import MonitoredQueue, PERSISTED_JOB_FIELDS
from v1_retry_policy import MAX_AUTOMATIC_RETRIES, is_transient_network_error, should_retry_job
global_queue = MonitoredQueue()
queue_counter = 0


def _reserve_queue_positions(count=1):
    global queue_counter
    if global_queue.empty() and global_queue.active_job is None:
        queue_counter = 0
    first_position = queue_counter + 1
    queue_counter += count
    return first_position
worker_task = None
_prefetch_tasks = set()
GLOBAL_BOT_APP = None

NETWORK_RETRY_DELAYS = (60, 300)
NETWORK_RETRY_LABELS = {60: "1 phút", 300: "5 phút"}
NETWORK_RETRY_FILE = "telegram_retry_state.json"
_RETRY_JOB_FIELDS = PERSISTED_JOB_FIELDS
_network_retry_jobs = {}
_network_retry_task = None


def network_retry_state_path():
    bot_system = Path(WORKSPACE) / "bot_system"
    return bot_system / NETWORK_RETRY_FILE if bot_system.is_dir() else Path(WORKSPACE) / NETWORK_RETRY_FILE


def _load_network_retry_state():
    path = network_retry_state_path()
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"version": 1, "jobs": {}}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    return {"version": 1, "jobs": jobs if isinstance(jobs, dict) else {}}


def _write_network_retry_state(state):
    path = network_retry_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, state)


def _serialize_retry_job(job):
    from copy import deepcopy
    return {field: deepcopy(job.get(field)) for field in _RETRY_JOB_FIELDS}


def _automatic_retry_count(job):
    return int(job.get("retry_count") or 0) + int(job.get("network_retry_count") or 0)


def schedule_network_retry(job):
    """Persist one delayed retry per job_id; returns None when attempts are exhausted."""
    job_id = str(job.get("job_id") or "").strip()
    if not job_id or _automatic_retry_count(job) >= MAX_AUTOMATIC_RETRIES:
        return None
    attempt = int(job.get("network_retry_count") or 0) + 1
    delay = NETWORK_RETRY_DELAYS[attempt - 1]
    now = time.time()
    retry_job = dict(job, network_retry_count=attempt)
    state = _load_network_retry_state()
    state["jobs"][job_id] = {
        "attempt": attempt,
        "delay_seconds": delay,
        "scheduled_at": now,
        "next_attempt_at": now + delay,
        "job": _serialize_retry_job(retry_job),
    }
    try:
        _write_network_retry_state(state)
    except (OSError, TypeError, ValueError):
        logger.exception("Cannot persist Telegram network retry for %s", job_id)
        return None
    _network_retry_jobs[job_id] = retry_job
    _ensure_network_retry_scheduler()
    return {"attempt": attempt, "total_retries": _automatic_retry_count(retry_job),
            "delay_seconds": delay, "next_attempt_at": now + delay}


def clear_network_retries():
    _network_retry_jobs.clear()
    path = network_retry_state_path()
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Cannot remove Telegram network retry state", exc_info=True)


async def process_due_network_retries(now=None):
    """Move due persisted retries back into the live queue exactly once per job_id."""
    now = time.time() if now is None else float(now)
    state = _load_network_retry_state()
    records = state["jobs"]
    if not records:
        return 0
    active = getattr(global_queue, "active_job", None)
    active_id = active.get("job_id") if isinstance(active, dict) else None
    queued_ids = {
        item.get("job_id")
        for item in list(getattr(global_queue, "_queue", ()))
        if isinstance(item, dict) and item.get("job_id")
    }
    changed = False
    enqueued = 0
    for job_id, record in list(records.items()):
        if not isinstance(record, dict):
            records.pop(job_id, None)
            _network_retry_jobs.pop(job_id, None)
            changed = True
            continue
        try:
            due = float(record.get("next_attempt_at", 0))
        except (TypeError, ValueError):
            due = 0
        if due > now or job_id == active_id:
            continue
        if job_id in queued_ids:
            records.pop(job_id, None)
            _network_retry_jobs.pop(job_id, None)
            changed = True
            continue
        job = _network_retry_jobs.pop(job_id, None)
        if job is None:
            stored = record.get("job") if isinstance(record.get("job"), dict) else {}
            job = {field: stored.get(field) for field in _RETRY_JOB_FIELDS}
        job["network_retry_count"] = int(record.get("attempt") or 0)
        if (_automatic_retry_count(job) > MAX_AUTOMATIC_RETRIES
                or not (job.get("url") or job.get("file_id") or
                        (job.get("type") == "local_file" and job.get("video_path")))):
            records.pop(job_id, None)
            changed = True
            continue
        try:
            job = prepare_telegram_job(job)
        except (TypeError, ValueError):
            logger.warning("Dropping invalid Telegram network retry %s", job_id, exc_info=True)
            records.pop(job_id, None)
            changed = True
            continue
        job["pos"] = _reserve_queue_positions()
        await global_queue.put(job)
        records.pop(job_id, None)
        changed = True
        enqueued += 1
    if changed:
        _write_network_retry_state(state)
    if not global_queue.empty():
        _ensure_video_worker()
    return enqueued


async def _network_retry_scheduler():
    while True:
        try:
            await process_due_network_retries()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram network retry scheduler failed")
        await asyncio.sleep(2)


def _ensure_network_retry_scheduler():
    global _network_retry_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _network_retry_task is None or _network_retry_task.done():
        _network_retry_task = loop.create_task(_network_retry_scheduler())
    return _network_retry_task


def _ensure_video_worker():
    global worker_task
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(video_worker())
    return worker_task


def _telegram_prefetch_depth():
    try:
        return min(2, max(0, int(os.getenv("V1_TELEGRAM_PREFETCH_DEPTH", "1"))))
    except (TypeError, ValueError):
        return 1


def _has_existing_telegram_source(job):
    state = job.get("resume_state")
    source_path = state.get("source_path") if isinstance(state, dict) else None
    return bool(source_path and Path(source_path).is_file())


async def _run_download_prefetch(job):
    try:
        source_path = await _download_job_source(job, None, reload_downloader=False)
    except asyncio.CancelledError:
        logger.info("Telegram download prefetch cancelled job_id=%s", job.get("job_id"))
        raise
    except Exception:
        logger.warning(
            "Telegram download prefetch failed job_id=%s",
            job.get("job_id"),
            exc_info=True,
        )
        raise
    logger.info(
        "Telegram download prefetch completed job_id=%s",
        job.get("job_id"),
    )
    return source_path


def _discard_download_prefetch(task):
    _prefetch_tasks.discard(task)
    if not task.cancelled():
        task.exception()


def _schedule_next_download_prefetch():
    if os.getenv("V1_TELEGRAM_PREFETCH", "1").strip() == "0":
        return None
    if getattr(shared_state, "stop_requested", False) or any(
        not task.done() for task in _prefetch_tasks
    ):
        return None
    depth = _telegram_prefetch_depth()
    if depth == 0:
        return None

    for job in list(global_queue._queue)[:depth]:
        if not isinstance(job, dict):
            continue
        if job.get("type") not in {"url", "video"}:
            continue
        if job.get("execution_mode") != "local":
            continue
        if _has_existing_telegram_source(job) or job.get("_prefetch_task"):
            continue
        task = asyncio.create_task(_run_download_prefetch(job))
        job["_prefetch_task"] = task
        _prefetch_tasks.add(task)
        task.add_done_callback(_discard_download_prefetch)
        return task
    return None


async def download_prefetcher():
    if os.getenv("V1_TELEGRAM_PREFETCH", "1").strip() == "0":
        return
    while True:
        if not getattr(shared_state, "stop_requested", False):
            _schedule_next_download_prefetch()
        await asyncio.sleep(1)


async def _cancel_download_prefetches():
    tasks = set(_prefetch_tasks)
    queued_jobs = list(global_queue._queue)
    if global_queue.active_job is not None:
        queued_jobs.append(global_queue.active_job)
    for job in queued_jobs:
        if isinstance(job, dict):
            task = job.pop("_prefetch_task", None)
            if task is not None:
                tasks.add(task)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _install_download_prefetcher(application):
    previous_post_init = application.post_init

    async def start_prefetcher(app):
        if previous_post_init:
            await previous_post_init(app)
        app.bot_data["_telegram_download_prefetcher"] = asyncio.create_task(
            download_prefetcher()
        )

    application.post_init = start_prefetcher
    previous_post_shutdown = application.post_shutdown

    async def stop_prefetcher(app):
        await _cancel_download_prefetches()
        task = app.bot_data.pop("_telegram_download_prefetcher", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if previous_post_shutdown:
            await previous_post_shutdown(app)

    application.post_shutdown = stop_prefetcher


async def send_video_safely(context, chat_id, final_video, caption, status_msg, url_or_filename):
    file_size = os.path.getsize(final_video)
    max_size = 49.5 * 1024 * 1024
    
    if file_size <= max_size:
        with open(final_video, 'rb') as vf:
            await context.bot.send_video(
                chat_id=chat_id, video=vf, caption=caption,
                supports_streaming=True, read_timeout=600, write_timeout=600, connect_timeout=600
            )
        await safe_edit_status(
            status_msg,
            f"✅ *Hoàn tất!*\n`{url_or_filename}`",
            parse_mode="Markdown",
        )
        return

    # Video dung lượng lớn (>49.5MB): Giữ nguyên 100% chất lượng gốc (không nén CRF làm giảm độ nét)
    # File video hoàn chỉnh chất lượng cao nhất đã được lưu trực tiếp tại D:\banve.
    await safe_edit_status(
        status_msg,
        f"💎 *Video chất lượng cao ({file_size // (1024*1024)}MB) đã lưu tại D:\\banve!*\n"
        f"Giữ nguyên 100% độ nét gốc (không nén), chia phần stream-copy gửi qua Telegram...",
        parse_mode="Markdown",
    )
    
    import math
    import subprocess
    import ffmpeg
    try:
        probe = ffmpeg.probe(final_video)
        duration = float(probe['format']['duration'])
        num_chunks = math.ceil(file_size / max_size)
        chunk_time = duration / num_chunks
        
        base_name = os.path.splitext(final_video)[0]
        CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
        
        cmd = [
            'ffmpeg', '-y', '-i', final_video, 
            '-c', 'copy', 
            '-f', 'segment', 
            '-segment_time', str(chunk_time), 
            '-reset_timestamps', '1', 
            f'{base_name}_part%03d.mp4'
        ]
        await asyncio.to_thread(subprocess.run, cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        
        import glob
        parts = sorted(glob.glob(f"{base_name}_part*.mp4"))
        
        for i, part in enumerate(parts, 1):
            if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
            await safe_edit_status(
                status_msg,
                f"📤 Đang gửi phần {i}/{len(parts)}...",
                parse_mode="Markdown",
            )
            part_caption = f"{caption}\n\n(Phần {i}/{len(parts)})" if i == 1 else f"🎬 Phần {i}/{len(parts)}"
            with open(part, 'rb') as vf:
                await context.bot.send_video(
                    chat_id=chat_id, video=vf, caption=part_caption,
                    supports_streaming=True, read_timeout=600, write_timeout=600, connect_timeout=600
                )
        
        await safe_edit_status(
            status_msg,
            f"✅ *Đã gửi thành công {len(parts)} phần video chất lượng cao!*\n`{url_or_filename}`",
            parse_mode="Markdown",
        )
        
    except Exception as e:
        logger.error(f"Error splitting video: {e}")
        await safe_edit_status(
            status_msg,
            "❌ *Lỗi chia nhỏ video:* Không thể gửi file lớn qua Telegram.",
        )

async def video_worker():
    while True:
        try:
            job = await global_queue.get()
            import shared_state
            shared_state.stop_requested = False
            import job_tracker
            tracker_job = None
            from telegram_progress import current_job
            progress_token = None
            result = None
            try:
                wait_notice_sent = False
                while tracker_job is None:
                    try:
                        tracker_job = job_tracker.start_batch(1, output_dir=OUTPUT_DIR)
                        progress_token = current_job.set(tracker_job)
                    except job_tracker.JobAlreadyRunningError:
                        if not wait_notice_sent and isinstance(job, dict) and job.get('update'):
                            try:
                                wait_notice_sent = True
                                update_obj = job.get('update')
                                if update_obj and hasattr(update_obj, 'message') and update_obj.message:
                                    await update_obj.message.reply_text(
                                        "⏳ *Hệ thống đang bận xử lý Batch Offline.*\nYêu cầu của bạn đã được xếp hàng an toàn và sẽ tự động bắt đầu ngay khi Batch hoàn tất.",
                                        parse_mode="Markdown"
                                    )
                            except Exception:
                                pass
                        await asyncio.sleep(2)
                if isinstance(job, dict):
                    job_voice_mode = job.get('voice_mode')
                    if not job_voice_mode:
                        from ai.v1_auto_voice import get_auto_voice_mode
                        job_voice_mode = get_auto_voice_mode(WORKSPACE)

                    if job['type'] == 'url':
                        result = await process_single_url(
                            job.get('update'), job.get('context'), job['url'], job.get('pos', 1),
                            chat_id=job.get('chat_id'), voice_mode=job_voice_mode,
                            retry_count=job.get('retry_count', 0), queue_job=job
                        )
                    elif job['type'] == 'video':
                        result = await process_single_video(
                            job.get('update'), job.get('context'), job.get('file_id'),
                            job.get('filename') or 'video.mp4', job.get('pos', 1),
                            chat_id=job.get('chat_id'), voice_mode=job_voice_mode,
                            retry_count=job.get('retry_count', 0), queue_job=job
                        )
                    elif job['type'] == 'local_file':
                        result = await _process_telegram_job(job)
                    elif job['type'] == 'resume_v2':
                        from pipeline_v2.config import PipelineSettings
                        from pipeline_v2.resume import resume_video_job

                        resumable = job['job']

                        async def resume_progress(job_id, stage, state):
                            logger.info(
                                "[resume:%s] %s: %s", job_id, stage, state
                            )

                        await resume_video_job(
                            resumable,
                            PipelineSettings.from_env(),
                            api_key=GEMINI_API_KEY,
                            progress=resume_progress,
                        )
                    else:
                        raise ValueError(f"Loại công việc Telegram không được hỗ trợ: {job.get('type')}")
                else:
                    pos, update, context, url = job
                    from ai.v1_auto_voice import get_auto_voice_mode
                    result = await process_single_url(update, context, url, pos, voice_mode=get_auto_voice_mode(WORKSPACE))
            except asyncio.CancelledError:
                if tracker_job:
                    job_tracker.mark_stopped()
                logger.info("Worker task cancelled by /stop.")
                break
            except Exception as e:
                if tracker_job:
                    job_tracker.fail_batch(str(e), job_id=tracker_job)
                logger.error(f"Worker error: {e}")
            finally:
                import gc, torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    if hasattr(torch.cuda, "ipc_collect"):
                        torch.cuda.ipc_collect()
                global_queue.task_done()
                if tracker_job:
                    state = job_tracker.get_status()
                    if isinstance(result, dict) and result.get("status") in ("retry_scheduled", "retry_queued"):
                        job_tracker.mark_retry_waiting(tracker_job, result["message"])
                    elif state.get("video_status") == "completed":
                        job_tracker.finish_batch()
                    elif state.get("active"):
                        job_tracker.fail_batch("Video kết thúc mà chưa xác nhận thành phẩm.", job_id=tracker_job)
                    job_tracker.release_batch(tracker_job)
                if progress_token is not None:
                    current_job.reset(progress_token)
        except asyncio.CancelledError:
            logger.info("Worker queue cancelled.")
            break

def prepare_telegram_job(job):
    """Freeze queue identity, execution mode snapshot, and settings before the first attempt."""
    import uuid
    from copy import deepcopy
    from v1_feature_flags import get_feature_flags
    from v1_job_policy import capture_job_settings
    from preset_service import resolve_effective_config

    if not job.get("job_id"):
        job["job_id"] = f"telegram_{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", job["job_id"]):
        raise ValueError("Job ID Telegram khong hop le.")

    flags_snapshot = get_feature_flags(WORKSPACE)
    if "execution_mode" not in job or job.get("execution_mode") not in ("cloud", "local"):
        is_cloud_snap = bool(job.get("force_cloud"))
        job["execution_mode"] = "cloud" if is_cloud_snap else "local"
    if not job.get("cloud_gpu"):
        job["cloud_gpu"] = str(flags_snapshot.get("V1_CLOUD_GPU", "T4") or "T4")
    if "allow_cloud_fallback" not in job:
        job["allow_cloud_fallback"] = False
    if not job.get("idempotency_key"):
        job["idempotency_key"] = f"tg:{job['job_id']}"

    if not job.get("job_overrides"):
        captured = capture_job_settings(WORKSPACE)
        mode = job.get("voice_mode") or captured["voice_mode"]
        captured["voice_mode"] = mode
        captured["voice_config"]["enabled"] = mode == "auto"
        settings = resolve_effective_config()["effective_config"]
        merged = deepcopy({**settings, **captured})
        merged["execution_mode"] = job["execution_mode"]
        merged["cloud_gpu"] = job["cloud_gpu"]
        merged["allow_cloud_fallback"] = bool(job["allow_cloud_fallback"])
        merged["idempotency_key"] = job["idempotency_key"]
        job["job_overrides"] = merged
    else:
        job["job_overrides"].setdefault("execution_mode", job["execution_mode"])
        job["job_overrides"].setdefault("cloud_gpu", job["cloud_gpu"])
        job["job_overrides"].setdefault("allow_cloud_fallback", bool(job["allow_cloud_fallback"]))
        job["job_overrides"].setdefault("idempotency_key", job["idempotency_key"])

    job["voice_mode"] = job["job_overrides"].get("voice_mode", "auto")
    job["resume_state"] = dict(job.get("resume_state") or {})
    if not job.get("chat_id"):
        message = getattr(job.get("update"), "message", None)
        if message:
            job["chat_id"] = message.chat_id
    return job


async def run_pipeline_cloud_for_telegram(video_path, job, status_msg):
    """Xử lý video hoàn toàn trên Google Colab GPU dùng chung hợp đồng Admission & Idempotency."""
    from cloud.cloud_contract import admit_and_enqueue_job, get_error_remediation
    from cloud.cloud_job_store import CloudJobStore
    from cloud.cloud_orchestrator import CloudOrchestrator
    from cloud.cloud_security import get_default_allowed_roots, redact_secrets
    from cloud.cloud_worker import CloudWorker
    from v1_feature_flags import get_feature_flags

    prepare_telegram_job(job)
    gpu_type = job.get("cloud_gpu") or get_feature_flags(WORKSPACE).get("V1_CLOUD_GPU", "T4")
    voice = job.get("voice") or "BV562_streaming"
    idem_key = job.get("idempotency_key") or f"tg:{job.get('job_id')}"

    store = CloudJobStore()
    v_path_str = str(video_path) if video_path else job.get("url")
    allowed_roots = [WORKSPACE, OUTPUT_DIR, *get_default_allowed_roots()]

    cloud_job, contract = await asyncio.to_thread(
        admit_and_enqueue_job, store,
        video_path=v_path_str,
        source_channel="telegram",
        source_job_id=job.get("job_id"),
        gpu_type=gpu_type,
        task_type="full_pipeline",
        voice=voice,
        allowed_roots=allowed_roots,
        enforce_budget=True,
        claim_immediately=True,
        owner_id=f"telegram:{job.get('job_id') or 'direct'}",
        idempotency_key=idem_key,
        rollout_permit=job.get("cloud_rollout_permit"),
        options={
            "execution_mode": "cloud",
            "allow_no_speech": bool(job.get("allow_no_speech", False)),
            "source_telegram_job_id": job.get("job_id"),
        },
    )

    resume_state = job.setdefault("resume_state", {})
    if getattr(contract, "deduplicated", False):
        if cloud_job.state == "completed":
            already_sent = resume_state.get("delivered_cloud_job_id") == cloud_job.job_id
            result_file = Path(cloud_job.result_path) if cloud_job.result_path else None
            manifest = getattr(cloud_job, "output_manifest", None) or {}
            if not result_file or not result_file.is_file() or result_file.stat().st_size <= 0:
                raise RuntimeError("Job Cloud trùng idempotency_key đã hoàn tất trước đó nhưng thiếu file thành phẩm.")
            if manifest and manifest.get("qc_passed") is False:
                raise RuntimeError("Video thành phẩm chưa đạt kiểm tra QC nên bị chặn gửi.")
            resume_state["delivered_cloud_job_id"] = cloud_job.job_id
            return {
                "status": "success",
                "final_video": cloud_job.result_path,
                "cu_actual": cloud_job.cu_actual,
                "gpu_type": cloud_job.gpu_type,
                "output_manifest": manifest or None,
                "mode": "cloud",
                "deduplicated": True,
                "already_delivered": already_sent,
            }
        if cloud_job.state in ("failed", "cancelled", "error", "stopped"):
            remediation = get_error_remediation(cloud_job.error_code)
            err_txt = redact_secrets(cloud_job.error or f"Job đã kết thúc với trạng thái {cloud_job.state}.")
            raise RuntimeError(f"{err_txt}" + (f" ({remediation})" if remediation else ""))
        if cloud_job.state != "queued":
            raise RuntimeError(f"Job Cloud {cloud_job.job_id} đang được thực thi ở tiến trình khác ({cloud_job.state}).")

    loop = asyncio.get_running_loop()

    def on_progress(percent: int, step_name: str):
        safe_step = redact_secrets(step_name)
        text = f"☁️ *[Cloud GPU {cloud_job.gpu_type}]* ({percent}%)\n\n👉 {safe_step}"
        asyncio.run_coroutine_threadsafe(
            safe_edit_status(status_msg, text, parse_mode="Markdown"),
            loop,
        )

    orchestrator = CloudOrchestrator(store, allowed_roots=allowed_roots)
    worker = CloudWorker(worker_id=cloud_job.owner_id, store=store, orchestrator=orchestrator)
    success = await asyncio.to_thread(worker.process_claimed_job, cloud_job, on_progress)

    updated_job = store.get_job(cloud_job.job_id)
    if not success or not updated_job or updated_job.state != "completed":
        err_msg = redact_secrets(updated_job.error if updated_job else "Pipeline Cloud GPU thất bại.")
        remediation = get_error_remediation(updated_job.error_code if updated_job else None)
        raise RuntimeError(f"{err_msg}" + (f"\n💡 {remediation}" if remediation else ""))

    result_file = Path(updated_job.result_path) if updated_job.result_path else None
    manifest = getattr(updated_job, "output_manifest", None) or {}
    if not result_file or not result_file.is_file() or result_file.stat().st_size <= 0:
        raise RuntimeError("Không tìm thấy file video thành phẩm sau khi Cloud hoàn tất.")
    if manifest and manifest.get("qc_passed") is False:
        raise RuntimeError("Video thành phẩm chưa đạt kiểm tra QC nên bị chặn gửi.")

    resume_state["delivered_cloud_job_id"] = updated_job.job_id
    return {
        "status": "success",
        "final_video": updated_job.result_path,
        "cu_actual": updated_job.cu_actual,
        "gpu_type": cloud_job.gpu_type,
        "output_manifest": manifest or None,
        "mode": "cloud",
        "deduplicated": False,
        "already_delivered": False,
    }


async def run_pipeline_v1_for_telegram(video_path, job, status_msg):
    import shared_state
    from v1_orchestrator import V1Orchestrator

    state = job["resume_state"]
    safe_stem = Path(video_path).stem.rstrip(". ") or "video"
    raw_target_dir = state.get("output_dir") or Path(WORKSPACE) / safe_stem
    out_dir = Path(str(raw_target_dir).rstrip(". ")).resolve()
    if not out_dir.is_relative_to(Path(WORKSPACE).resolve()):
        raise ValueError("Thu muc job Telegram nam ngoai workspace.")
    state["output_dir"] = str(out_dir)
    global_queue.publish()

    async def progress(stage, step, total, percent, message, details):
        from telegram_progress import suspend_text_progress
        if stage != "publish":
            job_tracker.update_step(step, message, percent=int(percent), stage=stage,
                                    total_steps=total, details=details)
        with suspend_text_progress():
            await safe_edit_status(status_msg, message)

    result = await V1Orchestrator(WORKSPACE).execute_job(
        video_path=video_path,
        job_id=job["job_id"],
        output_dir=out_dir,
        delivery_path=Path(OUTPUT_DIR) / f"Dubbed_{safe_stem}.mp4",
        user_mode=job.get("video_mode") or job["job_overrides"].get("feature_flags", {}).get("V1_VIDEO_MODE", "AUTO"),
        overrides=job["job_overrides"],
        progress_callback=progress,
        resume_if_possible=True,
        stop_checker=lambda: shared_state.stop_requested,
    )
    if result.get("status") != "success":
        raise RuntimeError(f"Pipeline V1 chua hoan tat: {result.get('status')}")
    if not result.get("tracker_finalized"):
        job_tracker.finish_video(Path(video_path).name, result["final_video"], 0)
    return result


async def _download_job_source(job, status_msg=None, *, reload_downloader=True) -> Path:
    from pipeline_v2.download_validation import probe_downloaded_video
    from v1_checkpoint import fingerprint

    download_dir = Path(WORKSPACE) / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    title = None
    if job["type"] == "url":
        import importlib
        import social_downloader
        if reload_downloader:
            importlib.reload(social_downloader)
        from social_downloader import download_social_video

        await safe_edit_status(
            status_msg,
            f"Đang tải video: {job.get('url') or job.get('filename') or 'Video Telegram'}",
        )
        download_task = asyncio.create_task(asyncio.to_thread(
            download_social_video, job["url"], str(download_dir), job["job_id"]
        ))

        def remove_cancelled_download(done):
            try:
                result = done.result()
                if not result[0] or not result[1]:
                    return
                downloaded_path = Path(result[1]).resolve()
                if (
                    downloaded_path.is_relative_to(download_dir.resolve())
                    and downloaded_path.is_file()
                ):
                    downloaded_path.unlink()
                    logger.info(
                        "Removed URL download after Telegram prefetch cancellation job_id=%s",
                        job.get("job_id"),
                    )
            except BaseException:
                logger.warning(
                    "Could not clean completed URL download after Telegram prefetch cancellation job_id=%s",
                    job.get("job_id"),
                    exc_info=True,
                )

        try:
            success, path, title, error = await asyncio.shield(download_task)
        except asyncio.CancelledError:
            download_task.add_done_callback(remove_cancelled_download)
            raise
        if not success:
            raise RuntimeError(error or "Khong tai duoc video.")
        video_path = Path(path)
        await asyncio.to_thread(probe_downloaded_video, video_path)
    else:
        bot = getattr(job.get("context"), "bot", None) or getattr(GLOBAL_BOT_APP, "bot", None)
        if not bot:
            raise RuntimeError("Bot client khong kha dung de tai video.")
        video_path = download_dir / f"{job['job_id']}.mp4"
        partial = video_path.with_suffix(".partial.mp4")
        await safe_edit_status(status_msg, "Đang tải video từ Telegram...")
        try:
            async def download():
                file = await bot.get_file(job["file_id"], read_timeout=60, connect_timeout=60)
                await file.download_to_drive(
                    str(partial), read_timeout=60, write_timeout=60, connect_timeout=60
                )

            await asyncio.wait_for(download(), timeout=600)
            await asyncio.to_thread(probe_downloaded_video, partial)
            os.replace(partial, video_path)
        finally:
            partial.unlink(missing_ok=True)

    new_sha = await asyncio.to_thread(fingerprint, video_path)
    state = job.setdefault("resume_state", {})
    if state.get("source_sha256") and state["source_sha256"] != new_sha:
        raise ValueError("Nguon tai lai khac nguon da dong bang.")
    state["source_path"] = str(video_path)
    state["source_sha256"] = new_sha
    if title is not None:
        state["title"] = title
    global_queue.publish()
    return video_path


async def _process_telegram_job(job):
    import shared_state
    from batch_processor import BatchStopRequested
    from pipeline_v2.download_validation import probe_downloaded_video
    from v1_checkpoint import fingerprint
    from v1_quality_gate import CriticalQualityError

    prepare_telegram_job(job)
    global_queue.publish()
    name = job.get("url") or job.get("filename") or "Video Telegram"
    pos = job.get("pos", 1)
    job_tracker.start_video(name, pos, pos + global_queue.qsize())
    status_msg = None
    started = time.monotonic()
    try:
        message = getattr(job.get("update"), "message", None)
        if message:
            status_msg = await message.reply_text(f"Đang xử lý video #{pos}: {name}")
        elif job.get("chat_id") and GLOBAL_BOT_APP:
            status_msg = await GLOBAL_BOT_APP.bot.send_message(
                chat_id=job["chat_id"], text=f"Đang tiếp tục video #{pos}: {name}"
            )
    except Exception:
        logger.warning("Cannot send Telegram start status", exc_info=True)

    try:
        if shared_state.stop_requested:
            raise BatchStopRequested("Đã dừng theo yêu cầu.")
        if job.get("type") == "local_file" and job.get("video_path"):
            video_path = Path(job["video_path"]).resolve()
            if not video_path.is_file():
                raise ValueError("Không tìm thấy file video local đã xếp hàng.")
            await asyncio.to_thread(probe_downloaded_video, video_path)
            state = job["resume_state"]
            source_sha = await asyncio.to_thread(fingerprint, video_path)
            if state.get("source_sha256") and state["source_sha256"] != source_sha:
                raise ValueError("Nguồn local đã thay đổi; không thể resume job cũ.")
            state["source_path"] = str(video_path)
            state["source_sha256"] = source_sha
            global_queue.publish()
        else:
            state = job["resume_state"]
            prefetch_task = job.get("_prefetch_task")
            if prefetch_task is not None:
                try:
                    if not prefetch_task.done():
                        await safe_edit_status(status_msg, "Đang chờ tải xong video...")
                    await asyncio.shield(prefetch_task)
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    cancelling = getattr(current_task, "cancelling", None)
                    if (
                        callable(cancelling) and cancelling()
                    ) or not prefetch_task.cancelled():
                        raise
                    job.pop("_prefetch_task", None)
                    logger.warning(
                        "Telegram prefetch cancelled; downloading in worker job_id=%s",
                        job.get("job_id"),
                    )
                    await _download_job_source(job, status_msg)
                except Exception:
                    job.pop("_prefetch_task", None)
                    logger.warning(
                        "Telegram prefetch failed; downloading in worker job_id=%s",
                        job.get("job_id"),
                        exc_info=True,
                    )
                    await _download_job_source(job, status_msg)
                else:
                    job.pop("_prefetch_task", None)
            video_path = Path(state["source_path"]) if state.get("source_path") else None
            if video_path and not video_path.resolve().is_relative_to((Path(WORKSPACE) / "downloads").resolve()):
                raise ValueError("Nguon Telegram nam ngoai thu muc downloads.")
            if video_path and video_path.is_file():
                await asyncio.to_thread(probe_downloaded_video, video_path)
                expected = state.get("source_sha256")
                if expected and await asyncio.to_thread(fingerprint, video_path) != expected:
                    raise ValueError("Nguon Telegram da thay doi; khong the resume job cu.")
            else:
                video_path = await _download_job_source(job, status_msg)

        if shared_state.stop_requested:
            raise BatchStopRequested("Đã dừng theo yêu cầu.")
        job_tracker.start_video(video_path.name, pos, pos + global_queue.qsize())

        if job["execution_mode"] == "cloud":
            result = await run_pipeline_cloud_for_telegram(video_path, job, status_msg)
        else:
            result = await run_pipeline_v1_for_telegram(video_path, job, status_msg)

        if isinstance(result, dict) and result.get("already_delivered"):
            return result

        elapsed = int(time.monotonic() - started)
        dest_dir = Path(result["final_video"]).parent
        remaining = global_queue.qsize()
        queue_msg = (
            "🎉 Đã hoàn tất toàn bộ hàng đợi!"
            if remaining == 0
            else f"⏳ Phía sau còn {remaining} video đang chờ xử lý..."
        )
        time_str = (
            f"{elapsed // 60} phút {elapsed % 60} giây"
            if elapsed >= 60
            else f"{elapsed} giây"
        )
        source_line = (
            f"📎 Link gốc: {job['url']}"
            if job.get("url")
            else f"📎 Tệp gốc: {job.get('filename') or job.get('name') or 'Video tải lên'}"
        )

        cu_info = f"⚡ Tiêu hao CU: ~{result.get('cu_actual', 0.0)} CU\n" if result.get("cu_actual") is not None else ""
        gpu_label = f" (Cloud GPU {result.get('gpu_type', 'T4')})" if result.get("mode") == "cloud" else ""
        completion_msg = (
            f"🎬 Video đã lồng tiếng Việt{gpu_label}\n\n"
            f"💾 Đã tự động lưu vào máy:\n"
            f"`{dest_dir}`\n\n"
            f"⏱️ Thời gian xử lý: {time_str}\n"
            f"{cu_info}\n"
            f"{queue_msg}\n\n"
            f"{source_line}"
        )
        await safe_edit_status(
            status_msg,
            completion_msg,
            parse_mode="Markdown",
        )
        return result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("Telegram V1 job %s failed", job["job_id"])
        stopped = isinstance(exc, BatchStopRequested) or shared_state.stop_requested
        blocked = isinstance(exc, (CriticalQualityError, ValueError)) or "Quality Gate BLOCK:" in str(exc)
        network_failure = is_transient_network_error(exc)
        attempts = _automatic_retry_count(job)
        can_retry = not blocked and should_retry_job(
            exc, attempt=attempts, stopped=stopped, retry_generated_output=True,
        )
        if can_retry and network_failure:
            scheduled = schedule_network_retry(job)
            if scheduled:
                delay_label = NETWORK_RETRY_LABELS.get(
                    scheduled["delay_seconds"], f"{scheduled['delay_seconds']} giây"
                )
                message = (f"Đang chờ mạng và sẽ thử lại sau {delay_label} "
                           f"(lần {scheduled['total_retries']}/{MAX_AUTOMATIC_RETRIES}).")
                await safe_edit_status(status_msg, f"⚠️ Mất kết nối mạng khi xử lý video.\n{message}")
                logger.warning(
                    "Telegram V1 job %s waiting for network; retry %s/%s in %ss",
                    job["job_id"], scheduled["total_retries"], MAX_AUTOMATIC_RETRIES,
                    scheduled["delay_seconds"],
                )
                return {"status": "retry_scheduled", "job_id": job["job_id"], "message": message}
        elif can_retry:
            retry = dict(job, retry_count=int(job.get("retry_count") or 0) + 1, pos=_reserve_queue_positions())
            global_queue.requeue_nowait(job, retry)
            message = f"Đã xếp cuối hàng chờ để thử lại lần {attempts + 1}/{MAX_AUTOMATIC_RETRIES}."
            await safe_edit_status(
                status_msg, f"Xử lý gặp lỗi: {str(exc)[:300]}\n{message}",
            )
            return {"status": "retry_queued", "job_id": job["job_id"], "message": message}
        job_tracker.set_error(name, str(exc), fatal=True)
        reason = " do lỗi mạng" if network_failure else ""
        exhausted = f"\nĐã dùng hết {MAX_AUTOMATIC_RETRIES} lượt thử lại." if attempts >= MAX_AUTOMATIC_RETRIES and not stopped else ""
        await safe_edit_status(status_msg, f"Không hoàn tất video{reason}: {str(exc)[:500]}{exhausted}")
        return {"status": "stopped" if stopped else "failed", "job_id": job["job_id"]}


async def process_single_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, pos: int = 1,
                             chat_id: int = None, voice_mode: str = None, retry_count: int = 0,
                             queue_job=None):
    job = queue_job if queue_job is not None else {
        "type": "url", "url": url, "pos": pos, "update": update, "context": context,
        "chat_id": chat_id, "voice_mode": voice_mode, "retry_count": retry_count,
    }
    return await _process_telegram_job(job)


# ===== XỬ LÝ MESSAGE CÓ CHỨA LINK =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Hỗ trợ nhiều URL, loại trùng và bỏ timestamp dính vào link khi copy chat.
    urls = extract_http_urls(text)

    if not urls:
        cleaned_text = text.strip()
        if len(cleaned_text) >= 4 and not cleaned_text.startswith("/"):
            prompt_preview = cleaned_text if len(cleaned_text) <= 80 else cleaned_text[:80] + "..."
            help_reply = (
                f"💡 *Bạn muốn làm video về chủ đề này?*\n"
                f"_{prompt_preview}_\n\n"
                f"👉 Bấm hoặc gõ: `/muse {cleaned_text}` để Meta Muse Spark tự động lên kịch bản và dựng video Whiteboard ngay!\n\n"
                f"*(Hoặc gửi link video Douyin, TikTok, Xiaohongshu... để tự động lồng tiếng)*"
            )
            await update.message.reply_text(help_reply, parse_mode="Markdown")
            return

        await update.message.reply_text(
            "❓ Hãy gửi link video (bắt đầu bằng http:// hoặc https://)\n"
            "Hoặc gõ `/muse <ý tưởng>` để tự động tạo video bằng Meta Muse Spark AI!\n"
            "Ví dụ: `/muse Tại sao cá voi sát thủ không bao giờ tấn công con người?`"
        )
        return

    import shared_state
    shared_state.stop_requested = False

    _ensure_video_worker()

    from ai.v1_auto_voice import get_auto_voice_mode
    captured_mode = get_auto_voice_mode(WORKSPACE)

    first_position = _reserve_queue_positions(len(urls))
    jobs = [
        prepare_telegram_job({
            'type': 'url',
            'pos': position,
            'update': update,
            'context': context,
            'url': url,
            'voice_mode': captured_mode,
        })
        for position, url in enumerate(urls, start=first_position)
    ]
    global_queue.put_many_nowait(jobs)

    await update.message.reply_text(
        f"✅ Đã thêm {len(urls)} link vào hàng đợi.\n"
        f"👉 Hàng đợi của bạn chạy từ thứ tự {first_position} đến {first_position + len(jobs) - 1}.\n"
        f"⏳ Hiện tại có tổng cộng {global_queue.qsize()} video đang chờ Bot xử lý lần lượt 1-1.",
        parse_mode="Markdown"
    )


# ===== XỬ LÝ VIDEO GỬI TRỰC TIẾP =====
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Người dùng gửi file video trực tiếp qua Telegram (Đẩy vào Queue)."""
    import shared_state
    shared_state.stop_requested = False

    if update.message.video:
        file_obj = update.message.video
        filename = update.message.video.file_name or f"tg_video_{int(time.time())}.mp4"
    elif update.message.document:
        file_obj = update.message.document
        filename = update.message.document.file_name or f"tg_doc_{int(time.time())}.mp4"
    else:
        await update.message.reply_text("❌ Không nhận dạng được file video.")
        return
        
    from ai.v1_auto_voice import get_auto_voice_mode
    captured_mode = get_auto_voice_mode(WORKSPACE)

    position = _reserve_queue_positions()
    _ensure_video_worker()
    await global_queue.put(prepare_telegram_job({
        'type': 'video',
        'pos': position,
        'update': update,
        'context': context,
        'file_id': file_obj.file_id,
        'filename': filename,
        'voice_mode': captured_mode,
    }))
    
    remaining = global_queue.qsize()
    await update.message.reply_text(
        f"✅ Đã thêm video tải lên vào hàng đợi.\n"
        f"👉 Vị trí của bạn: #{position}.\n"
        f"⏳ Hiện tại có tổng cộng {remaining} video đang chờ Bot xử lý lần lượt 1-1.",
        parse_mode="Markdown"
    )

async def process_single_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, filename: str,
                               pos: int, chat_id: int = None, voice_mode: str = None, retry_count: int = 0,
                               queue_job=None):
    job = queue_job if queue_job is not None else {
        "type": "video", "file_id": file_id, "filename": filename, "pos": pos,
        "update": update, "context": context, "chat_id": chat_id,
        "voice_mode": voice_mode, "retry_count": retry_count,
    }
    return await _process_telegram_job(job)


# ===== KHỞI CHẠY BOT =====
async def enqueue_interrupted_v2_jobs(application):
    """Put interrupted v2 jobs ahead of newly submitted work after restart."""

    global worker_task
    from pipeline_v2.config import PipelineMode, PipelineSettings

    settings = PipelineSettings.from_env()
    if settings.mode is not PipelineMode.V2:
        return
    from pipeline_v2.resume import find_resumable_jobs

    resumable_jobs = find_resumable_jobs(Path(WORKSPACE))
    for resumable in resumable_jobs:
        await global_queue.put({"type": "resume_v2", "job": resumable})
        logger.info(
            "Queued interrupted pipeline v2 job %s from stage %s",
            resumable.job_id,
            resumable.next_stage,
        )
    if resumable_jobs and (worker_task is None or worker_task.done()):
        worker_task = application.create_task(video_worker())


async def enqueue_pending_queue_jobs(application=None):
    """Tự động khôi phục các link còn tồn đọng trong telegram_queue.json sau khi khởi động hoặc rớt mạng."""
    global queue_counter, worker_task
    bot_system = Path(WORKSPACE) / "bot_system"
    bot_system_queue = bot_system / "telegram_queue.json"
    # Settings initialization can create bot_system before a legacy snapshot is
    # recovered. Prefer an existing snapshot rather than just its directory.
    queue_file = bot_system_queue if bot_system_queue.is_file() else (Path(WORKSPACE) / "telegram_queue.json")
    if not queue_file.is_file():
        _ensure_network_retry_scheduler()
        return 0
    try:
        import json
        from copy import deepcopy
        from queue_control_service import filter_restored, identity, submit_delete
        data = json.loads(queue_file.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("Snapshot hàng chờ không hợp lệ; giữ nguyên để khôi phục.")
        items = list(data["items"])
        active = data.get("active_job")
        if any(not isinstance(item, dict) for item in items) or (active is not None and not isinstance(active, dict)):
            raise ValueError("Bản ghi hàng chờ không hợp lệ; giữ nguyên để khôi phục.")
        clear_flag = queue_file.parent / "telegram_queue_clear.flag"
        if clear_flag.exists():
            # Clear requested while the bot was offline: retain the interrupted
            # active job and persist deletions before any worker can claim jobs.
            for item in items:
                if not active or identity(item) != identity(active):
                    submit_delete(queue_file, item)
            clear_network_retries()
            clear_flag.unlink(missing_ok=True)
        if active and not any(identity(active) == identity(item) for item in items):
            items.insert(0, active)
        items = filter_restored(queue_file, items, active)

        current = list(global_queue._queue)
        if global_queue.active_job:
            current.append(global_queue.active_job)
        existing = {identity(j) if isinstance(j, dict) else str(j) for j in current}
        prepared = []
        for item in items:
            target = item.get("url") or item.get("filename") or item.get("name") or item.get("video_path")
            if identity(item) in existing:
                continue
            kind = "url" if str(target).startswith("http") else item.get("type")
            if (not target or kind not in ("url", "video", "local_file")
                    or (kind == "video" and not item.get("file_id"))
                    or (kind == "local_file" and not item.get("video_path"))):
                raise ValueError("Job hàng chờ thiếu nguồn hoặc có loại không được hỗ trợ.")
            job = {field: deepcopy(item[field]) for field in PERSISTED_JOB_FIELDS if field in item}
            job.update(type=kind, pos=queue_counter + len(prepared) + 1,
                       url=(item.get("url") or target) if kind == "url" else "",
                       filename=item.get("filename") or target, update=None, context=None)
            prepared.append(prepare_telegram_job(job))
            existing.add(identity(item))

        # Validate and prepare the entire snapshot before mutating memory. A bad
        # later record must not publish a prefix and erase the remaining backlog.
        was_recovering = getattr(global_queue, 'recovery_pending', False)
        global_queue.recovery_pending = True
        for job in prepared:
            global_queue.put_nowait(job)
        queue_counter += len(prepared)
        global_queue.recovery_pending = was_recovering
        if not was_recovering:
            global_queue.publish()
        _ensure_network_retry_scheduler()
        restored = len(prepared)
        if restored > 0:
            logger.info(f"🔄 Đã tự động nạp {restored} video từ hàng chờ cũ vào xử lý.")
            if worker_task is None or worker_task.done():
                if application and hasattr(application, "create_task"):
                    worker_task = application.create_task(video_worker())
                else:
                    worker_task = asyncio.create_task(video_worker())
        return restored
    except Exception as e:
        logger.warning(f"Lỗi khi nạp hàng chờ từ telegram_queue.json: {e}")
        raise

async def queue_signal_watcher(application=None):
    """Lắng nghe tín hiệu kích hoạt xử lý hàng chờ hoặc xóa hàng chờ từ Dashboard."""
    global worker_task
    bot_system = Path(WORKSPACE) / "bot_system"
    flag_dir = bot_system if bot_system.is_dir() else Path(WORKSPACE)
    trigger_flag = flag_dir / "telegram_queue_trigger.flag"
    clear_flag = flag_dir / "telegram_queue_clear.flag"
    reorder_flag = flag_dir / "telegram_queue_reorder.flag"

    while True:
        try:
            from queue_control_service import apply_deletions, apply_reorders, submit_delete, submit_reorder
            from telegram_queue_monitor import SNAPSHOT
            apply_deletions(global_queue, SNAPSHOT)
            if clear_flag.exists():
                # Persist each pending identity before consuming the clear
                # request. A failed snapshot write or process crash must not
                # resurrect these jobs; the active job is deliberately excluded.
                for pending_job in list(global_queue._queue):
                    submit_delete(SNAPSHOT, pending_job)
                clear_flag.unlink(missing_ok=True)
                await _cancel_download_prefetches()
                global_queue.clear()
                clear_network_retries()
                logger.info("🗑️ Đã nhận lệnh xóa sạch hàng đợi từ Dashboard.")

            if reorder_flag.exists():
                try:
                    # One-time migration of an older dashboard's signal. New
                    # requests are immutable files and never edit the snapshot.
                    import json
                    qdata = json.loads(SNAPSHOT.read_text(encoding="utf-8-sig"))
                    submit_reorder(SNAPSHOT, qdata.get("items", []))
                    reorder_flag.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning("Lỗi cập nhật thứ tự hàng đợi: %s", e)
            apply_reorders(global_queue, SNAPSHOT)

            if trigger_flag.exists():
                try: trigger_flag.unlink(missing_ok=True)
                except Exception: pass
                restored = await enqueue_pending_queue_jobs(application)
                logger.info(f"▶️ Đã nhận lệnh kích hoạt hàng chờ từ Dashboard. Đã nạp {restored} video.")
                if worker_task is None or worker_task.done():
                    if application and hasattr(application, "create_task"):
                        worker_task = application.create_task(video_worker())
                    else:
                        worker_task = asyncio.create_task(video_worker())
        except Exception as e:
            logger.debug(f"Lỗi queue_signal_watcher: {e}")
        await asyncio.sleep(1.5)


def main():
    # Dam bao chi co duy nhat 1 tien trinh Telegram Bot chay tai 1 thoi diem

    bot_system = Path(WORKSPACE) / "bot_system"
    lock_dir = bot_system if bot_system.is_dir() else Path(WORKSPACE)
    lock_file_path = os.path.join(str(lock_dir), "bot_instance.lock")
    startup_fallback = _install_bot_startup_fallback()
    try:
        global _singleton_lock_file
        _singleton_lock_file = _acquire_bot_instance_lock(lock_file_path)
    except BotInstanceAlreadyRunning as exc:
        logger.warning("Bot instance already running; duplicate launch ignored: %s", exc)
        return
    except BotStartupError as exc:
        logger.error("%s", exc)
        raise SystemExit(2) from exc

    try:
        _configure_bot_logging()
    except Exception as exc:
        _emit_startup_diagnostic(_startup_failure_message(
            "telegram", "configure bot log", f"{backend_app_log};{root_app_log}", exc
        ))
        raise SystemExit(2) from exc
    finally:
        root_logger = logging.getLogger()
        if startup_fallback in root_logger.handlers:
            root_logger.removeHandler(startup_fallback)
        startup_fallback.close()

    if not BOT_TOKEN:
        logger.error("Startup failed: component=telegram operation=validate bot token error_type=MissingBotToken")
        raise SystemExit(2)
    import socket

    print("Dang khoi dong Telegram Bot...")
    print(f"Workspace: {WORKSPACE}")

    # Đợi kết nối mạng Internet trước khi khởi chạy (tránh lỗi DNS getaddrinfo khi vừa bật máy)
    for _ in range(30):
        try:
            socket.create_connection(("api.telegram.org", 443), timeout=3)
            break
        except Exception:
            time.sleep(2)

    from telegram.request import HTTPXRequest
    from ai.v1_gemini_dispatcher import start_gemini_model_catalog_refresh

    start_gemini_model_catalog_refresh()

    from telegram.error import NetworkError, TimedOut

    reconnect_delay = 5
    while True:
        try:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_closed():
                    asyncio.set_event_loop(asyncio.new_event_loop())
            except RuntimeError:
                asyncio.set_event_loop(asyncio.new_event_loop())

            # Tăng timeout lên 120 giây để không bị Timed out khi gửi/tải video lớn
            request = HTTPXRequest(
                connect_timeout=30,
                read_timeout=120,
                write_timeout=120,
                pool_timeout=120,
            )
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .request(request)
                .build()
            )
            global GLOBAL_BOT_APP
            GLOBAL_BOT_APP = app
            install_access_gate(app, load_allowed_chat_ids(), bot_name="v1")
            _install_download_prefetcher(app)

            async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
                from telegram.error import NetworkError, TimedOut, RetryAfter
                err = getattr(context, "error", None)
                if isinstance(err, RetryAfter):
                    logger.warning("Telegram Flood Control: tạm chờ %s giây.", getattr(err, "retry_after", 5))
                elif isinstance(err, (TimedOut, NetworkError)):
                    logger.warning(
                        "Telegram temporary network error (error_type=%s)", type(err).__name__
                    )
                else:
                    logger.error("Telegram event handler failed (error_type=%s)", type(err).__name__)

            # Đăng ký handlers
            app.add_error_handler(telegram_error_handler)
            app.add_handler(CommandHandler("chatid", cmd_chatid))
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("stop", cmd_stop))
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("cloud", cmd_cloud))
            app.add_handler(CommandHandler("cloud_status", cmd_cloud_status))
            app.add_handler(CommandHandler("batch", cmd_batch))
            app.add_handler(CommandHandler("local", cmd_batch))
            app.add_handler(CommandHandler("llm", cmd_llm))
            app.add_handler(CommandHandler("voice_auto", cmd_voice_auto))
            app.add_handler(CommandHandler("voice", cmd_voice_auto))
            app.add_handler(CommandHandler("sub_tts", cmd_sub_tts))
            app.add_handler(CommandHandler("ocr_tts", cmd_sub_tts))
            app.add_handler(CommandHandler("muse", cmd_muse))
            app.add_handler(CommandHandler("spark", cmd_muse))
            app.add_handler(CommandHandler("video", cmd_muse))
            app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

            print("Bot da san sang! Dang lang nghe tin nhan...")
            from tool_control_runtime import install as install_tool_control
            install_tool_control(app, globals(), 'v1')
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
            reconnect_delay = 5
        except (NetworkError, TimedOut) as exc:
            logger.warning(
                "Telegram network failure; retrying in %s seconds (error_type=%s)",
                reconnect_delay,
                type(exc).__name__,
            )
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
        except Exception as exc:
            logger.error(
                "Fatal Telegram startup or polling failure; retries stopped (error_type=%s)",
                type(exc).__name__,
            )
            raise SystemExit(3) from exc


if __name__ == "__main__":
    main()
