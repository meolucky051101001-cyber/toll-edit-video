import os
import re
import time
import uuid
import requests
import subprocess
import sys
import logging
from urllib.parse import urlsplit, urlunsplit

try:
    from .pipeline_v2.atomic_io import atomic_replace_file
    from .douyin_direct import (
        DouyinDirectError,
        configured_cookie_file,
        resolve_douyin_video,
    )
    from .pipeline_v2.download_validation import (
        DownloadValidationError,
        probe_downloaded_video,
        require_complete_response,
        require_partial_content,
    )
except ImportError:  # Running telegram_bot.py directly from backend/ on Windows.
    from pipeline_v2.atomic_io import atomic_replace_file
    from douyin_direct import (
        DouyinDirectError,
        configured_cookie_file,
        resolve_douyin_video,
    )
    from pipeline_v2.download_validation import (
        DownloadValidationError,
        probe_downloaded_video,
        require_complete_response,
        require_partial_content,
    )

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == 'win32' else 0

USER_AGENTS = {
    "mobile": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1",
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

# Do not shorten this list: the same originVideoKey is not available from every
# XHS CDN/ISP combination. Ordering keeps the long-standing clean-origin patch.
XHS_ORIGIN_CDN_DOMAINS = (
    'http://sns-video-qn.xhscdn.com',
    'https://sns-video-qn.xhscdn.com',
    'http://sns-video-bd.xhscdn.com',
    'https://sns-video-bd.xhscdn.com',
    'http://sns-video-qc.xhscdn.com',
    'https://sns-video-qc.xhscdn.com',
    'http://sns-video-hw.xhscdn.com',
    'https://sns-video-hw.xhscdn.com',
    'http://sns-video-al.xhscdn.com',
    'https://sns-video-al.xhscdn.com',
    'http://sns-video-ws.xhscdn.com',
    'https://sns-video-ws.xhscdn.com',
    'http://sns-video-ct.xhscdn.com',
    'https://sns-video-ct.xhscdn.com',
    'http://sns-video-tx.xhscdn.com',
    'https://sns-video-tx.xhscdn.com',
    'http://sns-video-v27.xhscdn.com',
    'http://sns-video-v26.xhscdn.com',
    'http://sns-video-v25.xhscdn.com',
    'http://sns-video-v24.xhscdn.com',
)

def clean_filename(title: str, max_len: int = 40) -> str:
    """Lọc bỏ ký tự đặc biệt để đặt tên file an toàn trên Windows"""
    if not title:
        return "social_video"
    cleaned = re.sub(r'[\\/*?:"<>|]', '', title).strip()
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\-_.]', '', cleaned)
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned[:max_len] or "social_video"

def download_file_stream(url: str, dest_path: str, headers: dict = None, timeout: tuple = (10, 30)) -> bool:
    """Stream to a sibling temp file, ffprobe it, then publish atomically."""
    temporary_path = f"{dest_path}.{uuid.uuid4().hex}.downloading"
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        req_headers = headers or {"User-Agent": USER_AGENTS["desktop"]}
        with requests.get(url, headers=req_headers, stream=True, timeout=timeout) as response:
            require_complete_response(response.status_code, response.headers)
            with open(temporary_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=512 * 1024):  # 512KB per chunk
                    if chunk:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
        actual_size = os.path.getsize(temporary_path)
        expected_size = response.headers.get("Content-Length")
        if expected_size is not None and actual_size != int(expected_size):
            raise DownloadValidationError(
                "Downloaded byte size {} differs from Content-Length {}".format(
                    actual_size, expected_size
                )
            )
        if actual_size <= 10000:
            raise DownloadValidationError("Downloaded video is unexpectedly small")
        probe_downloaded_video(temporary_path)
        atomic_replace_file(temporary_path, dest_path)
        return True
    except Exception as e:
        logger.error(f"Lỗi tải stream từ {url[:60]}: {e}")
        if os.path.exists(temporary_path):
            try: os.remove(temporary_path)
            except OSError: pass
        return False

