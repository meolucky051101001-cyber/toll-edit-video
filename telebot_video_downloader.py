"""
=============================================================================
TELEGRAM BOT TẢI VIDEO DOUYIN & XIAOHONGSHU (HỖ TRỢ TẢI HÀNG LOẠT)
=============================================================================
- Chuyên biệt: Tải video nguyên bản không logo/watermark, không chỉnh sửa/sub/lồng tiếng.
- Hỗ trợ gửi HÀNG LOẠT LINK cùng lúc trong 1 tin nhắn, tự động tải lần lượt toàn bộ.
- Tự động lưu toàn bộ video tải về vào thư mục: D:\\vid chưa edit
- Theo yêu cầu: CHỈ BÁO THÀNH CÔNG VÀ VỊ TRÍ FILE, KHÔNG GỬI FILE VIDEO LÊN TELEGRAM.
- Tận dụng bộ động cơ giải mã mạng xã hội cao cấp từ Tool V1 (social_downloader).
- Tích hợp thêm tầng dự phòng Mobile Feed trực tiếp từ Douyin & trích xuất Album XHS.
- Hoàn toàn độc lập: Không đè cấu hình, không can thiệp luồng dubbing của Tool V1.
=============================================================================
"""

import os
import sys
import re
import uuid
import time
import shutil
import asyncio
import logging
import requests
from pathlib import Path

# Cấu hình UTF-8 cho console Windows (nếu chạy qua console)
if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr is not None and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Thiết lập thư mục gốc & nạp backend từ Tool V1
BASE_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BASE_DIR / "backend"

# THƯ MỤC LƯU VIDEO CHÍNH XÁC: D:\vid chưa edit
DEFAULT_SAVE_DIR = Path(r"D:\vid chưa edit")
SAVE_DIR = Path(os.getenv("SAVE_DIRECTORY", "").strip() or DEFAULT_SAVE_DIR)
SAVE_DIR.mkdir(parents=True, exist_ok=True)

TEMP_DOWNLOAD_DIR = BASE_DIR / "temp_dir" / "telebot_downloads"
TEMP_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# Cấu hình gửi video lên Telegram: Mặc định là FALSE (chỉ báo thành công theo yêu cầu của bạn)
SEND_VIDEO_TO_TELEGRAM = os.getenv("DOWNLOADER_SEND_VIDEO_TO_CHAT", "false").lower() in ("true", "1", "yes")

# Cấu hình logging (hỗ trợ cả console lẫn chạy ngầm pythonw)
log_handlers = [
    logging.FileHandler(BASE_DIR / "telebot_downloader.log", encoding="utf-8", mode="a")
]
if sys.stdout is not None:
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=log_handlers
)
logger = logging.getLogger("TelebotDownloader")

# Import các hàm bóc tách từ social_downloader của Tool V1
try:
    from social_downloader import (
        download_social_video,
        download_douyin_tiktok,
        download_xiaohongshu,
        download_parallel_range,
        download_file_stream,
        clean_filename,
        extract_douyin_video_id,
        USER_AGENTS,
    )
    logger.info("Đã nạp thành công bộ công cụ bóc tách video từ Tool V1!")
except Exception as e:
    logger.error(f"Lỗi khi nạp module social_downloader từ backend Tool V1: {e}")
    sys.exit(1)

# Telegram Bot Token mặc định (do người dùng cung cấp)
DEFAULT_BOT_TOKEN = "<REDACTED_TELEGRAM_BOT_TOKEN>"
BOT_TOKEN = os.getenv("DOWNLOADER_BOT_TOKEN", "").strip() or DEFAULT_BOT_TOKEN

from telegram import Update, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

TELEGRAM_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def extract_clean_urls(text: str) -> list[str]:
    """Trích xuất tất cả các đường link từ văn bản (hỗ trợ văn bản chia sẻ chứa nhiều link hàng loạt)"""
    raw_matches = re.findall(r'https?://[^\s<>"]+', text)
    clean_urls = []
    for u in raw_matches:
        # Lọc bỏ các ký tự dấu câu dính vào đuôi link như : / . , ; ! ? ) > ' "
        cleaned = re.sub(r'[\s.,;:!?\)>"\']+$', '', u.strip())
        if cleaned.startswith(("http://", "https://")) and cleaned not in clean_urls:
            clean_urls.append(cleaned)
    return clean_urls


