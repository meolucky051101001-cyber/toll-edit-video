"""
Telegram Bot - Auto Video Dubbing
Gửi link video → Bot tự động tải, tạo phụ đề, lồng tiếng, gửi lại video.
"""
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

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

try:
    from .environment import load_environment, read_environment
    from .social_downloader import (
        SensitiveUrlFilter,
        sanitize_exception,
        sanitize_text,
        sanitize_url,
    )
except ImportError:
    from environment import load_environment, read_environment
    from social_downloader import (
        SensitiveUrlFilter,
        sanitize_exception,
        sanitize_text,
        sanitize_url,
    )

load_environment(Path(__file__).resolve().parent)
# Đảm bảo BOT_TOKEN và BOT_EXPECTED_USERNAME luôn ưu tiên file .env của Tool V2,
# ngăn ngừa hoàn toàn nguy cơ bị rò rỉ hoặc ghi đè từ tiến trình Tool V1 qua tool_control
_local_env = read_environment(Path(__file__).resolve().parent, environment={})
if _local_env.get("BOT_TOKEN"):
    os.environ["BOT_TOKEN"] = _local_env["BOT_TOKEN"]
if _local_env.get("BOT_EXPECTED_USERNAME"):
    os.environ["BOT_EXPECTED_USERNAME"] = _local_env["BOT_EXPECTED_USERNAME"]

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
BOT_EXPECTED_USERNAME = os.getenv("BOT_EXPECTED_USERNAME", "").strip().lstrip("@")
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "AutoDub Video Bot V2").strip()
BOT_SHORT_DESCRIPTION = os.getenv(
    "BOT_SHORT_DESCRIPTION",
    "Lồng tiếng video tự động bằng Pipeline V2.",
).strip()
BOT_DESCRIPTION = os.getenv(
    "BOT_DESCRIPTION",
    "Gửi link hoặc video để tải sạch, nhận dạng lời thoại, dịch, lồng tiếng, đồng bộ thời gian và kiểm tra chất lượng.",
).strip()

# Import các module xử lý từ backend
sys.path.insert(0, os.path.dirname(__file__))

# Đảm bảo console hỗ trợ UTF-8 để không bị lỗi UnicodeEncodeError
import io
if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding='utf-8')
if isinstance(sys.stderr, io.TextIOWrapper):
    sys.stderr.reconfigure(encoding='utf-8')

from ai.transcription import save_srt
from ai.v1_asr_isolated import extract_subtitles_isolated
from ai.translation import translate_subtitles
from ai.voice_cloning import rvc_runtime_available
from ai.v1_voice_isolated import generate_dubbing_audio_isolated
from url_utils import extract_http_urls
from social_downloader import sanitize_url
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video

try:
    from config.paths import AppPaths
except ImportError:
    from backend.config.paths import AppPaths

PATHS = AppPaths.from_environment(Path(__file__).resolve().parent.parent)
WORKSPACE = str(PATHS.workspace)
INPUT_DIR = str(PATHS.input_dir)
OUTPUT_DIR = str(PATHS.output_dir)
os.makedirs(WORKSPACE, exist_ok=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
root_app_log = os.path.abspath(os.path.join(BASE_DIR, "..", "app.log"))
backend_app_log = os.path.abspath(os.path.join(BASE_DIR, "app.log"))
log_handlers = [
    logging.FileHandler(backend_app_log, encoding='utf-8'),
    logging.StreamHandler()
]
try:
    log_handlers.append(logging.FileHandler(root_app_log, encoding='utf-8'))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=log_handlers,
    force=True
)
logger = logging.getLogger(__name__)
logger.info("=== AUTO VIDEO DUBBING TOOL V1 STARTED === [PID: %d] [CODEX SINGLE VOICE LOCK ACTIVE] [REPAIR: v1_independent_repair_20260930]", os.getpid())
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_COMMANDS = (
    BotCommand("start", "Xem hướng dẫn sử dụng AutoDub V2"),
    BotCommand("status", "Xem hàng đợi và tiến độ xử lý"),
    BotCommand("batch", "Xử lý video trong thư mục đầu vào"),
    BotCommand("local", "Chạy batch video cục bộ"),
    BotCommand("llm", "Chọn nhà cung cấp AI dịch thuật"),
    BotCommand("stop", "Dừng công việc đang xử lý"),
)


async def configure_bot_profile(application):
    """Validate the dedicated V2 identity and publish its Telegram profile."""

    bot = application.bot
    identity = await bot.get_me()
    actual_username = (identity.username or "").lstrip("@")
    if BOT_EXPECTED_USERNAME and actual_username.lower() != BOT_EXPECTED_USERNAME.lower():
        raise RuntimeError(
            "BOT_TOKEN belongs to @{}, expected @{}".format(
                actual_username or "unknown",
                BOT_EXPECTED_USERNAME,
            )
        )

    try:
        await bot.set_my_commands(BOT_COMMANDS)
        await bot.set_my_name(BOT_DISPLAY_NAME)
        await bot.set_my_short_description(BOT_SHORT_DESCRIPTION)
        await bot.set_my_description(BOT_DESCRIPTION)
        logger.info("Telegram V2 profile configured for @%s", actual_username)
    except Exception as exc:
        # Profile metadata is useful but must not take the rendering bot offline.
        logger.warning("Could not update Telegram V2 profile: %s", exc)


async def telegram_error_handler(update, context):
    """Keep transient Telegram polling failures visible without noisy tracebacks."""

    error = context.error
    if isinstance(error, NetworkError):
        logger.warning("Telegram network error; polling will retry: %s", error)
        return
    logger.error(
        "Unhandled Telegram update error",
        exc_info=(type(error), error, error.__traceback__),
    )

async def safe_edit_status(status_msg, text, parse_mode=None, retries=3):
    """
    Cập nhật status message trên Telegram an toàn, chống bị crash tiến trình
    khi mạng Internet bị giật hoặc lỗi Markdown entities.
    """
    from telegram_progress import report
    report(text)
    if not status_msg:
        return

    # Khử lỗi Markdown khi văn bản chứa tên file có dấu gạch dưới lẻ hoặc ký tự định dạng chưa đóng
    current_parse_mode = parse_mode
    if current_parse_mode == "Markdown":
        if text.count("_") % 2 != 0 or text.count("*") % 2 != 0:
            current_parse_mode = None

    for attempt in range(retries):
        try:
            await status_msg.edit_text(text, parse_mode=current_parse_mode)
            return
        except Exception as e:
            err_str = str(e).lower()
            if "not modified" in err_str:
                return
            if "parse" in err_str or "entity" in err_str:
                current_parse_mode = None
                try:
                    await status_msg.edit_text(text, parse_mode=None)
                    return
                except Exception as e2:
                    if "not modified" in str(e2).lower():
                        return
                    pass
                continue
            logger.warning(f"Lỗi cập nhật status Telegram (Lần {attempt+1}/{retries}): {e}")
            if "parse" in str(e).lower() or "entity" in str(e).lower():
                parse_mode = None
            if attempt < retries - 1:
                await asyncio.sleep(1.0)


async def run_pipeline_v2_for_telegram(
    video_path, out_dir, final_video, status_msg, delivery_copy_path=None
):
    """Build and run one production V2 request for the Telegram adapter."""

    from pipeline_v2.config import PipelineSettings
    from pipeline_v2.video_pipeline import (
        VideoPipelineRequest,
        VideoPipelineRunner,
        discover_rvc_model,
    )

    settings = PipelineSettings.from_env()
    # Preserve the recorded voice and request when restarting this queue item.
    from pipeline_v2.resume import find_resumable_jobs, resume_video_job, _published_outputs_present
    from pipeline_v2.manifest import ManifestStore
    manifest_path = Path(out_dir) / 'pipeline_v2' / 'job_manifest.json'
    if manifest_path.is_file():
        manifest = ManifestStore(manifest_path.parent).load()
        from pipeline_v2.artifact_store import hash_file
        source_hash, _ = await asyncio.to_thread(hash_file, video_path)
        if source_hash == manifest.fingerprints.source_sha256 and _published_outputs_present(manifest):
            return
        for resumable in find_resumable_jobs(Path(out_dir).parent):
            if resumable.job_directory.resolve() == Path(out_dir).resolve():
                async def resume_progress(job_id, stage, state):
                    await safe_edit_status(status_msg, f'V2: {stage} — {state}')
                return await resume_video_job(resumable, settings, api_key=GEMINI_API_KEY,
                                              tts_api_key=FPT_API_KEY, progress=resume_progress)
    rvc_model = discover_rvc_model(Path(WORKSPACE))

    async def progress(stage, state):
        await safe_edit_status(
            status_msg,
            "⚙️ Pipeline v2: `{}` — {}".format(stage, state),
            parse_mode="Markdown",
        )

    from voice_selection import resolve_voice, get_speaker_voice_map, get_speaker_map, is_dual_voice_enabled
    from dataclasses import replace
    dual_voice_on = is_dual_voice_enabled()
    settings = replace(settings, enable_auto_gender=dual_voice_on)
    selected_source, selected_param, selected_label = resolve_voice("rvc" if rvc_model else "edge", rvc_model)
    request = VideoPipelineRequest(
        video_path=Path(video_path),
        job_directory=Path(out_dir),
        output_path=Path(final_video),
        delivery_copy_path=(
            Path(delivery_copy_path) if delivery_copy_path else None
        ),
        settings=settings,
        api_key=GEMINI_API_KEY,
        voice_source=selected_source,
        voice_param=selected_param,
        rvc_model_path=Path(selected_param) if selected_source == "rvc" else rvc_model,
        speaker_map=get_speaker_map() if dual_voice_on else None,
        speaker_voice_map=get_speaker_voice_map() if dual_voice_on else None,
        progress=progress,
    )
    return await VideoPipelineRunner(request).run()


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
    )

    # Theo yêu cầu: không gửi file video lên Telegram mà lưu trực tiếp về máy vào thư mục định sẵn
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


def snapshot_legacy_telegram_run(
    video_path, out_dir, artifacts, run_started_at_epoch=None
):
    from pipeline_v2.config import PipelineMode, PipelineSettings

    if PipelineSettings.from_env().mode is not PipelineMode.SHADOW:
        return
    from pipeline_v2.shadow import snapshot_completed_legacy_run

    snapshot_completed_legacy_run(
        Path(video_path),
        Path(out_dir) / "pipeline_v2_shadow",
        artifacts,
        run_started_at_epoch=run_started_at_epoch,
    )

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
        "6️⃣ Xuất video chất lượng cao vào thư mục đầu ra đã cấu hình.\n\n"
        "📌 *Lệnh hỗ trợ:*\n"
        "• `/vram` hoặc `/gpu` - Giám sát VRAM & nhiệt độ GPU NVIDIA thời gian thực\n"
        "• `/workers` hoặc `/luong` - Cài đặt số luồng xử lý (1 luồng ổn định / 2 luồng so le)\n"
        "• `/script` hoặc `/kichban` - Cấu hình phong cách kịch bản (✨ Mặc định / 🤣 Hài hước)\n"
        "• `/voice_auto [on|off]` - Bật/tắt tự nhận diện giọng nói đầu video (Tool V1)\n"
        "• `/llm` - Cấu hình mô hình AI dịch thuật (Google Gemini / OpenAI GPT-4o / DeepSeek V4)\n"
        "• `/batch` - Tự động quét & edit hàng loạt video trong thư mục `D:\\video_input` trên máy\n"
        "• `/batch D:\\thu_muc` - Chỉ định thư mục chứa video cần edit\n"
        "• `/status` - Kiểm tra trạng thái hàng đợi & cấu hình\n"
        "• `/stop` - Dừng khẩn cấp toàn bộ tác vụ"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")



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
    female_label = female_v["label"] if female_v else "Chí Mai · RVC"
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