def extract_douyin_video_id(url: str) -> str:
    """Trích xuất ID video từ các định dạng link Douyin phức tạp (web search, modal_id, etc.)"""
    # 1. Tìm modal_id hoặc aweme_id hoặc item_id trong query params
    query_match = re.search(r'(?:modal_id|aweme_id|item_id|item_ids|video_id)=(\d+)', url)
    if query_match:
        return query_match.group(1)
        
    # 2. Tìm /video/123456 hoặc /note/123456
    path_match = re.search(r'/(?:video|note)/(\d+)', url)
    if path_match:
        return path_match.group(1)
        
    # 3. Nếu là shortlink v.douyin.com
    if "v.douyin.com" in url:
        try:
            res = requests.get(url, headers={"User-Agent": USER_AGENTS["mobile"]}, allow_redirects=True, timeout=10)
            sub_match = re.search(r'/(?:video|note)/(\d+)', res.url) or re.search(r'modal_id=(\d+)', res.url)
            if sub_match:
                return sub_match.group(1)
        except Exception as e:
            logger.warning(f"Error redirecting shortlink: {e}")
            
    return ""

# =========================================================================
# 1. BÓC TÁCH DOUYIN & TIKTOK (NO WATERMARK)
# =========================================================================
def download_douyin_tiktok(url: str, output_dir: str, prefix: str) -> tuple:
    """
    Tải video Douyin / TikTok không logo (Full HD) qua API giải mã trực tiếp.
    """
    logger.info(f"Đang giải mã Douyin/TikTok không logo: {url}")
    
    # Chuẩn hóa link nếu là link tìm kiếm trên web có modal_id
    video_id = extract_douyin_video_id(url)
    target_urls = [url]
    if video_id:
        target_urls.insert(0, f"https://www.douyin.com/video/{video_id}")
        target_urls.insert(1, f"https://www.iesdouyin.com/share/video/{video_id}/")

    resolver_error = ""

    # Chiến lược 1 (chỉ Douyin): API web chính chủ + X-Bogus, dựa trên
    # jiji262/douyin-downloader. Ưu tiên luồng CDN sạch có bitrate cao nhất.
    if video_id and any(host in url.lower() for host in ("douyin.com", "iesdouyin.com")):
        try:
            info = resolve_douyin_video(video_id)
            safe_title = clean_filename(info.title)
            target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
            for media_url in info.media_urls:
                if download_file_stream(
                    media_url,
                    target_path,
                    headers=dict(info.download_headers),
                    timeout=(10, 90),
                ):
                    logger.info("Tải thành công Douyin trực tiếp: %s", target_path)
                    return True, target_path, info.title, ""
            resolver_error = "Douyin đã trả metadata nhưng các CDN video đều thất bại"
        except DouyinDirectError as exc:
            resolver_error = str(exc)
            logger.warning("Douyin direct resolver không thành công: %s", resolver_error)

    # Chiến lược 2: TikWM Multi-platform API (không cần cookie).
    for t_url in target_urls:
        try:
            api_url = "https://www.tikwm.com/api/"
            res = requests.post(api_url, data={"url": t_url, "hd": 1}, headers={"User-Agent": USER_AGENTS["desktop"]}, timeout=12)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == 0 and "data" in data:
                    v_data = data["data"]
                    video_url = v_data.get("hdplay") or v_data.get("play")
                    title = v_data.get("title", "") or "douyin_video"
                    safe_title = clean_filename(title)
                    
                    if video_url:
                        if video_url.startswith("/"):
                            video_url = "https://www.tikwm.com" + video_url
                        
                        target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
                        if download_file_stream(video_url, target_path):
                            logger.info(f"Tải thành công Douyin/TikTok không logo: {target_path}")
                            return True, target_path, title, ""
        except Exception as e:
            logger.warning(f"TikWM thử link {t_url} lỗi: {e}")

    error = resolver_error or "Không thể bóc tách link Douyin/TikTok qua API"
    return False, "", "", error

import urllib.request
import concurrent.futures

