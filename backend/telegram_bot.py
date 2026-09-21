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
from telegram import BotCommand, Update
from telegram.error import NetworkError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

try:
    from .environment import load_environment
    from .social_downloader import (
        SensitiveUrlFilter,
        sanitize_exception,
        sanitize_text,
        sanitize_url,
    )
except ImportError:
    from environment import load_environment
    from social_downloader import (
        SensitiveUrlFilter,
        sanitize_exception,
        sanitize_text,
        sanitize_url,
    )

load_environment(Path(__file__).resolve().parent)

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

from ai.transcription import extract_subtitles_whisper, save_srt
from ai.translation import translate_subtitles
from ai.voice_cloning import generate_dubbing_audio
from telegram_jobs import TelegramJobPaths, build_v2_completion_caption
from url_utils import extract_http_urls
from social_downloader import sanitize_url
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video

WORKSPACE = os.path.abspath(
    os.getenv(
        "AUTODUB_WORKSPACE",
        os.path.join(os.path.dirname(__file__), "..", "workspace"),
    )
)
INPUT_DIR = os.path.abspath(os.getenv("AUTODUB_INPUT_DIR", r"D:\video phôi"))
OUTPUT_DIR = os.path.abspath(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\video tool v2"))
os.makedirs(WORKSPACE, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveUrlFilter())
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
    khi mạng Internet bị giật hoặc đứt kết nối tạm thời (httpx.ConnectError).
    """
    if not status_msg:
        return
    for attempt in range(retries):
        try:
            await status_msg.edit_text(text, parse_mode=parse_mode)
            return
        except Exception as e:
            logger.warning(f"Lỗi cập nhật status Telegram (Lần {attempt+1}/{retries}): {e}")
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

    from voice_selection import resolve_voice, get_speaker_voice_map, get_speaker_map
    from dataclasses import replace
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
        rvc_model_path=rvc_model,
        speaker_map=get_speaker_map() if settings.enable_auto_gender else None,
        speaker_voice_map=get_speaker_voice_map() if settings.enable_auto_gender else None,
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
    caption = build_v2_completion_caption(
        title=title,
        output_directory=OUTPUT_DIR,
        elapsed_seconds=elapsed_seconds,
        remaining_jobs=global_queue.qsize(),
    )
    final_video = paths.final_video
    if not Path(final_video).is_file() and paths.delivery_copy and Path(paths.delivery_copy).is_file():
        final_video = paths.delivery_copy

    if context and chat_id and Path(final_video).is_file():
        await send_video_safely(
            context,
            chat_id,
            str(final_video),
            caption,
            status_msg,
            url_or_filename or title,
        )
    else:
        await safe_edit_status(status_msg, caption, parse_mode="Markdown")


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
        "2️⃣ Nhận dạng giọng nói (Qwen3-ASR + Forced Aligner, fallback Whisper)\n"
        "3️⃣ Dịch phụ đề sang Tiếng Việt (Gemini 3.7 Flash)\n"
        "4️⃣ Tách giọng & giữ nhạc nền (BS-RoFormer, fallback Demucs)\n"
        "5️⃣ Lồng tiếng Tiếng Việt (Microsoft Neural TTS)\n"
        "6️⃣ Xuất video chất lượng cao lưu vào `D:\\video tool v2`\n\n"
        "📌 *Lệnh hỗ trợ:*\n"
        "• `/llm` - Cấu hình mô hình AI dịch thuật (Google Gemini / OpenAI GPT-4o / DeepSeek V4)\n"
        "• `/batch` - Tự động quét & edit hàng loạt video trong thư mục `D:\\video_input` trên máy\n"
        "• `/batch D:\\thu_muc` - Chỉ định thư mục chứa video cần edit\n"
        "• `/status` - Kiểm tra trạng thái hàng đợi\n"
        "• `/stop` - Dừng khẩn cấp toàn bộ tác vụ"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


# ===== LỆNH /status =====
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Bot đang hoạt động!\n"
        f"📂 Workspace: {WORKSPACE}\n"
        "🎯 Gửi link video để bắt đầu."
    )


# ===== LỆNH /batch =====
async def cmd_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Xử lý hàng loạt video từ thư mục cục bộ (mặc định: D:\\video_input)
    Cú pháp: /batch hoặc /batch D:\\duong_dan_thu_muc
    """
    input_dir = INPUT_DIR
    if context.args and len(context.args) > 0:
        input_dir = " ".join(context.args).strip()
        
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
            f"🔹 **Google Gemini (Vision 3.5/3.7):** {gemini_status}\n"
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
    global_queue.cancel()
    # Interrupt only this V2 process's children before draining threaded work.
    import psutil
    for child in psutil.Process(os.getpid()).children(recursive=True):
        try:
            child.kill()
        except psutil.Error:
            pass
    
    # 1. Hủy ngay lập tức worker task nếu đang chạy
    global worker_task
    if worker_task and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    
    # 2. Xóa sạch hàng đợi
    global_queue.cancel()
            
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

from durable_adapter import DurableQueue
global_queue = DurableQueue(Path(WORKSPACE) / "queue_v2.sqlite3")
queue_counter = 0
worker_task = None

def ensure_worker(application):
    global worker_task
    if worker_task is None or worker_task.done():
        # The perpetual consumer must not be awaited by Application.stop().
        worker_task = asyncio.create_task(video_worker())

async def shutdown_v2(application):
    global worker_task
    shared_state.stop_requested = True
    if worker_task is not None and not worker_task.done():
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    worker_task = None

async def initialize_v2(application):
    await configure_bot_profile(application)
    global_queue.initialize(application)
    ensure_worker(application)

async def run_durable_video(job):
    """Stable paths make pipeline manifests usable after a process restart."""
    context, update = job['context'], job['update']
    job_key = job.get('job_key') or ('queue_' + str(global_queue.active[0]))
    status = None
    try:
        status = await update.message.reply_text("V2 đang xử lý video trong hàng đợi đã lưu.")
    except Exception:
        logger.warning('Telegram status unavailable; continue the persisted job')
    video = job.get('video_path')
    if not video or not Path(video).is_file():
        downloads = Path(WORKSPACE) / 'downloads'
        downloads.mkdir(parents=True, exist_ok=True)
        prefix = job_key
        if job['type'] == 'url':
            from social_downloader import download_social_video
            ok, video, title, error = await asyncio.to_thread(download_social_video,
                job['url'], str(downloads), prefix)
            if not ok or not video or not Path(video).is_file():
                raise RuntimeError(error or 'Download failed')
        elif job['type'] == 'video':
            remote = await context.bot.get_file(job['file_id'])
            video = str(downloads / (prefix + '.mp4'))
            await remote.download_to_drive(video + '.part', read_timeout=600, write_timeout=600)
            os.replace(video + '.part', video)
        else:
            video = job['path']
            if not Path(video).is_file():
                raise FileNotFoundError(video)
        global_queue.checkpoint(video_path=str(video))
    paths = TelegramJobPaths.create(WORKSPACE, OUTPUT_DIR, job_key)
    paths.prepare_directories()
    resolved_chat_id = job.get('chat_id')
    if not resolved_chat_id and update and getattr(update, 'effective_chat', None):
        resolved_chat_id = update.effective_chat.id
    if not resolved_chat_id and update and getattr(update, 'message', None) and getattr(update.message, 'chat_id', None):
        resolved_chat_id = update.message.chat_id
    await process_v2_telegram_job(
        video,
        paths,
        job.get('filename') or Path(video).name,
        status,
        context=context,
        chat_id=resolved_chat_id,
        url_or_filename=job.get('url') or job.get('filename'),
    )

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

    # Nếu file quá lớn (do chất lượng 720p ép buộc), tiến hành cắt nhỏ video bằng FFmpeg (copy codec không làm giảm chất lượng)
    await safe_edit_status(
        status_msg,
        f"✂️ *Video gốc quá lớn ({file_size // (1024*1024)}MB)!*\nBot đang giữ nguyên chất lượng cao (>720p) và tự động cắt thành các phần <50MB để gửi cho bạn...",
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
            processing = asyncio.create_task(run_durable_video(job))
            try:
                await asyncio.shield(processing)
            except asyncio.CancelledError:
                # asyncio.to_thread cannot be killed by cancelling its waiter.
                # Drain the current pipeline before another worker can start.
                shared_state.stop_requested = True
                try:
                    await processing
                except Exception:
                    logger.info('Interrupted pipeline stopped')
                # Leave interrupted work recoverable, unless /stop cancelled it.
                global_queue.active = None
                logger.info("Worker task cancelled by /stop.")
                break
            except Exception as e:
                global_queue.fail(e)
                logger.error(f"Worker error: {e}")
                try:
                    await job['update'].message.reply_text("V2 xử lý lỗi. Công việc đã được lưu với trạng thái lỗi; hãy gửi lại nếu muốn thử lại.")
                except Exception:
                    logger.warning("Could not report job failure")
            finally:
                import gc, torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                global_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Worker queue cancelled.")
            break

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
    
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(video_worker())

    chat_id = update.effective_chat.id if update and getattr(update, 'effective_chat', None) else None

    # Đưa từng URL vào hàng đợi
    for url in urls:
        queue_counter += 1
        await global_queue.put({
            'type': 'url',
            'pos': queue_counter,
            'update': update,
            'context': context,
            'chat_id': chat_id,
            'url': url
        })
        
    await update.message.reply_text(
        f"✅ Đã thêm {len(urls)} link vào hàng đợi.\n"
        f"👉 Hàng đợi của bạn chạy từ thứ tự {queue_counter - len(urls) + 1} đến {queue_counter}.\n"
        f"⏳ Hiện tại có tổng cộng {global_queue.qsize()} video đang chờ Bot xử lý lần lượt 1-1.",
        parse_mode="Markdown"
    )


# ===== XỬ LÝ VIDEO GỬI TRỰC TIẾP =====
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Người dùng gửi file video trực tiếp qua Telegram (Đẩy vào Queue)."""
    global queue_counter, worker_task
    import shared_state
    shared_state.stop_requested = False

    if global_queue.empty():
        queue_counter = 0
    
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(video_worker())
        
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
        
    chat_id = update.effective_chat.id if update and getattr(update, 'effective_chat', None) else None
    await global_queue.put({
        'type': 'video',
        'pos': queue_counter,
        'update': update,
        'context': context,
        'chat_id': chat_id,
        'file_id': file_obj.file_id,
        'filename': filename
    })
    
    remaining = global_queue.qsize()
    await update.message.reply_text(
        f"✅ Đã thêm video tải lên vào hàng đợi.\n"
        f"👉 Vị trí của bạn: #{queue_counter}.\n"
        f"⏳ Hiện tại có tổng cộng {remaining} video đang chờ Bot xử lý lần lượt 1-1.",
        parse_mode="Markdown"
    )

# ===== KHỞI CHẠY BOT =====


def main():
    # Dam bao chi co duy nhat 1 tien trinh Telegram Bot chay tai 1 thoi diem
    import msvcrt
    lock_file_path = os.path.join(WORKSPACE, "bot_instance.lock")
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
            # Tăng timeout lên 120 giây để không bị Timed out khi gửi/tải video lớn
            request = HTTPXRequest(
                connect_timeout=30,
                read_timeout=120,
                write_timeout=120,
                pool_timeout=120,
            )
            get_updates_request = HTTPXRequest(
                connection_pool_size=8,
                connect_timeout=30,
                read_timeout=90,
                write_timeout=30,
                pool_timeout=30,
            )
            app = (
                Application.builder()
                .token(BOT_TOKEN)
                .request(request)
                .get_updates_request(get_updates_request)
                .post_init(initialize_v2)
                .post_stop(shutdown_v2)
                .build()
            )

            # Đăng ký handlers
            app.add_handler(CommandHandler("start", cmd_start))
            app.add_handler(CommandHandler("stop", cmd_stop))
            app.add_handler(CommandHandler("status", cmd_status))
            app.add_handler(CommandHandler("batch", cmd_batch))
            app.add_handler(CommandHandler("local", cmd_batch))
            app.add_handler(CommandHandler("llm", cmd_llm))
            app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
            app.add_error_handler(telegram_error_handler)

            print("Bot da san sang! Dang lang nghe tin nhan...")
            from tool_control_runtime import install as install_tool_control
            install_tool_control(app, globals(), 'v2')
            app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
                poll_interval=1.0,
                timeout=30,
                bootstrap_retries=-1,
            )
        except Exception as e:
            logger.error(f"Lỗi polling hoặc mạng gián đoạn: {e}. Đang tự động kết nối lại sau 5 giây...")
            print(f"⚠️ Mang chập chờn hoặc loi: {e}. Dang tu dong ket noi lai sau 5 giay...")
            time.sleep(5)


if __name__ == "__main__":
    main()