# ===== LỆNH /script & /kichban (Chọn phong cách kịch bản dịch thuật) =====
async def cmd_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Cấu hình phong cách kịch bản dịch thuật (Tool V1).
    Cú pháp:
    /script          - Mở menu chọn trực quan
    /script macdinh  - Chuyển sang kịch bản Mặc định
    /script haihuoc  - Chuyển sang kịch bản Hài hước
    """
    from audio_settings import get_audio_settings, save_audio_settings

    args = context.args
    cur_settings = get_audio_settings()
    current_mode = cur_settings.get("script_mode", "default")

    if args:
        arg = args[0].strip().lower()
        if arg in ("humorous", "haihuoc", "hai_huoc", "hai", "comedy", "troll"):
            new_mode = "humorous"
        elif arg in ("default", "macdinh", "mac_dinh", "chuan", "standard"):
            new_mode = "default"
        else:
            await update.message.reply_text(
                "❌ Cú pháp không hợp lệ. Vui lòng chọn:\n"
                "• `/script macdinh` - Kịch bản Mặc định\n"
                "• `/script haihuoc` - Kịch bản Hài hước\n"
                "Hoặc gõ `/script` để bấm nút chọn trực quan.",
                parse_mode="Markdown"
            )
            return

        save_audio_settings(
            cur_settings.get("bgm_volume_db", -2.0),
            cur_settings.get("dubbing_volume_db", 1.0),
            cur_settings.get("separation_mode", "roformer"),
            cur_settings.get("ducking_mode", "soft"),
            script_mode=new_mode,
        )
        lbl = "🤣 *Hài hước* (Dí dỏm, tấu hài, bắt trend TikTok/Douyin)" if new_mode == "humorous" else "✨ *Mặc định* (Tự nhiên, chuẩn xác, ngữ cảnh gốc)"
        await update.message.reply_text(
            f"✅ *Đã chuyển kịch bản video sang:*\n{lbl}\n\n"
            f"*(Áp dụng cho tất cả video mới gửi vào hàng đợi từ thời điểm này)*",
            parse_mode="Markdown"
        )
        return

    # Khi không có tham số: Hiển thị bảng chọn Inline Keyboard
    is_humorous = (current_mode == "humorous")
    btn_default = InlineKeyboardButton(
        f"{'🟢 ' if not is_humorous else ''}✨ Mặc định (Chuẩn xác)",
        callback_data="set_script:default"
    )
    btn_humorous = InlineKeyboardButton(
        f"{'🟢 ' if is_humorous else ''}🤣 Hài hước (Tấu hài)",
        callback_data="set_script:humorous"
    )
    keyboard = InlineKeyboardMarkup([[btn_default], [btn_humorous]])

    status_str = "🤣 *Hài hước* (Dí dỏm, bắt trend)" if is_humorous else "✨ *Mặc định* (Chuẩn xác, tự nhiên)"
    msg_text = (
        f"🎭 *CẤU HÌNH PHONG CÁCH KỊCH BẢN (TOOL V1)*\n\n"
        f"📍 *Kịch bản đang chọn:* {status_str}\n\n"
        f"📋 *Mô tả các chế độ:*\n"
        f"1️⃣ *Mặc định (Default):*\n"
        f"   • Dịch sát ngữ cảnh, tự nhiên, sang trọng.\n"
        f"   • Giữ chuẩn thuật ngữ sản phẩm, chi tiết kỹ thuật.\n\n"
        f"2️⃣ *Hài hước (Humorous / Comedy):*\n"
        f"   • Phong cách tấu hài, bắt trend TikTok/Douyin triệu view.\n"
        f"   • Chêm từ lóng, thán từ dí dỏm duyên dáng (*ối giồi ôi, cứu tui, bất ngờ chưa bà già, ảo ma...*).\n"
        f"   • Vẫn đảm bảo 100% khớp khẩu hình và đúng thông số gốc.\n\n"
        f"👉 *Bấm nút bên dưới để chọn chế độ:*"
    )
    await update.message.reply_text(msg_text, reply_markup=keyboard, parse_mode="Markdown")


async def callback_script_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if not data or not data.startswith("set_script:"):
        return

    new_mode = data.split(":", 1)[1]
    if new_mode not in ("default", "humorous"):
        new_mode = "default"

    from audio_settings import get_audio_settings, save_audio_settings
    cur = get_audio_settings()
    save_audio_settings(
        cur.get("bgm_volume_db", -2.0),
        cur.get("dubbing_volume_db", 1.0),
        cur.get("separation_mode", "roformer"),
        cur.get("ducking_mode", "soft"),
        script_mode=new_mode,
    )

    is_humorous = (new_mode == "humorous")
    btn_default = InlineKeyboardButton(
        f"{'🟢 ' if not is_humorous else ''}✨ Mặc định (Chuẩn xác)",
        callback_data="set_script:default"
    )
    btn_humorous = InlineKeyboardButton(
        f"{'🟢 ' if is_humorous else ''}🤣 Hài hước (Tấu hài)",
        callback_data="set_script:humorous"
    )
    keyboard = InlineKeyboardMarkup([[btn_default], [btn_humorous]])

    status_str = "🤣 *Hài hước* (Dí dỏm, bắt trend)" if is_humorous else "✨ *Mặc định* (Chuẩn xác, tự nhiên)"
    msg_text = (
        f"🎭 *CẤU HÌNH PHONG CÁCH KỊCH BẢN (TOOL V1)*\n\n"
        f"✅ *Đã cập nhật kịch bản sang:* {status_str}\n\n"
        f"📋 *Mô tả các chế độ:*\n"
        f"1️⃣ *Mặc định (Default):*\n"
        f"   • Dịch sát ngữ cảnh, tự nhiên, sang trọng.\n"
        f"   • Giữ chuẩn thuật ngữ sản phẩm, chi tiết kỹ thuật.\n\n"
        f"2️⃣ *Hài hước (Humorous / Comedy):*\n"
        f"   • Phong cách tấu hài, bắt trend TikTok/Douyin triệu view.\n"
        f"   • Chêm từ lóng, thán từ dí dỏm duyên dáng (*ối giồi ôi, cứu tui, bất ngờ chưa bà già, ảo ma...*).\n"
        f"   • Vẫn đảm bảo 100% khớp khẩu hình và đúng thông số gốc.\n\n"
        f"👉 *Bấm nút bên dưới nếu bạn muốn đổi lại:*"
    )
    try:
        await query.edit_message_text(msg_text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception:
        pass


# ===== LỆNH /vram & /gpu (Giám sát phần cứng thời gian thực) =====
async def cmd_vram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Báo cáo tình trạng VRAM và GPU NVIDIA hiện tại."""
    from v1_vram_monitor import get_vram_stats
    from worker_settings import get_worker_settings
    stats = get_vram_stats(force_refresh=True)
    w_cfg = get_worker_settings()
    conc = w_cfg.get("concurrency", 1)

    text = (
        "🎮 *THÔNG TIN GPU & VRAM THỜI GIAN THỰC*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🖥 *Thiết bị:* `{stats.get('gpu_name', 'NVIDIA GPU')}`\n"
        f"📊 *VRAM đã dùng:* *{stats.get('used_gb', 0):.2f} GB* / {stats.get('total_gb', 6):.2f} GB ({stats.get('percent', 0):.1f}%)\n"
        f"💾 *Khả dụng:* *{stats.get('free_gb', 0):.2f} GB*\n"
        f"🌡 *Nhiệt độ:* {stats.get('temperature_c', 0)}°C | *Tải GPU:* {stats.get('gpu_util_percent', 0)}%\n"
        f"🛡 *Trạng thái:* {stats.get('status_desc', '🟢 An toàn')}\n"
        f"⚙️ *Số luồng hiện tại:* *{conc} luồng* {'(Ổn định)' if conc == 1 else '(⚡ So le GPU)'}\n"
        f"💡 *Đánh giá:* _{stats.get('recommendation', 'N/A')}_\n\n"
        "👉 _Dùng lệnh /workers hoặc /luong để thay đổi số luồng._"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ===== LỆNH /workers & /luong (Cấu hình số luồng xử lý) =====
async def cmd_workers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cấu hình số luồng chạy song song (1 luồng hoặc 2 luồng so le)."""
    from worker_settings import get_worker_settings, save_worker_settings
    args = context.args
    cfg = get_worker_settings()
    conc = cfg.get("concurrency", 1)

    if args:
        val = args[0].strip()
        if val in ("1", "one", "don", "tuan_tu"):
            new_conc = 1
        elif val in ("2", "two", "doi", "song_song"):
            new_conc = 2
        else:
            await update.message.reply_text(
                "❌ Cú pháp không hợp lệ. Vui lòng chọn:\n"
                "• `/workers 1` - Chế độ 1 luồng (Ổn định)\n"
                "• `/workers 2` - Chế độ 2 luồng (So le GPU)\n"
                "Hoặc gõ `/workers` để bấm nút chọn trực quan.",
                parse_mode="Markdown"
            )
            return

        save_worker_settings(new_conc)
        ensure_workers_running()
        lbl = "⚡ *2 Luồng* (So le GPU Gatekeeper, tăng tốc an toàn)" if new_conc == 2 else "🟢 *1 Luồng* (Xử lý tuần tự, ổn định tuyệt đối)"
        await update.message.reply_text(f"✅ *Đã cập nhật số luồng:*\n{lbl}", parse_mode="Markdown")
        return

    # Hiển thị Inline Keyboard
    keyboard = [
        [
            InlineKeyboardButton(
                ("🟢 " if conc == 1 else "") + "1 Luồng (Ổn định nhất)",
                callback_data="set_worker_1"
            ),
            InlineKeyboardButton(
                ("🟢 " if conc == 2 else "") + "⚡ 2 Luồng (So le GPU)",
                callback_data="set_worker_2"
            )
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = (
        "⚙️ *CẤU HÌNH SỐ LUỒNG XỬ LÝ VIDEO (TOOL V1)*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 *Chế độ hiện tại:* *{conc} luồng*\n\n"
        "• *1 Luồng (Mặc định):* Xử lý tuần tự từng video một. An toàn tuyệt đối 100%, không lo quá nhiệt hay nghẽn RAM.\n"
        "• *2 Luồng (So le GPU):* Chạy song song 2 video qua cơ chế GPU Gatekeeper. Các bước Dịch Gemini & TTS chạy đồng thời, bước GPU tách âm xếp hàng lần lượt. Nhanh hơn 35-50% mà không gây sập VRAM.\n\n"
        "⚠️ *Lưu ý:* Card RTX 4050 6GB _không hỗ trợ 3 luồng_ để chống tràn VRAM (CUDA OOM).\n\n"
        "👉 *Bấm nút bên dưới để chọn chế độ:*"
    )
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def callback_worker_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    from worker_settings import save_worker_settings
    if query.data == "set_worker_1":
        save_worker_settings(1)
        ensure_workers_running()
        msg = "✅ Đã chuyển sang chế độ *1 Luồng* (Xử lý tuần tự, ổn định tuyệt đối)."
    elif query.data == "set_worker_2":
        save_worker_settings(2)
        ensure_workers_running()
        msg = "⚡ Đã bật chế độ *2 Luồng* (So le GPU Gatekeeper, tăng tốc an toàn)."
    else:
        return

    try:
        await query.edit_message_text(msg, parse_mode="Markdown")
    except Exception:
        pass


# ===== LỆNH /status =====
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from audio_settings import get_audio_settings
    from ai.v1_auto_voice import get_auto_voice_enabled, get_manual_voice_info
    from v1_vram_monitor import get_vram_stats
    from worker_settings import get_worker_settings

    cur = get_audio_settings()
    sm = cur.get("script_mode", "default")
    script_str = "🤣 Hài hước (Tấu hài TikTok/Douyin)" if sm == "humorous" else "✨ Mặc định (Tự nhiên, chuẩn xác)"
    auto_v = "🟢 Bật" if get_auto_voice_enabled(WORKSPACE) else "⚪ Tắt (Thủ công)"
    manual_v = get_manual_voice_info(WORKSPACE).get("label", "Mặc định")
    queue_len = global_queue.qsize()

    v_stats = get_vram_stats()
    w_cfg = get_worker_settings()
    conc = w_cfg.get("concurrency", 1)
    vram_str = f"{v_stats.get('used_gb', 0):.1f}GB/{v_stats.get('total_gb', 6):.1f}GB ({v_stats.get('percent', 0):.0f}%) · {v_stats.get('status_desc', '🟢 An toàn')}"
    worker_str = f"{conc} luồng {'(Ổn định)' if conc == 1 else '(⚡ So le GPU)'}"

    await update.message.reply_text(
        "✅ *Bot đang hoạt động!*\n\n"
        f"🎮 *VRAM GPU:* `{vram_str}`\n"
        f"⚙️ *Số luồng chạy:* `{worker_str}`\n"
        f"🎭 *Kịch bản dịch thuật:* `{script_str}`\n"
        f"🎙️ *Auto Voice:* {auto_v} (Giọng thủ công: `{manual_v}`)\n"
        f"⏳ *Hàng đợi chờ xử lý:* {queue_len} video\n"
        f"📂 *Workspace:* `{WORKSPACE}`\n\n"
        "🎯 Gửi link video hoặc file video để bắt đầu.",
        parse_mode="Markdown"
    )



# ===== LỆNH /batch =====
async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý hàng loạt video từ thư mục cục bộ đã cấu hình.
    Cú pháp: /batch hoặc /batch <đường_dẫn_thư_mục>
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
        
    output_dir = OUTPUT_DIR
    
    if not os.path.exists(input_dir):
        os.makedirs(input_dir, exist_ok=True)
        await update.message.reply_text(
            f"📁 *Đã tạo thư mục đầu vào:* `{input_dir}`\n\n"
            f"👉 Bạn hãy copy/thả các file video (.mp4, .mkv, .mov...) cần edit vào thư mục `{input_dir}`, sau đó gõ lại lệnh `/batch` để Bot tự động xử lý lần lượt nhé!",
            parse_mode="Markdown"
        )
        return
        
    from batch_processor import SUPPORTED_EXTENSIONS
    video_files = [
        f for f in os.listdir(input_dir)
        if f.lower().endswith(SUPPORTED_EXTENSIONS) and not f.startswith("Dubbed_")
        and Path(input_dir, f).is_file()
    ]
    
    if not video_files:
        await update.message.reply_text(
            f"📂 Thư mục `{input_dir}` hiện đang trống!\n\n"
            f"👉 Hãy thả các file video (.mp4, .mkv, .mov...) vào `{input_dir}` rồi gõ lại lệnh `/batch` nhé.",
            parse_mode="Markdown"
        )
        return
        
    added = 0
    chat_id = update.effective_chat.id if update and getattr(update, 'effective_chat', None) else None
    for filename in sorted(video_files):
        accepted = await global_queue.put({'type':'local', 'path':str(Path(input_dir, filename).resolve()),
            'update':update, 'context':context, 'chat_id':chat_id, 'pos':0})
        added += accepted is not None
    ensure_worker(context.application)
    await update.message.reply_text(f"Đã lưu {added} video vào hàng đợi V2.")
    return


async def cmd_llm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xem hoặc chuyển đổi nhà cung cấp AI dịch thuật (Gemini / OpenAI ChatGPT / DeepSeek)."""
    args = context.args
    current_provider = os.getenv("LLM_PROVIDER", "auto").lower()
    
    gemini_status = "🟢 Đã nạp Key" if os.getenv("GEMINI_API_KEY") else "⚪ Chưa cấu hình"
    openai_status = "🟢 Đã nạp Key" if os.getenv("OPENAI_API_KEY") else "⚪ Chưa cấu hình"
    deepseek_status = "🟢 Đã nạp Key" if os.getenv("DEEPSEEK_API_KEY") else "⚪ Chưa cấu hình"
    
    if not args:
        await update.message.reply_text(
            f"🧠 *CẤU HÌNH NHÀ CUNG CẤP AI DỊCH THUẬT (LLM)*\n\n"
            f"📍 *Chế độ ưu tiên hiện tại:* `{current_provider.upper()}`\n\n"
            f"🔹 **Google Gemini (3.8 Flash + fallback):** {gemini_status}\n"
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
    import job_tracker
    job_tracker.request_stop()
    
    # 1. Hủy ngay lập tức các worker task nếu đang chạy
    global worker_task, worker_tasks
    for t in worker_tasks:
        if not t.done():
            t.cancel()
    worker_tasks = []
    worker_task = None

    
    # 2. Xóa sạch hàng đợi
    while not global_queue.empty():
        try:
            await asyncio.wait_for(asyncio.shield(worker_task), timeout=4.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
            
    global queue_counter
    queue_counter = 0
            
    await update.message.reply_text("🛑 Đang dừng toàn bộ quá trình tải, bóc tách và render video...")
    
    # 3. Tiêu diệt tất cả các tiến trình con (yt-dlp, ffmpeg, demucs, ffprobe, separator_worker...)
    try:
        from process_killer import terminate_worker_processes
        killed = terminate_worker_processes()
        await update.message.reply_text(f"✅ Đã tiêu diệt xong các tiến trình chạy ngầm ({len(killed)} tiến trình).")
    except Exception as e:
        logger.error(f"Error stopping children: {e}")
        await update.message.reply_text("⚠️ Đã dừng hàng đợi nhưng có cảnh báo khi kiểm tra tiến trình con.")
    finally:
        # Khởi tạo lại worker task sẵn sàng cho các video tiếp theo mà không cần khởi động lại bot
        shared_state.stop_requested = False
        ensure_worker(context.application)

    job_tracker.mark_stopped("Đã dừng theo lệnh /stop từ Telegram.")

import re
from durable_adapter import DurableQueue

from telegram_queue_monitor import MonitoredQueue
global_queue = MonitoredQueue()
queue_counter = 0
worker_task = None
worker_tasks = []
GLOBAL_BOT_APP = None

def ensure_workers_running(app=None):
    """Đảm bảo số lượng worker tasks tương ứng với cấu hình worker_settings (1 hoặc 2)."""
    global worker_task, worker_tasks, GLOBAL_BOT_APP
    target_app = app or GLOBAL_BOT_APP
    from worker_settings import get_effective_concurrency
    conc = get_effective_concurrency()

    # Lọc các worker còn sống
    worker_tasks = [t for t in worker_tasks if not t.done()]
    while len(worker_tasks) < conc:
        worker_id = len(worker_tasks) + 1
        if target_app:
            try:
                t = target_app.create_task(video_worker(worker_id=worker_id))
            except Exception:
                t = asyncio.create_task(video_worker(worker_id=worker_id))
        else:
            t = asyncio.create_task(video_worker(worker_id=worker_id))
        worker_tasks.append(t)

    if worker_tasks:
        worker_task = worker_tasks[0]



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

async def video_worker(worker_id: int = 1):
    while True:
        try:
            import shared_state
            if getattr(shared_state, 'stop_requested', False):
                while not global_queue.empty():
                    try:
                        global_queue.get_nowait()
                        global_queue.task_done()
                    except:
                        pass
                global queue_counter
                queue_counter = 0
                import job_tracker
                job_tracker.mark_stopped("Đã dừng tiến trình!")
                await asyncio.sleep(1)
                continue

            # NẾU LÀ WORKER 2: KIỂM TRA SỐ LUỒNG HIỆU LỰC (EFFECTIVE CONCURRENCY)
            if worker_id > 1:
                from worker_settings import get_effective_concurrency
                if get_effective_concurrency() < worker_id:
                    # Hệ thống đang hạ về 1 luồng để bảo vệ VRAM: Worker 2 nhường sân và ngủ chờ
                    await asyncio.sleep(2)
                    continue

            job = await global_queue.get()
            if getattr(shared_state, 'stop_requested', False):
                global_queue.task_done()
                continue

            if worker_id > 1:
                from worker_settings import get_effective_concurrency
                if get_effective_concurrency() < worker_id:
                    # Trả lại job vào hàng đợi cho worker 1 xử lý an toàn
                    await global_queue.put(job)
                    global_queue.task_done()
                    await asyncio.sleep(2)
                    continue


            import job_tracker
            tracker_job = None
            from telegram_progress import current_job, current_worker_id
            progress_token = None
            worker_token = current_worker_id.set(worker_id)
            try:
                wait_notice_sent = False
                while tracker_job is None:
                    if getattr(shared_state, 'stop_requested', False):
                        break
                    try:
                        tracker_job = job_tracker.start_batch(1, output_dir=r"D:\banve")
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
                if tracker_job is None:
                    continue

                try:
                    from v1_gpu_gatekeeper import set_gpu_context_id
                    set_gpu_context_id(f"worker_{worker_id}")
                except Exception:
                    pass

                if isinstance(job, dict):
                    job_voice_mode = job.get('voice_mode')
                    if not job_voice_mode:
                        from ai.v1_auto_voice import get_auto_voice_mode
                        job_voice_mode = get_auto_voice_mode(WORKSPACE)

                    if job['type'] == 'url':
                        await process_single_url(
                            job.get('update'), job.get('context'), job['url'], job.get('pos', 1),
                            chat_id=job.get('chat_id'), voice_mode=job_voice_mode,
                            retry_count=job.get('retry_count', 0),
                            video_mode=job.get('video_mode'), job_overrides=job.get('job_overrides'),
                            resume_state=job.get('resume_state'),
                        )
                    elif job['type'] == 'video':
                        await process_single_video(
                            job.get('update'), job.get('context'), job.get('file_id'),
                            job.get('filename') or 'video.mp4', job.get('pos', 1),
                            chat_id=job.get('chat_id'), voice_mode=job_voice_mode,
                            retry_count=job.get('retry_count', 0),
                            video_mode=job.get('video_mode'), job_overrides=job.get('job_overrides'),
                            resume_state=job.get('resume_state'),
                        )
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
                    pos, update, context, url = job
                    from ai.v1_auto_voice import get_auto_voice_mode
                    await process_single_url(update, context, url, pos, voice_mode=get_auto_voice_mode(WORKSPACE))
            except asyncio.CancelledError:
                if tracker_job:
                    job_tracker.mark_stopped()
                logger.info("Worker task cancelled by /stop.")
                break
            except Exception as e:
                if tracker_job:
                    job_tracker.fail_batch(str(e), job_id=tracker_job)
                logger.error(f"Worker error: {e}")
                err_str = str(e)
                if "Download failed:" not in err_str:
                    try:
                        await job['update'].message.reply_text(
                            f"⚠️ V2 xử lý lỗi: {err_str[:150]}\nCông việc đã được lưu với trạng thái lỗi; hãy gửi lại nếu muốn thử lại."
                        )
                    except Exception:
                        logger.warning("Could not report job failure")
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
                    if state.get("video_status") == "completed":
                        job_tracker.finish_batch()
                    elif state.get("active"):
                        job_tracker.fail_batch("Video kết thúc mà chưa xác nhận thành phẩm.", job_id=tracker_job)
                    job_tracker.release_batch(tracker_job)
                if progress_token is not None:
                    current_job.reset(progress_token)
        except asyncio.CancelledError:
            logger.info("Worker queue cancelled.")
            break

async def process_single_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, pos: int = 1, chat_id: int = None, voice_mode: str = None, retry_count: int = 0, video_mode: str = None, job_overrides: dict = None, resume_state: dict = None):
    import job_tracker
    job_tracker.start_video("Video từ Telegram", pos, pos + global_queue.qsize())
    original_url = url
    target_chat_id = chat_id or (update.message.chat_id if update and hasattr(update, 'message') and update.message else None)

    # Thông báo bắt đầu
    remaining = global_queue.qsize()
    status_msg = None
    if update and hasattr(update, 'message') and update.message:
        try:
            status_msg = await update.message.reply_text(
                f"▶️ *Đang xử lý Video thứ {pos} trong hàng đợi:*\n`{url}`\n\n"
                f"⏳ Phía sau còn {remaining} video đang chờ...",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    elif target_chat_id and GLOBAL_BOT_APP and hasattr(GLOBAL_BOT_APP, "bot"):
        try:
            status_msg = await GLOBAL_BOT_APP.bot.send_message(
                chat_id=target_chat_id,
                text=f"▶️ *Đang tiếp tục xử lý Video thứ {pos} từ hàng chờ:*\n`{url}`\n\n⏳ Phía sau còn {remaining} video đang chờ...",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    import subprocess
    import sys
    CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0

    download_dir = os.path.join(WORKSPACE, "downloads")
    os.makedirs(download_dir, exist_ok=True)
    timestamp = str(int(time.time()))
    
    # Lấy UUID ngẫu nhiên để tránh trùng tên khi tải hàng loạt
    import uuid
    uid = str(uuid.uuid4())[:8]
    output_template = os.path.join(download_dir, f"{timestamp}_{uid}_%(title).30s.%(ext)s")

    try:
        import shared_state
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")

        # ===== BƯỚC 1: TẢI VIDEO ĐA NỀN TẢNG (DOUYIN / TIKTOK / XHS / FB / YT...) =====
        import importlib, sys
        if 'social_downloader' in sys.modules:
            try:
                importlib.reload(sys.modules['social_downloader'])
            except Exception:
                pass
        from social_downloader import download_social_video
        prefix = f"{timestamp}_{uid}"
        
        await safe_edit_status(
            status_msg,
            f"📥 *Đang tải video sạch không logo từ mạng xã hội...*\n`{url}`",
            parse_mode="Markdown"
        )
        
        success = False
        video_path = ""
        video_title = ""
        err_msg = ""
        if resume_state and os.path.isfile(resume_state.get('video_path', '')):
            success = True
            video_path = resume_state['video_path']
        for attempt in range(3):
            if success:
                break
            success, video_path, video_title, err_msg = await asyncio.to_thread(
                download_social_video, url, download_dir, prefix
            )
            if success and os.path.exists(video_path):
                break
            if attempt < 2:
                wait_sec = 5 * (attempt + 1)
                logger.warning(f"⚠️ Chưa tải được video từ {url} ({err_msg}). Tự động thử lại sau {wait_sec}s (Lần {attempt+1}/3)...")
                await safe_edit_status(
                    status_msg,
                    f"⚠️ *Đang tự động thử tải lại sau {wait_sec}s (Lần {attempt+1}/3)...*\n`{url}`",
                    parse_mode="Markdown"
                )
                await asyncio.sleep(wait_sec)
            else:
                break

        if not success or not os.path.exists(video_path):
            await safe_edit_status(
                status_msg,
                f"❌ *Không thể tải video!*\n\n"
                f"Link: {url}\n"
                f"Lỗi: {err_msg[:400] if err_msg else 'Không rõ nguyên nhân'}"
            )
            return

        downloaded_files = [os.path.basename(video_path)]
        job_tracker.start_video(os.path.basename(video_path), pos, pos + global_queue.qsize())
        base_name = os.path.splitext(downloaded_files[0])[0].rstrip('.')
        start_time = time.time()

        # Chuẩn bị thư mục output
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")
        dubbing_dir = os.path.join(out_dir, "dubbing")
        mixed_audio = os.path.join(out_dir, "mixed.wav")
        final_video = os.path.join(out_dir, f"final_{base_name}.mp4")

        from pipeline_v2.config import PipelineMode, PipelineSettings
        if PipelineSettings.from_env().mode is PipelineMode.V2:
            start_time = time.time()
            downloads_dir = r"D:\banve"
            os.makedirs(downloads_dir, exist_ok=True)
            local_save_path = os.path.join(downloads_dir, f"Dubbed_{base_name}.mp4")
            await run_pipeline_v2_for_telegram(
                video_path,
                out_dir,
                final_video,
                status_msg,
                delivery_copy_path=local_save_path,
            )
            elapsed_time = int(time.time() - start_time)
            mins = elapsed_time // 60
            secs = elapsed_time % 60
            time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
            remaining = global_queue.qsize()
            queue_status = f"\n⏳ Phía sau còn {remaining} video đang chờ xử lý..." if remaining > 0 else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
            caption = (
                f"✅ *Video đã lồng tiếng Tiếng Việt (Pipeline v2 - Âm thanh Studio)!*\n\n"
                f"🎬 Video: `{video_title if 'video_title' in locals() else base_name}`\n"
                f"💾 Đã tự động lưu vào máy: `D:\\banve`\n"
                f"⏱️ Thời gian xử lý: {time_str}"
                f"{queue_status}"
            )
            await safe_edit_status(
                status_msg,
                caption,
                parse_mode="Markdown",
            )
            return

        # ===== THỰC THI PIPELINE TOOL V1 QUA V1_ORCHESTRATOR =====
        try:
            from v1_feature_flags import get_feature_flags
            flags = get_feature_flags(WORKSPACE)
        except Exception:
            flags = {}

        if flags.get("V1_USE_ORCHESTRATOR", True):
            try:
                from v1_orchestrator import V1Orchestrator
                orch = V1Orchestrator(WORKSPACE)
                job_id = (resume_state or {}).get('job_id') or f"tg_{base_name}_{int(time.time())}"
                downloads_dir = r"D:\banve"
                os.makedirs(downloads_dir, exist_ok=True)
                local_save_path = os.path.join(downloads_dir, f"Dubbed_{base_name}.mp4")

                async def _tg_orch_progress(st, step, tot, pct, msg, d):
                    job_tracker.update_step(step, msg, percent=int(pct))
                    await safe_edit_status(
                        status_msg,
                        f"🎬 *Đang xử lý Video ({pct:.0f}%):*\n`{url}`\n\n{msg}",
                        parse_mode="Markdown"
                    )

                start_time = time.time()
                res = await orch.execute_job(
                    video_path=video_path,
                    job_id=job_id,
                    output_dir=out_dir,
                    delivery_path=local_save_path,
                    user_mode=video_mode or flags.get("V1_VIDEO_MODE", "AUTO"),
                    overrides={**(job_overrides or {}), **({"voice_mode": voice_mode} if voice_mode else {})},
                    progress_callback=_tg_orch_progress,
                    stop_checker=lambda: getattr(shared_state, 'stop_requested', False),
                )

                if not res.get('tracker_finalized'):
                    job_tracker.finish_video(os.path.basename(video_path), res['final_video'], time.time() - start_time)
                elapsed_time = int(time.time() - start_time)
                mins = elapsed_time // 60
                secs = elapsed_time % 60
                time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
                remaining = global_queue.qsize()
                queue_status = f"\n⏳ Phía sau còn {remaining} video đang chờ xử lý..." if remaining > 0 else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
                qc_st = res.get("qc_status", "PASS")
                mode_st = res.get("video_mode", "AUTO")
                caption = (
                    f"✅ *Video đã lồng tiếng Tiếng Việt (Tool V1 - {mode_st})!*\n\n"
                    f"🎬 Video: `{video_title if 'video_title' in locals() else base_name}`\n"
                    f"💾 Đã tự động lưu vào máy: `D:\\banve`\n"
                    f"⏱️ Thời gian xử lý: {time_str}\n"
                    f"🛡️ Quality Gate: {qc_st}"
                    f"{queue_status}\n\n"
                    f"📎 Link gốc: {original_url}"
                )
                await safe_edit_status(status_msg, caption)
                return
            except asyncio.CancelledError:
                raise
            except Exception as orch_err:
                if getattr(shared_state, 'stop_requested', False):
                    logger.info("V1 Orchestrator đã dừng do có yêu cầu dừng: %s", orch_err)
                    return
                logger.exception("V1 job failed; retain checkpoint, do not restart legacy pipeline")
                raise

        # ===== BƯỚC 2: TÁCH ÂM THANH (Legacy Fallback) =====
        start_time = time.time()
        # Đóng băng cấu hình job để ổn định suốt chu trình
        from job_config_service import get_frozen_config, freeze_job_config
        from audio_settings import get_audio_settings
        cur_audio_settings = get_audio_settings()
        video_file_name = downloaded_files[0]
        frozen_job_entry = get_frozen_config(video_file_name) or freeze_job_config(
            video_name=video_file_name,
            overrides={
                "bgm_volume_db": cur_audio_settings.get("bgm_volume_db", -2.0),
                "dubbing_volume_db": cur_audio_settings.get("dubbing_volume_db", 1.0),
                "separation_mode": cur_audio_settings.get("separation_mode", "roformer"),
                "ducking_mode": cur_audio_settings.get("ducking_mode", "soft"),
                "script_mode": (job_overrides or {}).get("script_mode") or cur_audio_settings.get("script_mode", "default"),
            }
        )
        frozen_eff = frozen_job_entry.get("effective_config", {}) if frozen_job_entry else {}
        v1_bgm_vol = frozen_eff.get("bgm_volume_db", cur_audio_settings.get("bgm_volume_db", -2.0))
        v1_dub_vol = frozen_eff.get("dubbing_volume_db", cur_audio_settings.get("dubbing_volume_db", 1.0))
        v1_sep_mode = frozen_eff.get("separation_mode", cur_audio_settings.get("separation_mode", "roformer"))
        v1_duck_mode = frozen_eff.get("ducking_mode", cur_audio_settings.get("ducking_mode", "soft"))
        v1_script_mode = frozen_eff.get("script_mode", cur_audio_settings.get("script_mode", "default"))

        await safe_edit_status(
            status_msg,
            f"✅ *Tải thành công!*\n`{url}`\n\n"
            "🎧 *Bước 1/8:* Đang trích xuất âm thanh gốc...",
            parse_mode="Markdown"
        )
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        result = await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)
        if not result or not os.path.exists(original_audio):
            await safe_edit_status(status_msg, f"❌ Không thể trích xuất âm thanh từ video.\n`{url}`", parse_mode="Markdown")
            return

        # ===== BƯỚC 2: TÁCH VOCAL BẰNG BS-ROFORMER GPU / DEMUCS =====
        await safe_edit_status(
            status_msg,
            f"🎧 *Trích xuất xong!*\n`{url}`\n\n"
            "🧠 *Bước 2/8:* Đang bóc tách giọng nói khỏi nhạc nền (BS-RoFormer GPU / Demucs)...",
            parse_mode="Markdown"
        )
        from video_utils import separate_vocals_demucs
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        vocals_audio, no_vocals_audio = await asyncio.to_thread(
            separate_vocals_demucs,
            original_audio,
            out_dir,
            separation_mode=v1_sep_mode,
        )

        # ===== BƯỚC 3: NHẬN DẠNG GIỌNG NÓI =====
        await safe_edit_status(
            status_msg,
            f"🧠 *Tách âm thanh nền xong!*\n`{url}`\n\n"
            "🤖 *Bước 3/8:* Whisper AI đang nhận dạng từ Vocal sạch...",
            parse_mode="Markdown"
        )
        # Sử dụng vocals_audio (giọng sạch) thay vì original_audio
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        from v1_gpu_gatekeeper import async_gpu_gatekeeper
        async with async_gpu_gatekeeper("speech_asr", timeout_seconds=900.0):
            srt_segments = await asyncio.to_thread(extract_subtitles_isolated, vocals_audio, srt_original)

        # ===== BƯỚC 4: KIỂM TRA VỊ TRÍ PHỤ ĐỀ CHÍNH VÀ QUÉT PHỤ ĐỀ CÂM (OCR) =====
        await safe_edit_status(status_msg, "👀 *Bước 4/8:* Đang quét vùng phụ đề cố định (OCR)...", parse_mode="Markdown")
        from ocr_utils import perform_video_ocr, extract_silent_subtitles_from_gaps
        from ass_utils import generate_ass_file, sync_and_clamp_subtitles
        try:
            if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
            async with async_gpu_gatekeeper("visual_ocr", timeout_seconds=900.0):
                _, vid_w, vid_h, main_y_pct = await asyncio.to_thread(perform_video_ocr, video_path, target_lang="vi", sample_rate=1.0, api_key=GEMINI_API_KEY, srt_segments=srt_segments)
            
            floating_segments = []
            
            # XỬ LÝ THỜI GIAN CHUẨN KHI LÀM SUB & LỒNG TIẾNG (Chống lệch giọng)
            import datetime
            for i in range(len(srt_segments) - 1):
                if srt_segments[i].end > srt_segments[i+1].start:
                    new_end = srt_segments[i+1].start - datetime.timedelta(seconds=0.05)
                    if new_end > srt_segments[i].start:
                        srt_segments[i].end = new_end
                    else:
                        srt_segments[i].end = srt_segments[i].start + datetime.timedelta(seconds=0.1)

            for i, seg in enumerate(srt_segments, 1):
                seg.index = i
        except Exception as e:
            logger.error(f"OCR Error: {e}", exc_info=True)
            raise RuntimeError("OCR failed; stopping job to avoid exposing original subtitles") from e
        finally:
            from ocr_utils import release_ocr_reader
            release_ocr_reader()

        # ===== BƯỚC 5: DỊCH PHỤ ĐỀ =====
        configured_m = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
        script_badge = " (Kịch bản: Hài hước)" if str(v1_script_mode).lower() in ("humorous", "hai_huoc", "haihuoc", "comedy") else ""
        await safe_edit_status(
            status_msg,
            f"🤖 *Nhận dạng xong ({len(srt_segments)} đoạn)!*\n`{url}`\n\n"
            f"🌐 *Bước 5/8:* Đang dùng {configured_m} để dịch chuẩn ngữ cảnh{script_badge}...",
            parse_mode="Markdown"
        )
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path, strict=True, script_mode=v1_script_mode)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)
        try:
            t_models = job_tracker.get_status().get("translation_models", [])
            model_info = ", ".join(t_models) if t_models else "Gemini"
            logger.info(f"Hoàn tất Bước 5/8 dịch phụ đề ({len(translated_segments)} đoạn) bằng model: {model_info}")
        except Exception:
            pass

        # ===== BƯỚC 6: LỒNG TIẾNG (Khóa giọng video theo người nói đầu - Codex Plan) =====
        from ai.v1_auto_voice import decide_video_voice
        voice_lock_info = await asyncio.to_thread(
            decide_video_voice,
            out_dir=out_dir,
            srt_segments=srt_segments,
            vocals_path=vocals_audio,
            original_audio_path=original_audio,
            video_path=video_path,
            voice_mode=voice_mode,
            workspace=WORKSPACE,
        )
        v_source = voice_lock_info["voice_source"]
        v_param = voice_lock_info["voice_param"]
        v_label = voice_lock_info["voice_label"]
        v_id = voice_lock_info["voice_id"]

        await safe_edit_status(
            status_msg,
            f"🗣️ *Bước 6/8:* Đang lồng tiếng AI ({v_label})...",
            parse_mode="Markdown"
        )
        
        vid_duration = None
        try:
            from pydub import AudioSegment
            vid_duration = len(AudioSegment.from_file(original_audio)) / 1000.0
        except Exception:
            pass

        seg_voices = voice_lock_info.get("segment_voices") if 'voice_lock_info' in locals() and voice_lock_info else None
        dubbing_audio_files = await generate_dubbing_audio_isolated(
            translated_segments, dubbing_dir, voice_source=v_source, voice_param=v_param, video_duration=vid_duration, segment_voices=seg_voices, script_mode=v1_script_mode
        )
        
        # ĐỒNG BỘ THỜI GIAN VÀ CHỐNG ĐÈ PHỤ ĐỀ THEO GIỌNG ĐỌC THỰC TẾ (Codex Plan - Điểm 4)
        translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)
        
        # TẠO FILE ASS (CÓ SUB DỊCH ĐÃ ĐỒNG BỘ TIMING)
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, floating_segments, ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)
        sub_file_to_use = ass_path
        
        # ===== BƯỚC 7: TRỘN ÂM (ADAPTIVE MIXER) =====
        await safe_edit_status(
            status_msg,
            f"🎛️ *Bước 7/8:* Đang hòa âm và cân bằng âm lượng (Adaptive Mixer)...",
            parse_mode="Markdown"
        )
        # Mix giọng tiếng Việt vào nền nhạc KHÔNG CÓ LỜI (no_vocals_audio) với cấu hình đóng băng
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(
            mix_audio_pydub,
            no_vocals_audio,
            dubbing_audio_files,
            mixed_audio,
            original_volume_db=v1_bgm_vol,
            dubbing_volume_db=v1_dub_vol,
            ducking_mode=v1_duck_mode,
            explicit=True,
        )

        # ===== BƯỚC 8: XUẤT VIDEO =====
        await safe_edit_status(
            status_msg,
            f"👀 *Hòa âm xong!*\n`{url}`\n\n"
            "🎬 *Bước 8/8:* Đang render video (NVENC)...\n"
            "⏳ Đây là bước cuối cùng...",
            parse_mode="Markdown"
        )
        # Lấy lại main_y_pct nếu có, nếu không thì dùng mặc định 88%
        y_pct = locals().get('main_y_pct', 0.88)
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        from v1_gpu_gatekeeper import async_gpu_gatekeeper
        async with async_gpu_gatekeeper("video_rendering", timeout_seconds=900.0):
            res = await asyncio.to_thread(process_video, video_path, sub_file_to_use, mixed_audio, final_video, main_y_pct=y_pct, delogo=False)
        if not res: raise Exception("Tiến trình render video bị lỗi hoặc đã bị hủy bằng lệnh /stop!")

        try:
            snapshot_legacy_telegram_run(
                video_path,
                out_dir,
                {
                    "extract_audio": {"original_audio": Path(original_audio)},
                    "demucs": {
                        "vocals": Path(vocals_audio),
                        "background": Path(no_vocals_audio),
                    },
                    "transcribe": {"srt": Path(srt_original)},
                    "translate": {"srt": Path(srt_translated)},
                    "tts": {"dubbing_directory": Path(dubbing_dir)},
                    "mix": {"mixed_audio": Path(mixed_audio)},
                    "render": {"final_video": Path(final_video)},
                },
                run_started_at_epoch=start_time,
            )
        except Exception as shadow_error:
            logger.warning("Shadow manifest warning: %s", shadow_error)

        caption_lines = [f"🎬 Video đã lồng tiếng Việt\n"]
        
        # Copy sang máy tính người dùng
        try:
            import shutil
            downloads_dir = r"D:\banve"
            os.makedirs(downloads_dir, exist_ok=True)
            local_save_path = os.path.join(downloads_dir, f"Dubbed_{base_name}.mp4")
            shutil.copy2(final_video, local_save_path)
            job_tracker.finish_video(os.path.basename(video_path), local_save_path, time.time() - start_time)
            caption_lines.append(f"💾 Đã tự động lưu vào máy:\n`D:\\banve`\n")
        except Exception as e:
            logger.error(f"Lỗi khi copy vào máy: {e}")

        # ===== GỬI VIDEO =====
        await safe_edit_status(
            status_msg,
            f"🎬 *Render xong!*\n`{url}`\n\n"
            "📤 Đang gửi video cho bạn...",
            parse_mode="Markdown"
        )

        # (Bỏ qua khởi tạo lại caption_lines ở đây vì đã tạo ở trên)
        elapsed_time = int(time.time() - start_time)
        mins = elapsed_time // 60
        secs = elapsed_time % 60
        time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
        caption_lines.append(f"⏱️ Thời gian xử lý: {time_str}\n")
        
        remaining = global_queue.qsize()
        if remaining > 0:
            caption_lines.append(f"⏳ Phía sau còn {remaining} video đang chờ xử lý...\n")
        else:
            caption_lines.append(f"🎉 Đã hoàn tất toàn bộ hàng đợi!\n")
        
        caption_lines.append(f"📎 Link gốc: {original_url}\n")


        caption = "\n".join(caption_lines)
        if len(caption) > 1024:
            caption = caption[:1020] + "..."

        # Tạm thời không gửi video qua Telegram để tiết kiệm mạng (chỉ lưu ổ đĩa)
        # await send_video_safely(context, chat_id, final_video, caption, status_msg, url)
        await safe_edit_status(status_msg, caption)

        # ===== DỌN DẸP RÁC (TRÁNH LỖI FULL Ổ CỨNG) =====
        # try:
        #     import shutil
        #     # Xóa thư mục tạm của video (chứa âm thanh gốc, srt, file trung gian...)
        #     if os.path.exists(out_dir):
        #         shutil.rmtree(out_dir, ignore_errors=True)
        #     # Xóa video gốc đã tải về trong thư mục downloads
        #     if 'video_path' in locals() and os.path.exists(video_path):
        #         os.remove(video_path)
        # except Exception as e:
        #     logger.error(f"Lỗi dọn dẹp rác: {e}")

    except subprocess.TimeoutExpired:
        await safe_edit_status(status_msg, "❌ Tải video quá lâu (>5 phút). Thử link khác nhé!")
    except Exception as e:
        logger.error(f"Error processing {url}: {e}", exc_info=True)
        MAX_AUTO_RETRIES = 10
        import shared_state
        from v1_retry_policy import should_retry_job
        if should_retry_job(e, retry_count, getattr(shared_state, 'stop_requested', False), MAX_AUTO_RETRIES):
            next_retry = retry_count + 1
            new_pos = global_queue.qsize() + 1
            await global_queue.put({
                "type": "url",
                "pos": new_pos,
                "url": original_url,
                "chat_id": target_chat_id,
                "voice_mode": voice_mode or "auto",
                "video_mode": video_mode,
                "job_overrides": job_overrides,
                "resume_state": {"video_path": locals().get('video_path', ''), "job_id": locals().get('job_id')},
                "update": update,
                "context": context,
                "retry_count": next_retry
            })
            logger.info(f"🔄 Video {original_url} gặp sự cố ({e}). Đã tự động đưa vào cuối hàng chờ (vị trí {new_pos}, lần thử {next_retry}/{MAX_AUTO_RETRIES})")
            await safe_edit_status(
                status_msg,
                f"⚠️ *Video gặp sự cố trong quá trình xử lý:*\n`{str(e)[:200]}`\n\n"
                f"🔄 *Đã tự động chuyển xuống cuối hàng chờ (Vị trí {new_pos})* để thử lại lần {next_retry}/{MAX_AUTO_RETRIES} sau khi hoàn tất các video khác!"
            )
        else:
            await safe_edit_status(status_msg, f"❌ Lỗi xử lý:\n{str(e)[:500]}")


