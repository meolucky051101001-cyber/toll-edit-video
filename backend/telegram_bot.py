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
except ImportError:
    from environment import load_environment

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
from video_utils import extract_audio_from_video, mix_audio_pydub, process_video

WORKSPACE = os.path.abspath(
    os.getenv(
        "AUTODUB_WORKSPACE",
        os.path.join(os.path.dirname(__file__), "..", "workspace"),
    )
)
INPUT_DIR = os.path.abspath(os.getenv("AUTODUB_INPUT_DIR", r"D:\video phôi"))
OUTPUT_DIR = os.path.abspath(os.getenv("AUTODUB_OUTPUT_DIR", r"D:\banve"))
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

    request = VideoPipelineRequest(
        video_path=Path(video_path),
        job_directory=Path(out_dir),
        output_path=Path(final_video),
        delivery_copy_path=(
            Path(delivery_copy_path) if delivery_copy_path else None
        ),
        settings=settings,
        api_key=GEMINI_API_KEY,
        voice_source="rvc" if rvc_model else "edge",
        voice_param=str(rvc_model) if rvc_model else "vi-VN-HoaiMyNeural",
        rvc_model_path=rvc_model,
        progress=progress,
    )
    return await VideoPipelineRunner(request).run()


async def process_v2_telegram_job(video_path, paths, title, status_msg):
    """Run and report the shared production V2 path for every Telegram input."""

    started_at = time.time()
    await run_pipeline_v2_for_telegram(
        video_path,
        str(paths.job_directory),
        str(paths.final_video),
        status_msg,
        delivery_copy_path=str(paths.delivery_copy),
    )
    caption = build_v2_completion_caption(
        title=title,
        output_directory=OUTPUT_DIR,
        elapsed_seconds=time.time() - started_at,
        remaining_jobs=global_queue.qsize(),
    )
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
        "6️⃣ Xuất video chất lượng cao lưu vào `D:\\banve`\n\n"
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
    for filename in sorted(video_files):
        accepted = await global_queue.put({'type':'local', 'path':str(Path(input_dir, filename).resolve()),
            'update':update, 'context':context, 'pos':0})
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
    await process_v2_telegram_job(video, paths, job.get('filename') or Path(video).name, status)

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