def download_parallel_range(url: str, dest_path: str, workers: int = 6, max_retries: int = 8) -> bool:
    """
    Tải file bằng đa luồng HTTP Range song song với cơ chế tự động resume khi rớt mạng.
    Tăng tốc độ tải file từ máy chủ CDN quốc tế lên gấp 5-10 lần và đảm bảo không bị timeout.
    """
    part_paths = []
    assembled_path = None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', USER_AGENTS["desktop"])
        with urllib.request.urlopen(req, timeout=8) as resp:
            total_size = int(resp.headers.get('Content-Length', 0))
            
        if total_size <= 0:
            return False

        workers = max(1, min(int(workers), total_size))
        chunk_size = total_size // workers
        # Each URL attempt gets isolated parts. Reusing leftovers from another
        # CDN candidate can produce a byte-perfect size with mixed content.
        tmp_base = "{}.{}.range".format(dest_path, uuid.uuid4().hex)

        def _download_part(start, end, part_num):
            part_name = f"{tmp_base}_{part_num}.part"
            current_start = start
            if os.path.exists(part_name):
                current_start += os.path.getsize(part_name)

            while current_start <= end:
                import shared_state
                if getattr(shared_state, 'stop_requested', False):
                    return part_name, False

                success = False
                prev_start = current_start
                for _ in range(max_retries):
                    if getattr(shared_state, 'stop_requested', False):
                        return part_name, False
                    try:
                        p_req = urllib.request.Request(url)
                        p_req.add_header('User-Agent', USER_AGENTS["desktop"])
                        p_req.add_header('Range', f'bytes={current_start}-{end}')
                        with urllib.request.urlopen(p_req, timeout=12) as p_resp:
                            require_partial_content(
                                getattr(p_resp, "status", p_resp.getcode()),
                                p_resp.headers,
                                current_start,
                                end,
                                total_size,
                            )
                            with open(part_name, 'ab') as f:
                                while True:
                                    if getattr(shared_state, 'stop_requested', False):
                                        return part_name, False
                                    chunk = p_resp.read(128 * 1024)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    current_start += len(chunk)
                        success = True
                        break
                    except Exception:
                        time.sleep(0.5)
                if not success:
                    return part_name, False
                if current_start == prev_start:
                    # No progress made despite success (EOF reached early)
                    break
            expected_part_size = end - start + 1
            return part_name, (
                current_start == end + 1
                and os.path.getsize(part_name) == expected_part_size
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = []
            for i in range(workers):
                start = i * chunk_size
                end = total_size - 1 if i == workers - 1 else (start + chunk_size - 1)
                part_paths.append(f"{tmp_base}_{i}.part")
                futures.append(executor.submit(_download_part, start, end, i))
            results = [f.result() for f in futures]

        for p, ok in results:
            if not ok or not os.path.exists(p):
                return False

        assembled_path = f"{dest_path}.{uuid.uuid4().hex}.assembling"
        with open(assembled_path, 'wb') as out_f:
            for p, _ in results:
                with open(p, 'rb') as in_f:
                    while True:
                        chunk = in_f.read(1024 * 1024)
                        if not chunk:
                            break
                        out_f.write(chunk)
            out_f.flush()
            os.fsync(out_f.fileno())

        if os.path.getsize(assembled_path) != total_size:
            raise DownloadValidationError("Merged Range download has the wrong byte size")
        probe_downloaded_video(assembled_path)
        atomic_replace_file(assembled_path, dest_path)
        return True
    except Exception as e:
        logger.warning(f"Parallel Range download error for {url[:60]}: {e}")
        return False
    finally:
        for temporary in [*part_paths, assembled_path]:
            if temporary and os.path.exists(temporary):
                try:
                    os.remove(temporary)
                except OSError:
                    pass

# =========================================================================
# 2. BÓC TÁCH XIAOHONGSHU (REDNOTE)
# =========================================================================
def download_xiaohongshu(url: str, output_dir: str, prefix: str) -> tuple:
    """
    Tải video Xiaohongshu (Tiểu Hồng Thư) không logo chất lượng cao
    """
    logger.info(f"Đang bóc tách Xiaohongshu: {url}")
    os.makedirs(output_dir, exist_ok=True)
    try:
        import json
        headers = {
            'User-Agent': USER_AGENTS["mobile"],
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'
        }
        
        # Chuẩn hóa link rút gọn sang HTTPS để tránh bị timeout trên một số mạng
        target_fetch_url = url.strip()
        if target_fetch_url.startswith("http://xhslink.com"):
            target_fetch_url = target_fetch_url.replace("http://xhslink.com", "https://xhslink.com")

        res = None
        for attempt in range(2):
            try:
                res = requests.get(target_fetch_url, headers=headers, allow_redirects=True, timeout=(15, 25))
                if res.status_code == 200:
                    break
            except Exception as req_err:
                logger.warning(f"Lần thử {attempt + 1} tải trang XHS thất bại ({req_err})")
                if attempt == 0:
                    headers['User-Agent'] = USER_AGENTS["desktop"]

        if res is None:
            return False, "", "", "Không thể kết nối đến máy chủ Tiểu Hồng Thư (Hết thời gian chờ / Timeout mạng)"
        if res.status_code != 200:
            return (
                False,
                "",
                "",
                "Máy chủ Tiểu Hồng Thư trả về HTTP {} sau 2 lần thử".format(
                    res.status_code
                ),
            )

        real_url = res.url
        
        # 1. Kiểm tra nếu link đã bị xóa / hết hạn (XHS tự động chuyển hướng về trang chủ hoặc /explore)
        parsed_real = urlsplit(real_url)
        clean_real = urlunsplit(
            (parsed_real.scheme, parsed_real.netloc, parsed_real.path, "", "")
        ).rstrip('/').lower()
        if clean_real in ["https://www.xiaohongshu.com", "http://www.xiaohongshu.com", "https://xiaohongshu.com", 
                          "https://www.xiaohongshu.com/explore", "http://www.xiaohongshu.com/explore",
                          "https://www.xiaohongshu.com/discovery", "http://www.xiaohongshu.com/discovery"]:
            return False, "", "", "Bài viết trên Tiểu Hồng Thư (XHS) này đã bị tác giả xóa, hết hạn hoặc không tồn tại"

        html = res.text
        if any(msg in html for msg in ["你访问的页面不见了", "页面不存在", "该笔记已被删除", "笔记不存在", "Note not found"]):
            return False, "", "", "Bài viết trên Tiểu Hồng Thư này đã bị tác giả xóa (Trang không tồn tại)"

        origin_video_url = None
        backup_stream_urls = []
        title = "xiaohongshu_video"

        # 2. Trích xuất từ window.__INITIAL_STATE__
        state_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\})</script>', html, re.DOTALL)
        if state_match:
            try:
                raw_json = state_match.group(1).replace("undefined", "null")
                state = json.loads(raw_json)
                note_data = state.get("noteData", {}).get("data", {}).get("noteData", {})
                title = note_data.get("title") or note_data.get("desc", "") or "xhs_video"
                
                video = note_data.get("video")
                if video and isinstance(video, dict):
                    # ƯU TIÊN SỐ 1: Bóc tách originVideoKey (Video GỐC SẠCH 100% KHÔNG WATERMARK/LOGO)
                    origin_key = video.get("consumer", {}).get("originVideoKey")
                    if origin_key:
                        for dom in XHS_ORIGIN_CDN_DOMAINS:
                            test_origin_url = f"{dom}/{origin_key}"
                            try:
                                h_res = requests.head(test_origin_url, headers=headers, timeout=4)
                                if h_res.status_code == 200 and int(h_res.headers.get("Content-Length", 0)) > 10000:
                                    origin_video_url = test_origin_url
                                    logger.info(f"Đã tìm thấy luồng video XHS GỐC SẠCH KHÔNG LOGO: {test_origin_url}")
                                    break
                            except:
                                pass

                    # Media stream backup
                    media = video.get("media", {})
                    stream = media.get("stream", {})
                    for st_type in ['h264', 'h265', 'av1', 'h266']:
                        st_list = stream.get(st_type, [])
                        if st_list and isinstance(st_list, list):
                            for item in st_list:
                                v_u = item.get("masterUrl") or item.get("url")
                                if v_u and v_u not in backup_stream_urls:
                                    backup_stream_urls.append(v_u)
                                for b_u in item.get("backupUrls", []):
                                    if b_u and b_u not in backup_stream_urls:
                                        backup_stream_urls.append(b_u)
            except Exception as e:
                logger.warning(f"Lỗi parse JSON state của XHS: {e}")

        safe_title = clean_filename(title)
        target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")

        # 3. TẢI VIDEO GỐC SẠCH KHÔNG LOGO BẰNG RANGE MULTI-THREAD
        if origin_video_url:
            logger.info(f"Đang tải video XHS GỐC KHÔNG WATERMARK bằng đa luồng: {origin_video_url}")
            if download_parallel_range(origin_video_url, target_path, workers=6):
                logger.info(f"Tải thành công video XHS GỐC KHÔNG WATERMARK: {target_path}")
                return True, target_path, title, ""
            # Thử lại bằng stream thông thường nếu parallel lỗi
            if download_file_stream(origin_video_url, target_path, headers=headers, timeout=(10, 60)):
                logger.info(f"Tải thành công video XHS GỐC: {target_path}")
                return True, target_path, title, ""

        # 4. Dự phòng: Quét các luồng stream backup
        for v_url in backup_stream_urls:
            logger.info(f"Thử tải luồng backup stream: {v_url[:80]}...")
            if download_file_stream(v_url, target_path, headers=headers, timeout=(10, 40)):
                logger.info(f"Tải thành công video XHS (stream): {target_path}")
                return True, target_path, title, ""

        # Nếu là bài đăng dạng Album ảnh (không có video)
        if state_match and "imageList" in str(state_match.group(1)):
            return False, "", "", "Bài viết này là Album ảnh (không phải video)"

        return False, "", "", "Không tìm thấy luồng video trong bài viết Tiểu Hồng Thư"
    except Exception as e:
        logger.warning(f"Xiaohongshu Direct Scraper gặp lỗi: {e}")
        return False, "", "", f"Lỗi bóc tách XHS: {str(e)}"