# ===== XỬ LÝ MESSAGE CÓ CHỨA LINK =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # Hỗ trợ nhiều URL, loại trùng và bỏ timestamp dính vào link khi copy chat.
    urls = extract_http_urls(text)

    if not urls:
        await update.message.reply_text(
            "❓ Hãy gửi link video (bắt đầu bằng http:// hoặc https://)\n"
            "Ví dụ: https://www.xiaohongshu.com/...\n"
            "💡 Mẹo: Bạn có thể gửi nhiều link cùng lúc để tải hàng loạt!"
        )
        return

    global queue_counter, worker_task
    import shared_state
    shared_state.stop_requested = False

    if global_queue.empty():
        queue_counter = 0
    
    ensure_workers_running()

    from ai.v1_auto_voice import get_auto_voice_mode
    captured_mode = get_auto_voice_mode(WORKSPACE)
    from v1_job_policy import capture_job_settings
    captured_settings = capture_job_settings(WORKSPACE)

    # Đưa từng URL vào hàng đợi
    for url in urls:
        queue_counter += 1
        await global_queue.put({
            'type': 'url',
            'pos': queue_counter,
            'update': update,
            'context': context,
            'url': url,
            'voice_mode': captured_mode,
            'video_mode': captured_settings['feature_flags'].get('V1_VIDEO_MODE', 'AUTO'),
            'job_overrides': captured_settings,
        })
        
    await update.message.reply_text(
        f"✅ Đã thêm {len(urls)} link vào hàng đợi.\n"
        f"👉 Hàng đợi của bạn chạy từ thứ tự {queue_counter - len(urls) + 1} đến {queue_counter}.\n"
        f"⏳ Hiện tại có tổng cộng {global_queue.qsize()} video đang chờ Bot xử lý.",
        parse_mode="Markdown"
    )