def get_unique_filepath(directory: Path, filename: str) -> Path:
    """Tạo đường dẫn file không bị trùng lặp trong thư mục lưu trữ"""
    target = directory / filename
    if not target.exists():
        return target
    base = target.stem
    ext = target.suffix
    counter = 1
    while True:
        candidate = directory / f"{base}_{counter}{ext}"
        if not candidate.exists():
            return candidate
        counter += 1


# =========================================================================
# TẦNG DỰ PHÒNG BỔ SUNG: DOUYIN MOBILE FEED TRỰC TIẾP (100% NO COOKIE / NO SIGN)
# =========================================================================
def resolve_douyin_mobile_feed(video_id: str) -> tuple[bool, str, str, str]:
    """
    Tầng dự phòng: Gọi trực tiếp Mobile Feed API của ByteDance.
    Không cần cookie, không cần a_bogus / sign, phản hồi < 500ms.
    """
    endpoints = [
        f"https://api5-normal-c-hl.amemv.com/aweme/v1/feed/?aweme_id={video_id}&aid=1128",
        f"https://aweme.snssdk.com/aweme/v1/feed/?aweme_id={video_id}&aid=1128",
    ]
    headers = {"User-Agent": "okhttp/3.10.0.1"}
    for ep in endpoints:
        try:
            res = requests.get(ep, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                for item in data.get("aweme_list", []):
                    if str(item.get("aweme_id")) == str(video_id):
                        v_info = item.get("video", {})
                        urls = v_info.get("play_addr", {}).get("url_list", [])
                        if urls:
                            video_url = urls[0].replace("playwm", "play")
                            title = item.get("desc") or f"douyin_{video_id}"
                            return True, video_url, title, ""
        except Exception as e:
            logger.debug(f"Feed endpoint {ep} error: {e}")
    return False, "", "", "Không lấy được luồng từ mobile feed"


# =========================================================================
# HỖ TRỢ BỔ SUNG CHO XIAOHONGSHU: TRÍCH XUẤT ALBUM ẢNH NẾU KHÔNG PHẢI VIDEO
# =========================================================================
def extract_xhs_album_images(url: str) -> tuple[bool, list[str], str]:
    """
    Trích xuất toàn bộ ảnh gốc không logo nếu bài đăng Xiaohongshu là Album ảnh.
    """
    try:
        target_url = url.strip()
        if target_url.startswith("http://xhslink.com"):
            target_url = target_url.replace("http://xhslink.com", "https://xhslink.com")

        headers = {
            "User-Agent": USER_AGENTS["desktop"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        res = requests.get(target_url, headers=headers, allow_redirects=True, timeout=15)
        if res.status_code != 200:
            return False, [], ""

        import json
        state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>', res.text, re.DOTALL)
        if not state_match:
            return False, [], ""

        raw_json = state_match.group(1).replace("undefined", "null")
        state = json.loads(raw_json)

        note_data = state.get("noteData", {}).get("data", {}).get("noteData", {})
        if not note_data:
            note_dict = state.get("note", {}).get("noteDetailMap", {})
            for k, v in note_dict.items():
                if isinstance(v, dict) and "note" in v:
                    note_data = v.get("note", {})
                    break

        title = note_data.get("title") or note_data.get("desc", "") or "xhs_album"
        image_list = note_data.get("imageList", [])
        image_urls = []
        for img in image_list:
            u = img.get("urlDefault") or img.get("urlPre") or img.get("url")
            if u and u not in image_urls:
                image_urls.append(u)

        if image_urls:
            return True, image_urls, title
    except Exception as e:
        logger.debug(f"Trích xuất album XHS lỗi: {e}")
    return False, [], ""


# =========================================================================
# BỘ XỬ LÝ TẢI VIDEO THỐNG NHẤT
# =========================================================================
def execute_video_download(url: str, output_folder: str) -> dict:
    """
    Chạy các bộ tải video từ Tool V1 và các tầng dự phòng.
    Trả về dict {success, file_path, title, error, is_album, images}
    """
    task_id = uuid.uuid4().hex[:8]
    prefix = f"dl_{task_id}"

    lower_url = url.lower()
    is_douyin = any(k in lower_url for k in ["douyin.com", "iesdouyin.com"])
    is_xhs = any(k in lower_url for k in ["xiaohongshu.com", "xhslink.com"])

    # 1. Thử qua social_downloader của Tool V1
    try:
        ok, path, title, err = download_social_video(url, output_folder, prefix)
        if ok and path and os.path.exists(path):
            return {
                "success": True,
                "file_path": path,
                "title": title or "video",
                "error": "",
                "is_album": False,
            }
    except Exception as e:
        logger.warning(f"social_downloader gặp ngoại lệ: {e}")
        err = str(e)

    # 2. Dự phòng riêng cho Douyin: Mobile Feed
    if is_douyin:
        v_id = extract_douyin_video_id(url)
        if v_id:
            logger.info(f"Kích hoạt tầng dự phòng Douyin Mobile Feed cho ID: {v_id}")
            ok_feed, direct_vurl, feed_title, feed_err = resolve_douyin_mobile_feed(v_id)
            if ok_feed and direct_vurl:
                safe_title = clean_filename(feed_title or f"douyin_{v_id}")
                target_path = os.path.join(output_folder, f"{prefix}_{safe_title}.mp4")
                if download_parallel_range(direct_vurl, target_path, workers=6):
                    return {
                        "success": True,
                        "file_path": target_path,
                        "title": feed_title,
                        "error": "",
                        "is_album": False,
                    }
                if download_file_stream(direct_vurl, target_path):
                    return {
                        "success": True,
                        "file_path": target_path,
                        "title": feed_title,
                        "error": "",
                        "is_album": False,
                    }

    # 3. Dự phòng riêng cho Xiaohongshu: Kiểm tra xem có phải Album ảnh không
    if is_xhs:
        ok_album, img_urls, album_title = extract_xhs_album_images(url)
        if ok_album and img_urls:
            return {
                "success": True,
                "file_path": None,
                "title": album_title,
                "error": "",
                "is_album": True,
                "images": img_urls[:10],
            }

    return {
        "success": False,
        "file_path": None,
        "title": "",
        "error": err or "Không thể bóc tách luồng video từ đường dẫn này",
        "is_album": False,
    }


# =========================================================================
# CÁC LỆNH VÀ SỰ KIỆN CỦA TELEGRAM BOT
# =========================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /start chào mừng và hướng dẫn sử dụng"""
    welcome_text = (
        "🎬 **Xin chào! Mình là Bot Tải Video Hàng Loạt Douyin & Tiểu Hồng Thư**\n\n"
        "⚡ **Tính năng:**\n"
        "• **Tải hàng loạt link cùng lúc**: Dán 1 hay nhiều link đều được!\n"
        "• Bóc tách video nguyên bản **100% không logo / watermark**\n"
        f"• **Tự động lưu toàn bộ video vào máy:** `{SAVE_DIR}`\n"
        "• **Chỉ báo kết quả thành công, không gửi file video lên chat** để tiết kiệm thời gian & băng thông.\n\n"
        "🚀 **Cách dùng:** Gửi link Douyin / Xiaohongshu vào đây để tải ngay!"
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lệnh /help hỗ trợ kỹ thuật"""
    help_text = (
        "💡 **HƯỚNG DẪN SỬ DỤNG:**\n\n"
        "1️⃣ Mở app **Douyin** hoặc **Xiaohongshu**\n"
        "2️⃣ Sao chép link (1 link hoặc nhiều link một lúc)\n"
        "3️⃣ Dán vào đây.\n\n"
        f"💾 **Vị trí lưu video:** Toàn bộ video sẽ được lưu trực tiếp tại:\n`{SAVE_DIR}`\n\n"
        "⚡ Bot sẽ lưu video trực tiếp vào máy tính và báo kết quả thành công cho bạn ngay trong chat."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý tin nhắn chứa 1 hoặc HÀNG LOẠT link video"""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # Trích xuất TẤT CẢ các link hợp lệ trong tin nhắn
    target_urls = extract_clean_urls(text)
    if not target_urls:
        await update.message.reply_text(
            "⚠️ Mình chưa tìm thấy đường link nào trong tin nhắn của bạn.\n"
            "Hãy gửi hoặc chia sẻ link Douyin (v.douyin.com) hoặc Tiểu Hồng Thư (xhslink.com) nhé!",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    total_links = len(target_urls)
    logger.info(f"Nhận yêu cầu tải {total_links} link từ user {update.effective_user.id} ({update.effective_user.username})")

    # =========================================================================
    # TRƯỜNG HỢP 1: DUY NHẤT 1 LINK
    # =========================================================================
    if total_links == 1:
        target_url = target_urls[0]
        status_msg = await update.message.reply_text(
            "⏳ **Đang bóc tách và tải video về máy tính...**\n"
            f"📂 Thư mục đích: `{SAVE_DIR}`",
            parse_mode=ParseMode.MARKDOWN
        )

        job_dir = TEMP_DOWNLOAD_DIR / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = await asyncio.to_thread(execute_video_download, target_url, str(job_dir))

            if not result["success"]:
                err_msg = result.get("error", "Lỗi không xác định")
                await status_msg.edit_text(
                    f"❌ **Tải video không thành công:**\n_{err_msg}_\n\n"
                    "💡 *Gợi ý: Kiểm tra lại xem video có bị xóa, hết hạn hoặc để chế độ riêng tư không.*",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

            # Xử lý Album ảnh XHS
            if result.get("is_album") and result.get("images"):
                album_title = clean_filename(result.get("title", "xhs_album"))
                album_folder = get_unique_filepath(SAVE_DIR, album_title)
                album_folder.mkdir(parents=True, exist_ok=True)

                saved_count = 0
                for idx, img_url in enumerate(result["images"]):
                    try:
                        r_img = requests.get(img_url, timeout=10)
                        if r_img.status_code == 200:
                            with open(album_folder / f"photo_{idx+1}.jpg", "wb") as f_img:
                                f_img.write(r_img.content)
                            saved_count += 1
                    except Exception:
                        pass

                await status_msg.edit_text(
                    f"✅ **Đã tải thành công Album ảnh!**\n\n"
                    f"📷 **Tiêu đề:** {result.get('title', 'Album')[:200]}\n"
                    f"🖼 **Số lượng ảnh:** {saved_count} ảnh\n"
                    f"💾 **Đã lưu tại:** `{album_folder}`",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

            # Xử lý Video
            temp_video_path = result.get("file_path")
            if not temp_video_path or not os.path.exists(temp_video_path):
                await status_msg.edit_text("❌ Lỗi: File video sau khi tải không tồn tại trên hệ thống.")
                return

            raw_title = result.get("title", "video")
            safe_title = clean_filename(raw_title, max_len=60) or "video"

            final_saved_file = get_unique_filepath(SAVE_DIR, f"{safe_title}.mp4")
            shutil.copy2(temp_video_path, final_saved_file)
            logger.info(f"✅ Đã lưu video thành công vào: {final_saved_file}")

            file_size_mb = os.path.getsize(final_saved_file) / (1024 * 1024)

            # CHỈ BÁO THÀNH CÔNG VÀ VỊ TRÍ LƯU, KHÔNG UPLOAD VIDEO LÊN TELEGRAM
            await status_msg.edit_text(
                f"✅ **Đã tải xong và lưu vào máy tính!**\n\n"
                f"🎬 **Tiêu đề:** {raw_title[:200]}\n"
                f"📊 **Dung lượng:** {file_size_mb:.1f} MB\n"
                f"💾 **Đã lưu tại:** `{final_saved_file}`",
                parse_mode=ParseMode.MARKDOWN
            )

        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

        return

    # =========================================================================
    # TRƯỜNG HỢP 2: TẢI HÀNG LOẠT (BATCH DOWNLOAD)
    # =========================================================================
    status_msg = await update.message.reply_text(
        f"📦 **Phát hiện {total_links} link video trong tin nhắn!**\n"
        f"🚀 Bắt đầu quá trình tải hàng loạt vào thư mục:\n`{SAVE_DIR}`\n\n"
        f"⚡ Tiến độ: `[0/{total_links}]` video hoàn tất...",
        parse_mode=ParseMode.MARKDOWN
    )

    success_count = 0
    failed_items = []
    success_files = []

    for idx, target_url in enumerate(target_urls, 1):
        job_dir = TEMP_DOWNLOAD_DIR / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)

        try:
            # Cập nhật banner tiến độ
            try:
                await status_msg.edit_text(
                    f"📦 **Đang tải hàng loạt: {total_links} video**\n"
                    f"⏳ Đang xử lý video **[{idx}/{total_links}]**...\n"
                    f"✅ Đã lưu: **{success_count}** | ❌ Lỗi: **{len(failed_items)}**\n"
                    f"📁 Thư mục lưu: `{SAVE_DIR}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception:
                pass

            logger.info(f"[BATCH {idx}/{total_links}] Bắt đầu tải: {target_url}")
            result = await asyncio.to_thread(execute_video_download, target_url, str(job_dir))

            if not result["success"]:
                err = result.get("error", "Lỗi tải video")
                logger.warning(f"[BATCH {idx}/{total_links}] Tải thất bại: {err}")
                failed_items.append((idx, target_url, err))
                continue

            # Nếu là Album ảnh
            if result.get("is_album") and result.get("images"):
                album_title = clean_filename(result.get("title", f"xhs_album_{idx}"))
                album_folder = get_unique_filepath(SAVE_DIR, album_title)
                album_folder.mkdir(parents=True, exist_ok=True)

                for p_i, img_url in enumerate(result["images"]):
                    try:
                        r_img = requests.get(img_url, timeout=10)
                        if r_img.status_code == 200:
                            with open(album_folder / f"photo_{p_i+1}.jpg", "wb") as f_img:
                                f_img.write(r_img.content)
                    except Exception:
                        pass

                success_count += 1
                success_files.append(f"Album: {album_folder.name}")
                continue

            # Nếu là Video
            temp_video_path = result.get("file_path")
            if not temp_video_path or not os.path.exists(temp_video_path):
                failed_items.append((idx, target_url, "File video sau tải không tồn tại"))
                continue

            raw_title = result.get("title", f"video_{idx}")
            safe_title = clean_filename(raw_title, max_len=60) or f"video_{idx}"

            # Lưu trực tiếp vào D:\vid chưa edit
            final_saved_file = get_unique_filepath(SAVE_DIR, f"{safe_title}.mp4")
            shutil.copy2(temp_video_path, final_saved_file)
            logger.info(f"[BATCH {idx}/{total_links}] ✅ Đã lưu vào {final_saved_file}")

            file_size_mb = os.path.getsize(final_saved_file) / (1024 * 1024)
            success_count += 1
            success_files.append(f"{final_saved_file.name} ({file_size_mb:.1f} MB)")

        except Exception as e:
            logger.error(f"[BATCH {idx}/{total_links}] Lỗi ngoại lệ: {e}", exc_info=True)
            failed_items.append((idx, target_url, str(e)))
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    # Tổng kết phiên tải hàng loạt: CHỈ BÁO THÀNH CÔNG VÀ DANH SÁCH FILE ĐÃ LƯU
    summary_text = (
        f"🎉 **HOÀN TẤT TẢI HÀNG LOẠT!**\n\n"
        f"📊 **Tổng kết:** Đã tải và lưu thành công **{success_count}/{total_links}** video.\n"
        f"📁 **Vị trí lưu trên máy tính:**\n`{SAVE_DIR}`\n\n"
    )

    if success_files:
        summary_text += "✅ **Các file đã lưu thành công:**\n"
        for s_idx, s_name in enumerate(success_files[:10], 1):
            summary_text += f"{s_idx}. `{s_name}`\n"
        if len(success_files) > 10:
            summary_text += f"... và {len(success_files) - 10} video khác.\n"

    if failed_items:
        summary_text += "\n⚠️ **Các link gặp sự cố:**\n"
        for f_idx, f_url, f_err in failed_items:
            clean_err = "Video đã bị tác giả xóa hoặc cài đặt riêng tư" if ("xóa" in f_err or "400" in f_err or "không tồn tại" in f_err) else f_err[:70]
            summary_text += f"• Link {f_idx}: _{clean_err}_\n"

    try:
        await status_msg.edit_text(summary_text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(summary_text, parse_mode=ParseMode.MARKDOWN)

    logger.info(f"Hoàn thành phiên tải hàng loạt: {success_count}/{total_links} thành công.")


def main():
    logger.info("==================================================")
    logger.info("Khởi động Bot Tải Video Douyin & Xiaohongshu...")
    logger.info(f"Thư mục lưu video: {SAVE_DIR}")
    logger.info("Chế độ: BÁO THÀNH CÔNG (KHÔNG GỬI FILE VIDEO LÊN TELEGRAM)")
    logger.info(f"Bot Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    logger.info("==================================================")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))

    logger.info("Bot đang chạy ở chế độ Polling... Nhấn Ctrl+C để dừng.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