async def process_single_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, pos: int = 1):
    original_url = url

    # Thông báo bắt đầu
    remaining = global_queue.qsize()
    status_msg = await update.message.reply_text(
        f"▶️ *Đang xử lý Video thứ {pos} trong hàng đợi:*\n`{url}`\n\n"
        f"⏳ Phía sau còn {remaining} video đang chờ...",
        parse_mode="Markdown"
    )

    download_dir = os.path.join(WORKSPACE, "downloads")
    os.makedirs(download_dir, exist_ok=True)
    timestamp = str(int(time.time()))
    
    # Lấy UUID ngẫu nhiên để tránh trùng tên khi tải hàng loạt
    import uuid
    uid = str(uuid.uuid4())[:8]
    try:
        import shared_state
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")

        # ===== BƯỚC 1: TẢI VIDEO ĐA NỀN TẢNG (DOUYIN / TIKTOK / XHS / FB / YT...) =====
        from social_downloader import download_social_video
        prefix = f"{timestamp}_{uid}"
        
        await safe_edit_status(
            status_msg,
            f"📥 *Đang tải video sạch không logo từ mạng xã hội...*\n`{url}`",
            parse_mode="Markdown"
        )
        
        success, video_path, video_title, err_msg = await asyncio.to_thread(
            download_social_video, url, download_dir, prefix
        )

        if not success or not os.path.exists(video_path):
            await safe_edit_status(
                status_msg,
                f"❌ *Không thể tải video!*\n\n"
                f"Link: {url}\n"
                f"Lỗi: {err_msg[:400] if err_msg else 'Không rõ nguyên nhân'}"
            )
            return

        base_name = os.path.splitext(os.path.basename(video_path))[0].rstrip('.')

        paths = TelegramJobPaths.create(WORKSPACE, OUTPUT_DIR, base_name)
        paths.prepare_directories()
        out_dir = str(paths.job_directory)
        original_audio = str(paths.original_audio)
        srt_original = str(paths.original_srt)
        srt_translated = str(paths.translated_srt)
        dubbing_dir = str(paths.dubbing_directory)
        mixed_audio = str(paths.mixed_audio)
        final_video = str(paths.final_video)

        from pipeline_v2.config import PipelineMode, PipelineSettings
        if PipelineSettings.from_env().mode is PipelineMode.V2:
            await process_v2_telegram_job(
                video_path,
                paths,
                video_title if "video_title" in locals() else base_name,
                status_msg,
            )
            return

        # ===== BƯỚC 2: TÁCH ÂM THANH =====
        start_time = time.time()
        await safe_edit_status(
            status_msg,
            f"✅ *Tải thành công!*\n`{url}`\n\n"
            "🎧 *Bước 2/6:* Đang trích xuất âm thanh gốc...",
            parse_mode="Markdown"
        )
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        result = await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)
        if not result or not os.path.exists(original_audio):
            await safe_edit_status(status_msg, f"❌ Không thể trích xuất âm thanh từ video.\n`{url}`", parse_mode="Markdown")
            return

        # ===== BƯỚC 2.5: TÁCH VOCAL BẰNG DEMUCS =====
        await safe_edit_status(
            status_msg,
            f"🎧 *Trích xuất xong!*\n`{url}`\n\n"
            "🧠 *Bước 2.5/6:* BS-RoFormer đang tách giọng nhân vật khỏi nhạc nền...",
            parse_mode="Markdown"
        )
        from video_utils import separate_vocals_demucs
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

        # ===== BƯỚC 3: NHẬN DẠNG GIỌNG NÓI =====
        await safe_edit_status(
            status_msg,
            f"🧠 *Tách âm thanh nền xong!*\n`{url}`\n\n"
            "🤖 *Bước 3/6:* Qwen3-ASR đang nhận dạng và căn timestamp từ vocal sạch...",
            parse_mode="Markdown"
        )
        # Sử dụng vocals_audio (giọng sạch) thay vì original_audio
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        srt_segments = await asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original)

        # ===== BƯỚC 3.5: KIỂM TRA VỊ TRÍ PHỤ ĐỀ CHÍNH VÀ QUÉT PHỤ ĐỀ CÂM =====
        await safe_edit_status(status_msg, "👀 *Bước 3.5/6:* Đang quét vùng phụ đề cố định (OCR)...", parse_mode="Markdown")
        from ocr_utils import perform_video_ocr, extract_silent_subtitles_from_gaps
        from ass_utils import generate_ass_file
        try:
            if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
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
            vid_w, vid_h, main_y_pct, floating_segments = 1920, 1080, 0.88, []
        finally:
            from ocr_utils import release_ocr_reader
            release_ocr_reader()

        # ===== BƯỚC 4: DỊCH PHỤ ĐỀ =====
        await safe_edit_status(
            status_msg,
            f"🤖 *Nhận dạng xong ({len(srt_segments)} đoạn)!*\n`{url}`\n\n"
            "🌐 *Bước 4/6:* Đang dùng Gemini AI để dịch chuẩn ngữ cảnh...",
            parse_mode="Markdown"
        )
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # (Di chuyển BƯỚC 4.5 xuống sau BƯỚC 5 để đồng bộ thời gian biến mất của phụ đề với audio)

        # ===== BƯỚC 5: LỒNG TIẾNG =====
        # Khôi phục giọng RVC (Đáng yêu / Chí Mai)
        rvc_model_path = None
        search_dirs = [
            os.path.join(os.path.dirname(__file__), "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "models", "rvc"),
            os.path.join(os.path.dirname(__file__), "..", "models", "rvc"),
        ]
        for d in search_dirs:
            if os.path.exists(d):
                for f in sorted(os.listdir(d)):
                    if f.endswith(".pth"):
                        candidate = os.path.join(d, f)
                        try:
                            if os.path.getsize(candidate) > 1024:
                                rvc_model_path = candidate
                                break
                        except OSError:
                            continue
            if rvc_model_path:
                break
                    
        v_source = "rvc" if rvc_model_path else "edge"
        v_label = "Giọng Chí Mai (RVC)" if v_source == "rvc" else "Giọng Hoài My"
        await safe_edit_status(
            status_msg,
            f"🗣️ *Bước 5/6:* Đang lồng tiếng AI ({v_label})...",
            parse_mode="Markdown"
        )
        if rvc_model_path:
            v_param = rvc_model_path
        else:
            # from audio_analysis import detect_gender
            # gender = detect_gender(vocals_audio)
            # v_param = "vi-VN-HoaiMyNeural" if gender == "female" else "vi-VN-NamMinhNeural"
            v_param = "vi-VN-HoaiMyNeural"  # Tạm thời cố định giọng nữ
        
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir, voice_source=v_source, voice_param=v_param
        )
        
        # ĐỒNG BỘ THỜI GIAN BIẾN MẤT CỦA PHỤ ĐỀ THEO GIỌNG ĐỌC
        import datetime
        for i, audio_info in enumerate(dubbing_audio_files):
            if audio_info:
                idx = audio_info["index"]
                actual_duration = audio_info.get("actual_audio_duration", 0)
                # Cập nhật end time của đoạn sub tương ứng để nó biến mất NGAY khi đọc xong
                for seg in translated_segments:
                    if seg.index == idx and actual_duration > 0:
                        new_end = seg.start + datetime.timedelta(seconds=actual_duration + 0.1) # Thêm 0.1s cho tự nhiên
                        seg.end = new_end
                        break
                        
        # CHỐNG ĐÈ SUB (Anti-Overlap): Đảm bảo sub trước phải biến mất trước khi sub sau xuất hiện
        for i in range(len(translated_segments) - 1):
            if translated_segments[i].end > translated_segments[i+1].start:
                safe_end = translated_segments[i+1].start - datetime.timedelta(seconds=0.05)
                if safe_end > translated_segments[i].start:
                    translated_segments[i].end = safe_end
                else:
                    translated_segments[i].end = translated_segments[i].start + datetime.timedelta(seconds=0.1)
        
        # ===== BƯỚC 4.5: TẠO FILE ASS (CÓ SUB DỊCH ĐÃ ĐỒNG BỘ TIMING) =====
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, floating_segments, ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)
        sub_file_to_use = ass_path
        
        # Mix giọng tiếng Việt vào nền nhạc KHÔNG CÓ LỜI (no_vocals_audio)
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(mix_audio_pydub, no_vocals_audio, dubbing_audio_files, mixed_audio, original_volume_db=-2, dubbing_volume_db=1)

        # ===== BƯỚC 6: XUẤT VIDEO =====
        await safe_edit_status(
            status_msg,
            f"👀 *Quét chữ xong!*\n`{url}`\n\n"
            "🎬 *Bước 6/6:* Đang render video (NVENC)...\n"
            "⏳ Đây là bước cuối cùng...",
            parse_mode="Markdown"
        )
        # Lấy lại main_y_pct nếu có, nếu không thì dùng mặc định 88%
        y_pct = locals().get('main_y_pct', 0.88)
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
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
    
    if worker_task is None or worker_task.done():
        worker_task = asyncio.create_task(video_worker())

    # Đưa từng URL vào hàng đợi
    for url in urls:
        queue_counter += 1
        await global_queue.put({
            'type': 'url',
            'pos': queue_counter,
            'update': update,
            'context': context,
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
        
    await global_queue.put({
        'type': 'video',
        'pos': queue_counter,
        'update': update,
        'context': context,
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

async def process_single_video(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, filename: str, pos: int):
    status_msg = await update.message.reply_text(
        f"▶️ *Đang xử lý Video tải lên (Thứ {pos} trong hàng đợi):*\n`{filename}`\n\n"
        f"⏳ Phía sau còn {global_queue.qsize()} video đang chờ...",
        parse_mode="Markdown"
    )

    try:
        file = await context.bot.get_file(file_id)
        download_dir = os.path.join(WORKSPACE, "downloads")
        os.makedirs(download_dir, exist_ok=True)
        
        # Thêm timestamp và uuid để tránh trùng lặp khi chạy hàng loạt
        import uuid
        uid = str(uuid.uuid4())[:8]
        safe_filename = f"{int(time.time())}_{uid}_{filename}"
        video_path = os.path.join(download_dir, safe_filename)

        await safe_edit_status(status_msg, "⏳ Đang tải video từ Telegram...")
        # Tăng timeout lên 600s để tránh lỗi Timed out khi tải file video lớn
        await file.download_to_drive(video_path, read_timeout=600, connect_timeout=600, pool_timeout=600, write_timeout=600)
        
        base_name = os.path.splitext(safe_filename)[0]
        paths = TelegramJobPaths.create(WORKSPACE, OUTPUT_DIR, base_name)
        paths.prepare_directories()
        out_dir = str(paths.job_directory)
        original_audio = str(paths.original_audio)
        srt_original = str(paths.original_srt)
        srt_translated = str(paths.translated_srt)
        dubbing_dir = str(paths.dubbing_directory)
        mixed_audio = str(paths.mixed_audio)
        final_video = str(paths.final_video)

        start_time = time.time()
        from pipeline_v2.config import PipelineMode, PipelineSettings
        if PipelineSettings.from_env().mode is PipelineMode.V2:
            await process_v2_telegram_job(
                video_path,
                paths,
                filename,
                status_msg,
            )
            return

        await safe_edit_status(status_msg, "🎧 Đang tách âm thanh...")
        import shared_state
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(extract_audio_from_video, video_path, original_audio)

        # ===== BƯỚC 2.5: TÁCH VOCAL BẰNG DEMUCS =====
        await safe_edit_status(status_msg, "🎧 Đang tách giọng khỏi nhạc nền (BS-RoFormer / Demucs fallback)...")
        from video_utils import separate_vocals_demucs
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        vocals_audio, no_vocals_audio = await asyncio.to_thread(separate_vocals_demucs, original_audio, out_dir)

        # ===== BƯỚC 3: NHẬN DẠNG GIỌNG NÓI =====
        await safe_edit_status(status_msg, "🤖 Qwen3-ASR đang nhận dạng và căn timestamp từ vocal sạch...")
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        srt_segments = await asyncio.to_thread(extract_subtitles_whisper, vocals_audio, srt_original)

        # ===== BƯỚC 3.5: KIỂM TRA VỊ TRÍ PHỤ ĐỀ CHÍNH VÀ QUÉT PHỤ ĐỀ CÂM =====
        await safe_edit_status(status_msg, "👀 Đang quét vùng phụ đề cố định (OCR)...", parse_mode="Markdown")
        from ocr_utils import perform_video_ocr, extract_silent_subtitles_from_gaps
        from ass_utils import generate_ass_file
        try:
            if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
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
            vid_w, vid_h, main_y_pct, floating_segments = 1920, 1080, 0.88, []
        finally:
            from ocr_utils import release_ocr_reader
            release_ocr_reader()

        # ===== BƯỚC 4: DỊCH PHỤ ĐỀ =====
        await safe_edit_status(status_msg, f"🌐 Đang dịch {len(srt_segments)} đoạn phụ đề (Có hỗ trợ AI Vision)...")
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        translated_segments = await asyncio.to_thread(translate_subtitles, srt_segments, "vi", api_key=GEMINI_API_KEY, video_path=video_path)
        await asyncio.to_thread(save_srt, translated_segments, srt_translated)

        # ===== BƯỚC 5: LỒNG TIẾNG =====
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        # Khôi phục giọng RVC (Đáng yêu / Chí Mai)
        rvc_model_path = None
        search_dirs = [
            os.path.join(os.path.dirname(__file__), "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "..", "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "MyVoiceModel_v2"),
            os.path.join(WORKSPACE, "models", "rvc"),
            os.path.join(os.path.dirname(__file__), "..", "models", "rvc"),
        ]
        for d in search_dirs:
            if os.path.exists(d):
                for f in sorted(os.listdir(d)):
                    if f.endswith(".pth"):
                        candidate = os.path.join(d, f)
                        try:
                            if os.path.getsize(candidate) > 1024:
                                rvc_model_path = candidate
                                break
                        except OSError:
                            continue
            if rvc_model_path:
                break
                    
        v_source = "rvc" if rvc_model_path else "edge"
        v_label = "Giọng Chí Mai (RVC)" if v_source == "rvc" else "Giọng Hoài My"
        await safe_edit_status(status_msg, f"🗣️ Đang lồng tiếng AI ({v_label})...")
        if rvc_model_path:
            v_param = rvc_model_path
        else:
            v_param = "vi-VN-HoaiMyNeural"  # Tạm thời cố định giọng nữ
        
        dubbing_audio_files = await generate_dubbing_audio(
            translated_segments, dubbing_dir, voice_source=v_source, voice_param=v_param
        )
        
        # ĐỒNG BỘ THỜI GIAN BIẾN MẤT CỦA PHỤ ĐỀ THEO GIỌNG ĐỌC
        import datetime
        for i, audio_info in enumerate(dubbing_audio_files):
            if audio_info:
                idx = audio_info["index"]
                actual_duration = audio_info.get("actual_audio_duration", 0)
                for seg in translated_segments:
                    if seg.index == idx and actual_duration > 0:
                        new_end = seg.start + datetime.timedelta(seconds=actual_duration + 0.1)
                        seg.end = new_end
                        break

        # CHỐNG ĐÈ SUB (Anti-Overlap): Đảm bảo sub trước phải biến mất trước khi sub sau xuất hiện
        for i in range(len(translated_segments) - 1):
            if translated_segments[i].end > translated_segments[i+1].start:
                safe_end = translated_segments[i+1].start - datetime.timedelta(seconds=0.05)
                if safe_end > translated_segments[i].start:
                    translated_segments[i].end = safe_end
                else:
                    translated_segments[i].end = translated_segments[i].start + datetime.timedelta(seconds=0.1)

        # ===== BƯỚC 4.5: TẠO FILE ASS (CÓ SUB DỊCH ĐÃ ĐỒNG BỘ TIMING) =====
        ass_path = os.path.join(out_dir, "final.ass")
        await asyncio.to_thread(generate_ass_file, translated_segments, floating_segments, ass_path, play_res_x=vid_w, play_res_y=vid_h, main_y_pct=main_y_pct)
        sub_file_to_use = ass_path

        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
        await asyncio.to_thread(mix_audio_pydub, no_vocals_audio, dubbing_audio_files, mixed_audio, original_volume_db=-2, dubbing_volume_db=1)

        # ===== BƯỚC 6: XUẤT VIDEO =====
        await safe_edit_status(status_msg, "🎬 Đang render video (NVENC)...")
        y_pct = locals().get('main_y_pct', 0.88)
        if shared_state.stop_requested: raise Exception("Bị hủy bởi lệnh /stop")
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
        logger.error(f"Error processing video: {e}", exc_info=True)
        await safe_edit_status(status_msg, f"❌ Lỗi: {str(e)[:500]}")


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