# ===== XỬ LÝ VIDEO GỬI TRỰC TIẾP =====
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Người dùng gửi file video trực tiếp qua Telegram (Đẩy vào Queue)."""
    global queue_counter
    import shared_state
    shared_state.stop_requested = False

    if global_queue.empty():
        queue_counter = 0
    
    ensure_workers_running()
        
    queue_counter += 1
    
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
    from v1_job_policy import capture_job_settings
    captured_settings = capture_job_settings(WORKSPACE)

    await global_queue.put({
        'type': 'video',
        'pos': queue_counter,
        'update': update,
        'context': context,
        'chat_id': chat_id,
        'file_id': file_obj.file_id,
        'filename': filename,
        'voice_mode': captured_mode,
        'video_mode': captured_settings['feature_flags'].get('V1_VIDEO_MODE', 'AUTO'),
        'job_overrides': captured_settings,
    })
    
    remaining = global_queue.qsize()
    await update.message.reply_text(
        f"✅ Đã thêm video tải lên vào hàng đợi.\n"
        f"👉 Vị trí của bạn: #{queue_counter}.\n"
        f"⏳ Hiện tại có tổng cộng {remaining} video đang chờ Bot xử lý lần lượt 1-1.",
        parse_mode="Markdown"
    )

async def process_single_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, filename: str, pos: int, chat_id: int = None, voice_mode: str = None, retry_count: int = 0, video_mode: str = None, job_overrides: dict = None, resume_state: dict = None):
    import job_tracker
    job_tracker.start_video(filename, pos, pos + global_queue.qsize())
    status_msg = None
    target_chat_id = chat_id or (update.message.chat_id if update and hasattr(update, 'message') and update.message else None)
    if update and hasattr(update, 'message') and update.message:
        try:
            status_msg = await update.message.reply_text(
                f"▶️ *Đang xử lý Video tải lên (Thứ {pos} trong hàng đợi):*\n`{filename}`\n\n"
                f"⏳ Phía sau còn {global_queue.qsize()} video đang chờ...",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    elif target_chat_id and GLOBAL_BOT_APP and hasattr(GLOBAL_BOT_APP, "bot"):
        try:
            status_msg = await GLOBAL_BOT_APP.bot.send_message(
                chat_id=target_chat_id,
                text=f"▶️ *Đang xử lý Video tải lên (Thứ {pos} từ hàng chờ):*\n`{filename}`\n\n⏳ Phía sau còn {global_queue.qsize()} video đang chờ...",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    try:
        bot_client = context.bot if (context and hasattr(context, 'bot')) else getattr(GLOBAL_BOT_APP, 'bot', None)
        if not bot_client:
            raise RuntimeError("Bot client không khả dụng để tải video file_id")
        download_dir = os.path.join(WORKSPACE, "downloads")
        os.makedirs(download_dir, exist_ok=True)
        
        # Thêm timestamp và uuid để tránh trùng lặp khi chạy hàng loạt
        import uuid
        uid = str(uuid.uuid4())[:8]
        safe_filename = f"{int(time.time())}_{uid}_{filename}"
        video_path = os.path.join(download_dir, safe_filename)

        await safe_edit_status(status_msg, "⏳ Đang tải video từ Telegram...")
        # Tăng timeout lên 600s để tránh lỗi Timed out khi tải file video lớn
        if resume_state and os.path.isfile(resume_state.get('video_path', '')):
            video_path = resume_state['video_path']
            safe_filename = os.path.basename(video_path)
        else:
            file = await bot_client.get_file(file_id)
            await file.download_to_drive(video_path, read_timeout=600, connect_timeout=600, pool_timeout=600, write_timeout=600)
        
        base_name = os.path.splitext(safe_filename)[0]
        start_time = time.time()
        out_dir = os.path.join(WORKSPACE, base_name)
        os.makedirs(out_dir, exist_ok=True)

        original_audio = os.path.join(out_dir, "original.wav")
        srt_original = os.path.join(out_dir, "original.srt")
        srt_translated = os.path.join(out_dir, "translated.srt")
        dubbing_dir = os.path.join(out_dir, "dubbing")
        mixed_audio = os.path.join(out_dir, "mixed.wav")
        final_video = os.path.join(out_dir, f"final_{base_name}.mp4")

        from pipeline_v2.config import PipelineMode, PipelineSettings
        if PipelineSettings.from_env().mode is PipelineMode.V2:
            start_time = time.time()
            downloads_dir = r"D:\banve"
            os.makedirs(downloads_dir, exist_ok=True)
            local_save_path = os.path.join(downloads_dir, f"Dubbed_{base_name}.mp4")
            await run_pipeline_v2_for_telegram(
                video_path,
                out_dir,
                final_video,
                status_msg,
                delivery_copy_path=local_save_path,
            )
            elapsed_time = int(time.time() - start_time)
            mins = elapsed_time // 60
            secs = elapsed_time % 60
            time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
            remaining = global_queue.qsize()
            queue_status = f"\n⏳ Phía sau còn {remaining} video đang chờ xử lý..." if remaining > 0 else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
            caption = (
                f"✅ *Video đã lồng tiếng Tiếng Việt (Pipeline v2 - Âm thanh Studio)!*\n\n"
                f"🎬 Video: `{filename}`\n"
                f"💾 Đã tự động lưu vào máy: `D:\\banve`\n"
                f"⏱️ Thời gian xử lý: {time_str}"
                f"{queue_status}"
            )
            await safe_edit_status(
                status_msg,
                caption,
                parse_mode="Markdown",
            )
            return

        # ===== THỰC THI PIPELINE TOOL V1 QUA V1_ORCHESTRATOR =====
        try:
            from v1_feature_flags import get_feature_flags
            flags = get_feature_flags(WORKSPACE)
        except Exception:
            flags = {}

        if flags.get("V1_USE_ORCHESTRATOR", True):
            try:
                from v1_orchestrator import V1Orchestrator
                orch = V1Orchestrator(WORKSPACE)
                job_id = (resume_state or {}).get('job_id') or f"tg_{safe_filename}_{int(time.time())}"
                downloads_dir = r"D:\banve"
                os.makedirs(downloads_dir, exist_ok=True)
                local_save_path = os.path.join(downloads_dir, f"Dubbed_{safe_filename}")

                async def _tg_orch_progress(st, step, tot, pct, msg, d):
                    job_tracker.update_step(step, msg, percent=int(pct))
                    await safe_edit_status(
                        status_msg,
                        f"🎬 *Đang xử lý Video ({pct:.0f}%):*\n`{filename}`\n\n{msg}",
                        parse_mode="Markdown"
                    )

                start_time = time.time()
                res = await orch.execute_job(
                    video_path=video_path,
                    job_id=job_id,
                    output_dir=out_dir,
                    delivery_path=local_save_path,
                    user_mode=video_mode or flags.get("V1_VIDEO_MODE", "AUTO"),
                    overrides={**(job_overrides or {}), **({"voice_mode": voice_mode} if voice_mode else {})},
                    progress_callback=_tg_orch_progress,
                    stop_checker=lambda: getattr(shared_state, 'stop_requested', False),
                )

                if not res.get('tracker_finalized'):
                    job_tracker.finish_video(os.path.basename(video_path), res['final_video'], time.time() - start_time)
                elapsed_time = int(time.time() - start_time)
                mins = elapsed_time // 60
                secs = elapsed_time % 60
                time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
                remaining = global_queue.qsize()
                queue_status = f"\n⏳ Phía sau còn {remaining} video đang chờ xử lý..." if remaining > 0 else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
                qc_st = res.get("qc_status", "PASS")
                mode_st = res.get("video_mode", "AUTO")
                caption = (
                    f"✅ *Video đã lồng tiếng Tiếng Việt (Tool V1 - {mode_st})!*\n\n"
                    f"🎬 Video: `{filename}`\n"
                    f"💾 Đã tự động lưu vào máy: `D:\\banve`\n"
                    f"⏱️ Thời gian xử lý: {time_str}\n"
                    f"🛡️ Quality Gate: {qc_st}"
                    f"{queue_status}"
                )
                await safe_edit_status(status_msg, caption)
                return
            except asyncio.CancelledError:
                raise
            except Exception as orch_err:
                if getattr(shared_state, 'stop_requested', False):
                    logger.info("V1 Orchestrator đã dừng do có yêu cầu dừng: %s", orch_err)
                    return
                logger.exception("V1 job failed; retain checkpoint, do not restart legacy pipeline")
                raise

        start_time = time.time()
        # Đóng băng cấu hình job để ổn định suốt chu trình
        from job_config_service import get_frozen_config, freeze_job_config
        from audio_settings import get_audio_settings
        cur_audio_settings = get_audio_settings()
        video_file_name = safe_filename
        frozen_job_entry = get_frozen_config(video_file_name) or freeze_job_config(
            video_name=video_file_name,
            overrides={
                "bgm_volume_db": cur_audio_settings.get("bgm_volume_db", -2.0),
                "dubbing_volume_db": cur_audio_settings.get("dubbing_volume_db", 1.0),
                "separation_mode": cur_audio_settings.get("separation_mode", "roformer"),
                "ducking_mode": cur_audio_settings.get("ducking_mode", "soft"),
                "script_mode": (job_overrides or {}).get("script_mode") or cur_audio_settings.get("script_mode", "default"),
            }
        )
        frozen_eff = frozen_job_entry.get("effective_config", {}) if frozen_job_entry else {}
        v1_bgm_vol = frozen_eff.get("bgm_volume_db", cur_audio_settings.get("bgm_volume_db", -2.0))
        v1_dub_vol = frozen_eff.get("dubbing_volume_db", cur_audio_settings.get("dubbing_volume_db", 1.0))
        v1_sep_mode = frozen_eff.get("separation_mode", cur_audio_settings.get("separation_mode", "roformer"))
        v1_duck_mode = frozen_eff.get("ducking_mode", cur_audio_settings.get("ducking_mode", "soft"))
        v1_script_mode = frozen_eff.get("script_mode", cur_audio_settings.get("script_mode", "default"))

        await safe_edit_status(status_msg, "🎧 Bước 1/8: Đang trích xuất âm thanh...")
        import shared_state
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)

        # ===== BƯỚC 2: TÁCH VOCAL BẰNG BS-ROFORMER GPU / DEMUCS =====
        await safe_edit_status(status_msg, "🧠 Bước 2/8: Đang bóc tách giọng nói khỏi nhạc nền (BS-RoFormer GPU / Demucs)...")
        from video_utils import separate_vocals_demucs
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        vocals_audio, no_vocals_audio = await asyncio.to_thread(
            separate_vocals_demucs,
            original_audio,
            out_dir,
            separation_mode=v1_sep_mode,
        )

        # ===== BƯỚC 3: NHẬN DẠNG GIỌNG NÓI =====
        await safe_edit_status(status_msg, "🤖 Bước 3/8: Whisper AI đang nhận dạng từ Vocal sạch...")
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        from v1_gpu_gatekeeper import async_gpu_gatekeeper
        async with async_gpu_gatekeeper("speech_asr", timeout_seconds=900.0):
            srt_segments = await asyncio.to_thread(extract_subtitles_isolated, vocals_audio, srt_original)

        # ===== BƯỚC 4: KIỂM TRA VỊ TRÍ PHỤ ĐỀ CHÍNH VÀ QUÉT PHỤ ĐỀ CÂM (OCR) =====
        await safe_edit_status(status_msg, "👀 Bước 4/8: Đang quét vùng phụ đề cố định (OCR)...", parse_mode="Markdown")
        from ocr_utils import perform_video_ocr, extract_silent_subtitles_from_gaps
        from ass_utils import generate_ass_file, sync_and_clamp_subtitles
        try:
            if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
            async with async_gpu_gatekeeper("visual_ocr", timeout_seconds=900.0):
                _, vid_w, vid_h, main_y_pct = await asyncio.to_thread(perform_video_ocr, video_path, target_lang="vi", sample_rate=1.0, api_key=GEMINI_API_KEY, srt_segments=srt_segments)
            
            floating_segments = []
            
            import datetime
            for i in range(len(srt_segments) - 1):
                if srt_segments[i].end > srt_segments[i+1].start:
                    new_end = srt_segments[i+1].start - datetime.timedelta(seconds=0.05)
                    if new_end > srt_segments[i].start:
                        srt_segments[i].end = new_end
                    else:
                        srt_segments[i].end = srt_segments[i].start + datetime.timedelta(seconds=0.1)

            for i, seg in enumerate(srt_segments, 1):
                seg.index = i
        except Exception as e:
            logger.error(f"OCR Error: {e}", exc_info=True)
            raise RuntimeError("OCR failed; stopping job to avoid exposing original subtitles") from e
        finally:
            from ocr_utils import release_ocr_reader
            release_ocr_reader()

        # ===== BƯỚC 5: DỊCH PHỤ ĐỀ =====
        configured_m = os.getenv("GEMINI_MODEL", "gemini-3.7-flash").strip() or "gemini-3.7-flash"
        script_badge = " (Kịch bản: Hài hước)" if str(v1_script_mode).lower() in ("humorous", "hai_huoc", "haihuoc", "comedy") else ""
        await safe_edit_status(status_msg, f"🌐 Bước 5/8: {configured_m} đang dịch{script_badge} {len(srt_segments)} đoạn phụ đề (Có hỗ trợ AI Vision)...")
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path, strict=True, script_mode=v1_script_mode)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)
        try:
            t_models = job_tracker.get_status().get("translation_models", [])
            model_info = ", ".join(t_models) if t_models else "Gemini"
            logger.info(f"Hoàn tất Bước 5/8 dịch phụ đề ({len(translated_segments)} đoạn) bằng model: {model_info}")
        except Exception:
            pass

        # ===== BƯỚC 6: LỒNG TIẾNG (Khóa giọng video theo người nói đầu - Codex Plan) =====
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        from ai.v1_auto_voice import decide_video_voice
        voice_lock_info = await asyncio.to_thread(
            decide_video_voice,
            out_dir=out_dir,
            srt_segments=srt_segments,
            vocals_path=vocals_audio,
            original_audio_path=original_audio,
            video_path=video_path,
            voice_mode=voice_mode,
            workspace=WORKSPACE,
        )
        v_source = voice_lock_info["voice_source"]
        v_param = voice_lock_info["voice_param"]
        v_label = voice_lock_info["voice_label"]
        v_id = voice_lock_info["voice_id"]

        await safe_edit_status(status_msg, f"🗣️ Bước 6/8: Đang lồng tiếng AI ({v_label})...")
        
        vid_duration = None
        try:
            from pydub import AudioSegment
            vid_duration = len(AudioSegment.from_file(original_audio)) / 1000.0
        except Exception:
            pass

        seg_voices = voice_lock_info.get("segment_voices") if 'voice_lock_info' in locals() and voice_lock_info else None
        dubbing_audio_files = await generate_dubbing_audio_isolated(
            translated_segments, dubbing_dir, voice_source=v_source, voice_param=v_param, video_duration=vid_duration, segment_voices=seg_voices, script_mode=v1_script_mode
        )
        
        # ĐỒNG BỘ THỜI GIAN VÀ CHỐNG ĐÈ PHỤ ĐỀ THEO GIỌNG ĐỌC THỰC TẾ (Codex Plan - Điểm 4)
        translated_segments = sync_and_clamp_subtitles(translated_segments, dubbing_audio_files)

        # TẠO FILE ASS (CÓ SUB DỊCH ĐÃ ĐỒNG BỘ TIMING)
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, floating_segments, ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)
        sub_file_to_use = ass_path

        # ===== BƯỚC 7: TRỘN ÂM (ADAPTIVE MIXER) =====
        await safe_edit_status(status_msg, "🎛️ Bước 7/8: Đang hòa âm và cân bằng âm lượng (Adaptive Mixer)...")
        # Mix giọng tiếng Việt vào nền nhạc KHÔNG CÓ LỜI (no_vocals_audio) với cấu hình đóng băng
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(
            mix_audio_pydub,
            no_vocals_audio,
            dubbing_audio_files,
            mixed_audio,
            original_volume_db=v1_bgm_vol,
            dubbing_volume_db=v1_dub_vol,
            ducking_mode=v1_duck_mode,
            explicit=True,
        )

        # ===== BƯỚC 8: XUẤT VIDEO =====
        await safe_edit_status(status_msg, "🎬 Bước 8/8: Đang render video (NVENC)...")
        y_pct = locals().get('main_y_pct', 0.88)
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        from v1_gpu_gatekeeper import async_gpu_gatekeeper
        async with async_gpu_gatekeeper("video_rendering", timeout_seconds=900.0):
            res = await asyncio.to_thread(process_video, video_path, sub_file_to_use, mixed_audio, final_video, main_y_pct=y_pct, delogo=False)
        if not res: raise Exception("Tiến trình render video bị lỗi hoặc đã bị hủy bằng lệnh /stop!")

        try:
            snapshot_legacy_telegram_run(
                video_path,
                out_dir,
                {
                    "extract_audio": {"original_audio": Path(original_audio)},
                    "demucs": {
                        "vocals": Path(vocals_audio),
                        "background": Path(no_vocals_audio),
                    },
                    "transcribe": {"srt": Path(srt_original)},
                    "translate": {"srt": Path(srt_translated)},
                    "tts": {"dubbing_directory": Path(dubbing_dir)},
                    "mix": {"mixed_audio": Path(mixed_audio)},
                    "render": {"final_video": Path(final_video)},
                },
                run_started_at_epoch=start_time,
            )
        except Exception as shadow_error:
            logger.warning("Shadow manifest warning: %s", shadow_error)

        caption_lines = [f"🎬 Video đã lồng tiếng Việt\n"]
        try:
            import shutil
            downloads_dir = r"D:\banve"
            os.makedirs(downloads_dir, exist_ok=True)
            local_save_path = os.path.join(downloads_dir, f"Dubbed_{base_name}.mp4")
            shutil.copy2(final_video, local_save_path)
            job_tracker.finish_video(os.path.basename(video_path), local_save_path, time.time() - start_time)
            caption_lines.append(f"💾 Đã tự động lưu vào máy:\n`D:\\banve`\n")
        except Exception as e:
            logger.error(f"Lỗi khi copy vào máy: {e}")

        await safe_edit_status(status_msg, "📤 Đang gửi video...")
        
        elapsed_time = int(time.time() - start_time)
        mins = elapsed_time // 60
        secs = elapsed_time % 60
        time_str = f"{mins} phút {secs} giây" if mins > 0 else f"{secs} giây"
        remaining = global_queue.qsize()
        queue_status = f"\n⏳ Phía sau còn {remaining} video đang chờ xử lý..." if remaining > 0 else "\n🎉 Đã hoàn tất toàn bộ hàng đợi!"
        caption = f"✅ Video đã lồng tiếng Tiếng Việt!\n⏱️ Thời gian xử lý: {time_str}{queue_status}"


        await safe_edit_status(status_msg, caption)

        # ===== DỌN DẸP RÁC (TRÁNH LỖI FULL Ổ CỨNG) =====
        # try:
        #     import shutil
        #     if os.path.exists(out_dir):
        #         shutil.rmtree(out_dir, ignore_errors=True)
        #     if 'video_path' in locals() and os.path.exists(video_path):
        #         os.remove(video_path)
        # except Exception as e:
        #     logger.error(f"Lỗi dọn dẹp rác: {e}")

    except Exception as e:
        logger.error(f"Error processing video {filename}: {e}", exc_info=True)
        MAX_AUTO_RETRIES = 10
        import shared_state
        from v1_retry_policy import should_retry_job
        if should_retry_job(e, retry_count, getattr(shared_state, 'stop_requested', False), MAX_AUTO_RETRIES):
            next_retry = retry_count + 1
            new_pos = global_queue.qsize() + 1
            await global_queue.put({
                "type": "video",
                "pos": new_pos,
                "file_id": file_id,
                "filename": filename,
                "chat_id": target_chat_id,
                "voice_mode": voice_mode or "auto",
                "video_mode": video_mode,
                "job_overrides": job_overrides,
                "resume_state": {"video_path": locals().get('video_path', ''), "job_id": locals().get('job_id')},
                "update": update,
                "context": context,
                "retry_count": next_retry
            })
            logger.info(f"🔄 Video {filename} gặp sự cố ({e}). Đã tự động đưa vào cuối hàng chờ (vị trí {new_pos}, lần thử {next_retry}/{MAX_AUTO_RETRIES})")
            await safe_edit_status(
                status_msg,
                f"⚠️ *Video gặp sự cố trong quá trình xử lý:*\n`{str(e)[:200]}`\n\n"
                f"🔄 *Đã tự động chuyển xuống cuối hàng chờ (Vị trí {new_pos})* để thử lại lần {next_retry}/{MAX_AUTO_RETRIES} sau khi hoàn tất các video khác!"
            )
        else:
            await safe_edit_status(status_msg, f"❌ Lỗi: {str(e)[:500]}")


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
    if resumable_jobs:
        ensure_workers_running(application)



async def enqueue_pending_queue_jobs(application=None):
    """Tự động khôi phục các link còn tồn đọng trong telegram_queue.json sau khi khởi động hoặc rớt mạng."""
    global queue_counter, worker_task
    bot_system = Path(WORKSPACE) / "bot_system"
    bot_system_queue = bot_system / "telegram_queue.json"
    queue_file = bot_system_queue if (bot_system_queue.is_file() or bot_system.is_dir()) else (Path(WORKSPACE) / "telegram_queue.json")
    if not queue_file.is_file():
        return 0
    try:
        import json
        data = json.loads(queue_file.read_text(encoding="utf-8-sig"))
        items = data.get("items", [])
        if not items:
            return 0

        existing_names = set()
        for j in list(global_queue._queue):
            if isinstance(j, dict):
                existing_names.add(j.get("url") or j.get("filename") or j.get("name"))
            else:
                existing_names.add(str(j))

        restored = 0
        for item in items:
            target = item.get("url") or item.get("name")
            if not target or target in existing_names:
                continue
            queue_counter += 1
            if str(target).startswith("http"):
                await global_queue.put({
                    "type": "url",
                    "pos": queue_counter,
                    "url": target,
                    "chat_id": item.get("chat_id"),
                    "voice_mode": item.get("voice_mode", "auto"),
                    "video_mode": item.get("video_mode"),
                    "job_overrides": item.get("job_overrides"),
                    "resume_state": item.get("resume_state"),
                    "retry_count": item.get("retry_count", 0),
                    "update": None,
                    "context": None
                })
                existing_names.add(target)
                restored += 1
            elif item.get("type") == "video" and item.get("file_id"):
                await global_queue.put({
                    "type": "video",
                    "pos": queue_counter,
                    "file_id": item.get("file_id"),
                    "filename": item.get("filename") or target,
                    "chat_id": item.get("chat_id"),
                    "voice_mode": item.get("voice_mode", "auto"),
                    "video_mode": item.get("video_mode"),
                    "job_overrides": item.get("job_overrides"),
                    "resume_state": item.get("resume_state"),
                    "retry_count": item.get("retry_count", 0),
                    "update": None,
                    "context": None
                })
                existing_names.add(target)
                restored += 1

        if restored > 0:
            logger.info(f"🔄 Đã tự động nạp {restored} video từ hàng chờ cũ vào xử lý.")
            ensure_workers_running(application)
        return restored

    except Exception as e:
        logger.warning(f"Lỗi khi nạp hàng chờ từ telegram_queue.json: {e}")
        return 0

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
            if clear_flag.exists():
                try: clear_flag.unlink(missing_ok=True)
                except Exception: pass
                global_queue.clear()
                logger.info("🗑️ Đã nhận lệnh xóa sạch hàng đợi từ Dashboard.")

            if reorder_flag.exists():
                try:
                    reorder_flag.unlink(missing_ok=True)
                except Exception:
                    pass
                try:
                    queue_file = flag_dir / "telegram_queue.json"
                    if queue_file.is_file():
                        import json
                        qdata = json.loads(queue_file.read_text(encoding="utf-8-sig"))
                        desired_items = qdata.get("items", [])
                        if desired_items and not global_queue.empty():
                            current_jobs = list(global_queue._queue)

                            def _job_key(j):
                                if isinstance(j, dict):
                                    return str(j.get("url") or j.get("filename") or j.get("file_id") or j.get("name") or "").strip()
                                return str(j).strip()

                            job_map = {}
                            for j in current_jobs:
                                k = _job_key(j)
                                if k:
                                    if k not in job_map:
                                        job_map[k] = []
                                    job_map[k].append(j)

                            new_jobs = []
                            for it in desired_items:
                                k = str(it.get("url") or it.get("filename") or it.get("file_id") or it.get("name") or "").strip()
                                if k in job_map and job_map[k]:
                                    new_jobs.append(job_map[k].pop(0))

                            deleted_keys = set(qdata.get("deleted_keys", []))
                            for remaining in job_map.values():
                                for j in remaining:
                                    k = _job_key(j)
                                    if k not in deleted_keys:
                                        new_jobs.append(j)

                            global_queue._queue.clear()
                            for pos, j in enumerate(new_jobs, 1):
                                if isinstance(j, dict):
                                    j["pos"] = pos
                                global_queue._queue.append(j)
                            global_queue.publish()
                            logger.info("🔀 Đã cập nhật lại thứ tự ưu tiên hàng đợi từ Dashboard (%s video).", len(new_jobs))
                except Exception as e:
                    logger.warning("Lỗi cập nhật thứ tự hàng đợi: %s", e)

            if trigger_flag.exists():
                try: trigger_flag.unlink(missing_ok=True)
                except Exception: pass
                restored = await enqueue_pending_queue_jobs(application)
                ensure_workers_running(application)

        except Exception as e:
            logger.debug(f"Lỗi queue_signal_watcher: {e}")
        await asyncio.sleep(1.5)


def main():
    # Dam bao chi co duy nhat 1 tien trinh Telegram Bot chay tai 1 thoi diem

    import msvcrt
    bot_system = Path(WORKSPACE) / "bot_system"
    lock_dir = bot_system if bot_system.is_dir() else Path(WORKSPACE)
    lock_file_path = os.path.join(str(lock_dir), "bot_instance.lock")
    try:
        global _singleton_lock_file
        _singleton_lock_file = open(lock_file_path, "w")
        msvcrt.locking(_singleton_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except (IOError, OSError):
        print("⚠️ Một tiến trình Telegram Bot khác đang chạy! Đang tự động thoát tiến trình này để tránh render 2 lần...")
        logger.warning("Bot instance already running. Exiting duplicate process.")
        sys.exit(0)

    if not BOT_TOKEN:
        print("=" * 60)
        print("❌ LỖI: Chưa cấu hình Bot Token!")
        print("Mở file telegram_bot.py và dán Token vào dòng BOT_TOKEN")
        print("Lấy Token từ @BotFather trên Telegram")
        print("=" * 60)
        return
    import socket
    import urllib.request

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
            async def _on_bot_startup(app_instance):
                try:
                    global_queue.publish()
                except Exception:
                    pass
                try:
                    if hasattr(app_instance, "create_task"):
                        app_instance.create_task(queue_signal_watcher(app_instance))
                    else:
                        asyncio.create_task(queue_signal_watcher(app_instance))
                except Exception as ex:
                    logger.warning(f"Lỗi khởi động queue_signal_watcher: {ex}")
                try:
                    await enqueue_pending_queue_jobs(app_instance)
                except Exception as ex:
                    logger.warning(f"Lỗi khôi phục hàng đợi ban đầu: {ex}")
                # AUTO_SWEEPER removed: users move completed videos out of D:\banve intentionally,
                # so scanning workspace and copying old final_*.mp4 back to D:\banve caused deleted/moved videos to resurrect.

            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .request(request)
                .post_init(_on_bot_startup)
                .build()
            )
            global GLOBAL_BOT_APP
            GLOBAL_BOT_APP = app

            # Đăng ký handlers
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("stop", cmd_stop))
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("batch", cmd_batch))
            app.add_handler(CommandHandler("local", cmd_batch))
            app.add_handler(CommandHandler("llm", cmd_llm))
            app.add_handler(CommandHandler("voice_auto", cmd_voice_auto))
            app.add_handler(CommandHandler("voice", cmd_voice_auto))
            app.add_handler(CommandHandler("script", cmd_script))
            app.add_handler(CommandHandler("kichban", cmd_script))
            app.add_handler(CommandHandler("vram", cmd_vram))
            app.add_handler(CommandHandler("gpu", cmd_vram))
            app.add_handler(CommandHandler("workers", cmd_workers))
            app.add_handler(CommandHandler("luong", cmd_workers))
            app.add_handler(CallbackQueryHandler(callback_script_selection, pattern=r"^set_script:"))
            app.add_handler(CallbackQueryHandler(callback_worker_selection, pattern=r"^set_worker_"))

            app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_error_handler(telegram_error_handler)

            async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
                err = context.error
                import telegram.error
                if isinstance(err, (telegram.error.NetworkError, telegram.error.TimedOut)):
                    logger.warning("Telegram network transient warning (tự động thử lại): %s", err)
                    return
                logger.error("Exception while handling an update: %s", err, exc_info=err)

            app.add_error_handler(global_error_handler)

            print("Bot da san sang! Dang lang nghe tin nhan...")
            from tool_control_runtime import install as install_tool_control
            install_tool_control(app, globals(), 'v1')
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)
        except Exception as e:
            logger.error(f"Lỗi polling hoặc mạng gián đoạn: {e}. Đang tự động kết nối lại sau 5 giây...")
            print(f"⚠️ Mang chập chờn hoặc loi: {e}. Dang tu dong ket noi lai sau 5 giay...")
            time.sleep(5)


if __name__ == "__main__":
    main()