# =========================================================================
# 3. BỘ ĐIỀU PHỐI ĐA NỀN TẢNG (ROUTER)
# =========================================================================
def download_social_video(url: str, output_dir: str, prefix: str) -> tuple:
    """
    Hàm tổng quản lý tải video đa nền tảng không logo:
    - Douyin / TikTok / Kuaishou / Xiaohongshu / Facebook / YouTube / X...
    """
    os.makedirs(output_dir, exist_ok=True)
    lower_url = url.lower()
    
    # 1. Nhánh Douyin & TikTok
    if any(k in lower_url for k in ["douyin.com", "iesdouyin.com", "tiktok.com", "tikwm.com"]):
        success, path, title, err = download_douyin_tiktok(url, output_dir, prefix)
        if success:
            return True, path, title, ""
            
    # 2. Nhánh Xiaohongshu
    elif any(k in lower_url for k in ["xiaohongshu.com", "xhslink.com"]):
        return download_xiaohongshu(url, output_dir, prefix)

    # 3. Chuẩn hóa URL cho yt-dlp fallback
    clean_target_url = url
    if "douyin.com" in lower_url:
        v_id = extract_douyin_video_id(url)
        if v_id:
            clean_target_url = f"https://www.douyin.com/video/{v_id}"

    logger.info(f"Sử dụng Universal Downloader cho: {clean_target_url}")
    safe_output_template = os.path.join(output_dir, f"{prefix}_%(title).30s.%(ext)s")
    
    cmd_download = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", safe_output_template,
        "--no-playlist",
        "--restrict-filenames",
        "--socket-timeout", "60",
        "--retries", "3",
        "--user-agent", USER_AGENTS["desktop"],
        "--referer", "https://www.douyin.com/",
    ]
    if "douyin.com" in lower_url:
        cookie_file = configured_cookie_file()
        if cookie_file:
            cmd_download.extend(["--cookies", cookie_file])
    cmd_download.append(clean_target_url)
    
    try:
        configured_timeout = float(
            os.getenv("SOCIAL_DOWNLOAD_TIMEOUT_SECONDS", "0")
        )
        proc = subprocess.run(
            cmd_download,
            capture_output=True,
            text=True,
            timeout=configured_timeout if configured_timeout > 0 else None,
            creationflags=CREATE_NO_WINDOW
        )
        
        downloaded = [f for f in os.listdir(output_dir) if f.startswith(prefix) and f.endswith(".mp4")]
        if downloaded:
            final_path = os.path.join(output_dir, downloaded[0])
            try:
                probe_downloaded_video(final_path)
            except DownloadValidationError as probe_error:
                logger.warning("yt-dlp output failed ffprobe validation: %s", probe_error)
                return False, "", "", str(probe_error)
            return True, final_path, downloaded[0], ""
        else:
            return False, "", "", proc.stderr[:400] if proc.stderr else "Không tìm thấy file sau khi tải"
    except subprocess.TimeoutExpired:
        return False, "", "", "Tải video quá lâu (>5 phút)"
    except Exception as e:
        return False, "", "", str(e)
