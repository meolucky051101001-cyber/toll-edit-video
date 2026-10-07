import os
import re
import time
import uuid
import json
import base64
import urllib.parse
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


def sanitize_url(url: str) -> str:
    """Redact sensitive query parameters like xsec_token, token, secret, api_key from URLs for safe logging."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
        parsed = urlparse(url)
        if not parsed.query:
            return url
        sensitive_keys = {
            "xsec_token", "token", "api_key", "secret", "auth",
            "signature", "key", "shareredid", "share_id", "password",
            "access_token", "sign", "sig", "pass", "ticket", "session",
            "credential", "hash",
        }
        qsl = parse_qsl(parsed.query, keep_blank_values=True)
        sanitized_qsl = [
            (k, "[REDACTED]" if k.lower() in sensitive_keys else v)
            for k, v in qsl
        ]
        return urlunparse(parsed._replace(query=urlencode(sanitized_qsl)))
    except Exception:
        return re.sub(r"(xsec_token|token|api_key|secret|shareRedId|share_id|sign|sig|pass|ticket|session)=[^&]+", r"\1=[REDACTED]", str(url), flags=re.IGNORECASE)


_URL_REGEX = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def sanitize_text(text: str) -> str:
    """Scrub sensitive query parameters from any URLs found within a string."""
    if not text:
        return ""
    text_str = str(text)

    def _replace_match(m: re.Match) -> str:
        raw_url = m.group(0)
        return sanitize_url(raw_url)

    return _URL_REGEX.sub(_replace_match, text_str)


def sanitize_exception(exc: BaseException, include_traceback: bool = False) -> str:
    """Format an exception with all embedded URLs sanitized of sensitive query params."""
    if exc is None:
        return ""
    if include_traceback:
        import traceback
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return sanitize_text("".join(tb_lines))
    return sanitize_text(str(exc))


class SensitiveUrlFilter(logging.Filter):
    """Logging filter that scrubs sensitive query parameters from all log messages, args, and tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = sanitize_text(record.msg)
            elif record.msg is not None:
                record.msg = sanitize_text(str(record.msg))

            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        k: (sanitize_text(v) if isinstance(v, str) else v)
                        for k, v in record.args.items()
                    }
                elif isinstance(record.args, (tuple, list)):
                    record.args = tuple(
                        sanitize_text(a) if isinstance(a, str) else (
                            sanitize_exception(a) if isinstance(a, BaseException) else a
                        )
                        for a in record.args
                    )

            if record.exc_info:
                import traceback
                raw_tb = "".join(traceback.format_exception(*record.exc_info))
                record.exc_text = sanitize_text(raw_tb)
                record.exc_info = None

            if record.exc_text:
                record.exc_text = sanitize_text(record.exc_text)
        except Exception:
            pass
        return True


logger.addFilter(SensitiveUrlFilter())


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
    try:
        if any(ord(c) > 127 for c in title):
            # Tự động khắc phục nếu chuỗi là UTF-8 bị giải mã nhầm qua Latin-1 (Mojibake)
            fixed = title.encode('latin-1').decode('utf-8')
            if fixed and len(fixed) > 0:
                title = fixed
    except Exception:
        pass
    cleaned = re.sub(r'[\\/*?:"<>|]', '', title).strip()
    cleaned = re.sub(r'[^\w\s\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\-_.]', '', cleaned)
    cleaned = re.sub(r'\s+', '_', cleaned)
    return cleaned[:max_len] or "social_video"

def download_file_stream(url: str, dest_path: str, headers: dict = None, timeout: tuple = (15, 90), max_retries: int = 4) -> bool:
    """Stream to a sibling temp file with automatic HTTP Range resuming, ffprobe it, then publish atomically."""
    temporary_path = f"{dest_path}.{uuid.uuid4().hex}.downloading"
    base_headers = dict(headers or {"User-Agent": USER_AGENTS["desktop"]})
    header_keys = [k.lower() for k in base_headers]
    if "referer" not in header_keys:
        if "douyin" in url or "zjcdn" in url:
            base_headers["Referer"] = "https://www.douyin.com/"
        elif "tiktok" in url or "byteoversea" in url:
            base_headers["Referer"] = "https://www.tiktok.com/"

    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        downloaded = 0
        expected_size = None

        for attempt in range(max_retries):
            req_headers = dict(base_headers)
            mode = "wb"
            if downloaded > 0:
                req_headers["Range"] = f"bytes={downloaded}-"
                mode = "ab"

            try:
                with requests.get(url, headers=req_headers, stream=True, timeout=timeout) as response:
                    if downloaded > 0:
                        require_partial_content(response.status_code, response.headers)
                    else:
                        require_complete_response(response.status_code, response.headers)
                        content_len = response.headers.get("Content-Length")
                        if content_len:
                            expected_size = int(content_len)

                    with open(temporary_path, mode) as f:
                        for chunk in response.iter_content(chunk_size=512 * 1024):  # 512KB per chunk
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                        f.flush()
                        os.fsync(f.fileno())

                actual_size = os.path.getsize(temporary_path)
                if expected_size is not None and actual_size < expected_size:
                    if attempt < max_retries - 1:
                        downloaded = actual_size
                        logger.warning(f"Stream bị ngắt ({actual_size}/{expected_size} bytes), tự động resume lần {attempt+1}...")
                        time.sleep(1.0)
                        continue
                    else:
                        raise DownloadValidationError(f"Downloaded byte size {actual_size} differs from Content-Length {expected_size}")

                if actual_size <= 10000:
                    raise DownloadValidationError("Downloaded video is unexpectedly small")

                probe_downloaded_video(temporary_path)
                atomic_replace_file(temporary_path, dest_path)
                return True

            except Exception as exc:
                if os.path.exists(temporary_path):
                    actual_size = os.path.getsize(temporary_path)
                    if actual_size > 0 and attempt < max_retries - 1:
                        downloaded = actual_size
                        logger.warning(f"Lỗi tải stream: {exc}. Tự động resume từ byte {downloaded} (lần {attempt+1}/{max_retries})...")
                        time.sleep(1.5)
                        continue
                if attempt == max_retries - 1:
                    raise
    except Exception as e:
        logger.error(f"Lỗi tải stream từ {sanitize_url(url)}: {sanitize_exception(e)}")
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
            res = requests.get(url, headers={"User-Agent": USER_AGENTS["mobile"]}, allow_redirects=False, timeout=10)
            loc = res.headers.get("Location") or ""
            sub_match = re.search(r'/(?:video|note)/(\d+)', loc) or re.search(r'modal_id=(\d+)', loc)
            if sub_match:
                return sub_match.group(1)
            res2 = requests.get(url, headers={"User-Agent": USER_AGENTS["mobile"]}, allow_redirects=True, timeout=10)
            sub_match2 = re.search(r'/(?:video|note)/(\d+)', res2.url) or re.search(r'modal_id=(\d+)', res2.url)
            if sub_match2:
                return sub_match2.group(1)
        except Exception as e:
            logger.warning(f"Error redirecting shortlink: {e}")
            
    return ""

def resolve_douyin_so9(url: str, video_id: str = "") -> tuple:
    """
    Bóc tách link video Douyin không watermark Full HD qua dịch vụ SO9.
    Trả về (success, video_url, title, error_message).
    """
    candidate_urls = []
    if video_id:
        candidate_urls.append(f"https://www.douyin.com/video/{video_id}")
    if url and url not in candidate_urls:
        candidate_urls.append(url)

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Referer': 'https://so9.vn/9downloader/douyin',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7',
    }

    timeout_seconds = int(os.getenv("SO9_RESOLVER_TIMEOUT", "50"))
    last_err = ""
    for target in candidate_urls:
        try:
            encoded_url = urllib.parse.quote(target.strip(), safe='')
            so9_url = f"https://so9.vn/9downloader/douyin?link={encoded_url}"
            res = requests.get(so9_url, headers=headers, timeout=12)
            if res.status_code != 200:
                last_err = f"SO9 trả về HTTP {res.status_code}"
                continue
            res.encoding = 'utf-8'

                match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', res.text)
                if not match:
                    last_err = "Không tìm thấy dữ liệu __NEXT_DATA__ từ SO9"
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    break

                payload = json.loads(match.group(1))
                d_data = payload.get("props", {}).get("pageProps", {}).get("downloadData")
                if isinstance(d_data, dict):
                    video_info = d_data.get("data", {}) or {}
                    video_url = video_info.get("video")
                    raw_title = str(video_info.get("title") or "").strip()
                    title = raw_title or (f"douyin_{video_id}" if video_id else "douyin_video")
                    if video_url:
                        logger.info("Bóc tách Douyin thành công qua SO9: %s", sanitize_url(video_url))
                        return True, video_url, title, ""
                    else:
                        msg = d_data.get("message") or "SO9 chưa sẵn sàng link tải video"
                        last_err = msg
                        if attempt == 0:
                            logger.info(f"SO9 đang xử lý video ({msg}), chờ 3s thử lại...")
                            time.sleep(3)
                            continue
            except requests.exceptions.Timeout:
                last_err = f"SO9 hết thời gian chờ ({timeout_seconds}s)"
                logger.warning(f"SO9 timeout lần {attempt + 1} với link {sanitize_url(target)}")
                if attempt == 0:
                    logger.info("SO9 có thể đang crawl video ngầm từ Douyin; thử lại lần 2 sau 3s...")
                    time.sleep(3)
                    continue
            except Exception as exc:
                last_err = f"Lỗi kết nối SO9: {exc}"
                logger.warning(f"SO9 resolver gặp lỗi với link {sanitize_url(target)}: {exc}")
                if attempt == 0:
                    time.sleep(2)
                    continue

    return False, "", "", last_err or "Bóc tách SO9 thất bại"


def resolve_douyin_viesnap(url: str, timeout: int = 15) -> tuple:
    """
    Bóc tách link video Douyin không watermark Full HD qua động cơ Viesnap (Montague engine).
    Trả về (success, video_url, title, cdn_headers, error_message).
    """
    headers = {
        "Content-Type": "application/json",
        "Origin": "https://montague.ie",
        "Referer": "https://montague.ie/",
        "User-Agent": USER_AGENTS["desktop"],
    }
    try:
        r = requests.post(
            "https://api.viesnap.com/douyin/info",
            json={"url": url.strip()},
            headers=headers,
            timeout=timeout,
        )
        if r.status_code == 200:
            r.encoding = 'utf-8'
            data = r.json()
            title = data.get("title") or data.get("description") or "douyin_video"
            qualities = data.get("qualities", {})
            best = qualities.get("best") or qualities.get("hd") or qualities.get("sd")
            if best and best.get("cdn_url"):
                cdn_url = best["cdn_url"]
                req_headers = {}
                if best.get("cdn_headers"):
                    try:
                        raw = base64.b64decode(best["cdn_headers"]).decode("utf-8")
                        req_headers = json.loads(raw)
                    except Exception:
                        req_headers = {}
                logger.info("Bóc tách Douyin thành công qua Viesnap/Montague: %s", sanitize_url(cdn_url))
                return True, cdn_url, title, req_headers, ""
            return False, "", "", {}, "Viesnap không trả về cdn_url"
        return False, "", "", {}, f"Viesnap trả về HTTP {r.status_code}"
    except Exception as e:
        return False, "", "", {}, f"Lỗi kết nối Viesnap: {sanitize_exception(e)}"

# =========================================================================
# 1. BÓC TÁCH DOUYIN & TIKTOK (NO WATERMARK)
# =========================================================================
def download_douyin_tiktok(url: str, output_dir: str, prefix: str) -> tuple:
    """
    Tải video Douyin / TikTok không logo (Full HD) qua API giải mã trực tiếp.
    """
    logger.info(f"Đang giải mã Douyin/TikTok không logo: {sanitize_url(url)}")
    os.makedirs(output_dir, exist_ok=True)
    lower_url = url.lower()
    is_douyin = any(k in lower_url for k in ["douyin.com", "iesdouyin.com"])
    
    # Chuẩn hóa link nếu là link tìm kiếm trên web có modal_id
    video_id = extract_douyin_video_id(url) if is_douyin else ""
    target_urls = [url]
    if video_id:
        target_urls.insert(0, f"https://www.douyin.com/video/{video_id}")
        target_urls.insert(1, f"https://www.iesdouyin.com/share/video/{video_id}/")

    resolver_error = ""

    # 1. Chiến lược cho Douyin:
    if is_douyin:
        diagnostics = []
        # Tầng 1: Bóc tách qua Montague / Viesnap Resolver (Tốc độ siêu nhanh ~1.5s, API trực tiếp không watermark từ Tool V1)
        try:
            logger.info("Thử bóc tách Douyin qua Montague / Viesnap Resolver...")
            t0 = time.monotonic()
            ok_vn, v_url_vn, v_title_vn, v_headers_vn, err_vn = resolve_douyin_viesnap(url)
            t_resolve = time.monotonic() - t0
            if ok_vn and v_url_vn:
                logger.info(f"[RESOLVE] Montague/Viesnap thành công trong {t_resolve:.2f}s")
                safe_title = clean_filename(v_title_vn or (f"douyin_{video_id}" if video_id else "douyin_video"))
                target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
                if download_parallel_range(v_url_vn, target_path, workers=4):
                    logger.info(f"Tải thành công Douyin không logo qua Montague/Viesnap (Range): {target_path}")
                    return True, target_path, v_title_vn, ""
                if download_file_stream(v_url_vn, target_path, headers=v_headers_vn):
                    logger.info(f"Tải thành công Douyin không logo qua Montague/Viesnap (Stream): {target_path}")
                    return True, target_path, v_title_vn, ""
                logger.warning("Tải luồng video từ Montague/Viesnap thất bại.")
        except Exception as e_vn:
            logger.warning(f"Montague/Viesnap Resolver gặp lỗi: {e_vn}")

                # Ưu tiên tải HTTP Range 6 workers song song để đạt tốc độ tối đa (~3s thay vì ~68s)
                if download_parallel_range(v_url_vn, target_path, workers=6, headers=v_headers_vn):
                    logger.info("Tải thành công Douyin không logo qua Montague/Viesnap (Range): %s", target_path)
                    return True, target_path, v_title_vn, ""

                # Fallback sang stream nếu máy chủ từ chối Range
                if download_file_stream(v_url_vn, target_path, headers=v_headers_vn):
                    logger.info("Tải thành công Douyin không logo qua Montague/Viesnap (Stream): %s", target_path)
                    return True, target_path, v_title_vn, ""

                diagnostics.append(f"Viesnap: bóc tách OK ({t_resolve:.2f}s) nhưng tải video thất bại (cả Range và Stream)")
                logger.warning("Tải luồng video từ Montague/Viesnap thất bại.")
            else:
                diagnostics.append(f"Viesnap: {err_vn or 'không trả về cdn_url'}")
        except Exception as e_vn:
            diagnostics.append(f"Viesnap: {sanitize_exception(e_vn)}")
            logger.warning("Montague/Viesnap Resolver gặp lỗi: %s", sanitize_exception(e_vn))

        # Tầng 2: API web chính chủ + X-Bogus / A-Bogus (nếu có cookie hợp lệ)
        if video_id:
            try:
                t0 = time.monotonic()
                info = resolve_douyin_video(video_id)
                t_resolve = time.monotonic() - t0
                logger.info(f"[RESOLVE] Douyin direct thành công trong {t_resolve:.2f}s")
                safe_title = clean_filename(info.title)
                target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
                for media_url in info.media_urls:
                    if download_parallel_range(media_url, target_path, workers=6, headers=dict(info.download_headers)):
                        logger.info("Tải thành công Douyin trực tiếp (Range): %s", target_path)
                        return True, target_path, info.title, ""
                    if download_file_stream(
                        media_url,
                        target_path,
                        headers=dict(info.download_headers),
                        timeout=(10, 90),
                    ):
                        logger.info("Tải thành công Douyin trực tiếp (Stream): %s", target_path)
                        return True, target_path, info.title, ""
                diagnostics.append("Direct: đã trả metadata nhưng các CDN video đều tải thất bại")
            except DouyinDirectError as exc:
                diagnostics.append(f"Direct: {exc}")
                logger.warning("Douyin direct resolver không thành công: %s", exc)
            except Exception as e_direct:
                diagnostics.append(f"Direct: {sanitize_exception(e_direct)}")
                logger.warning("Douyin direct resolver lỗi: %s", sanitize_exception(e_direct))
        else:
            diagnostics.append("Direct: không trích xuất được video ID")

        # Tầng 3: Bóc tách qua SO9 Downloader (Dự phòng chất lượng cao)
        try:
            logger.info("Thử bóc tách Douyin qua SO9 Resolver...")
            t0 = time.monotonic()
            ok, v_url, v_title, err = resolve_douyin_so9(url, video_id=video_id)
            t_resolve = time.monotonic() - t0
            if ok and v_url:
                logger.info(f"[RESOLVE] SO9 thành công trong {t_resolve:.2f}s")
                safe_title = clean_filename(v_title or (f"douyin_{video_id}" if video_id else "douyin_video"))
                target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
                if download_parallel_range(v_url, target_path, workers=6):
                    logger.info("Tải thành công Douyin không logo qua SO9 (Range): %s", target_path)
                    return True, target_path, v_title, ""
                if download_file_stream(v_url, target_path, timeout=(15, 60)):
                    logger.info("Tải thành công Douyin không logo qua SO9 (Stream): %s", target_path)
                    return True, target_path, v_title, ""
                diagnostics.append(f"SO9: bóc tách OK ({t_resolve:.2f}s) nhưng tải luồng video thất bại")
                logger.warning("Tải luồng video từ SO9 thất bại, chuyển sang chiến lược tiếp theo.")
            else:
                diagnostics.append(f"SO9: {err or 'không có link'}")
        except Exception as e_so9:
            diagnostics.append(f"SO9: {sanitize_exception(e_so9)}")
            logger.warning("SO9 Downloader gặp lỗi: %s", sanitize_exception(e_so9))

        all_err = " | ".join(diagnostics) if diagnostics else (resolver_error or "Tất cả các nguồn bóc tách Douyin đều thất bại")
        return False, "", "", f"Bóc tách Douyin thất bại: {all_err}"

    # 2. Chiến lược dành riêng cho TikTok: TikWM Multi-platform API
    for t_url in target_urls:
        try:
            api_url = "https://www.tikwm.com/api/"
            res = requests.post(api_url, data={"url": t_url, "hd": 1}, headers={"User-Agent": USER_AGENTS["desktop"]}, timeout=12)
            if res.status_code == 200:
                data = res.json()
                if data.get("code") == 0 and "data" in data:
                    v_data = data["data"]
                    video_url = v_data.get("hdplay") or v_data.get("play")
                    title = v_data.get("title", "") or "tiktok_video"
                    safe_title = clean_filename(title)
                    
                    if video_url:
                        if video_url.startswith("/"):
                            video_url = "https://www.tikwm.com" + video_url
                        
                        target_path = os.path.join(output_dir, f"{prefix}_{safe_title}.mp4")
                        if download_file_stream(video_url, target_path):
                            logger.info(f"Tải thành công Douyin/TikTok không logo: {target_path}")
                            return True, target_path, title, ""
        except Exception as e:
            logger.warning(f"TikWM thử link {sanitize_url(t_url)} lỗi: {e}")

    error = resolver_error or "Không thể bóc tách link TikTok qua API"
    return False, "", "", error

import urllib.request
import concurrent.futures

def download_parallel_range(
    url: str,
    dest_path: str,
    workers: int = 6,
    max_retries: int = 8,
    headers: dict = None,
    max_transfer_seconds: float = None,
) -> bool:
    """
    Tải file bằng đa luồng HTTP Range song song với cơ chế tự động resume khi rớt mạng.
    Tăng tốc độ tải file từ máy chủ CDN quốc tế lên gấp 5-10 lần và đảm bảo không bị timeout.
    """
    part_paths = []
    assembled_path = None
    transfer_start = time.monotonic()
    deadline = max_transfer_seconds or float(os.getenv("SOCIAL_STREAM_TIMEOUT_SECONDS", "180"))
    try:
        os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
        req = urllib.request.Request(url, method='HEAD')
        req.add_header('User-Agent', USER_AGENTS["desktop"])
        if headers:
            for k, v in headers.items():
                if k.lower() != 'user-agent':
                    req.add_header(k, str(v))
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
                if deadline and (time.monotonic() - transfer_start > deadline):
                    return part_name, False

                success = False
                prev_start = current_start
                for _ in range(max_retries):
                    if getattr(shared_state, 'stop_requested', False) or (deadline and (time.monotonic() - transfer_start > deadline)):
                        return part_name, False
                    try:
                        p_req = urllib.request.Request(url)
                        p_req.add_header('User-Agent', USER_AGENTS["desktop"])
                        if headers:
                            for hk, hv in headers.items():
                                if hk.lower() != 'user-agent':
                                    p_req.add_header(hk, str(hv))
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
                                    if getattr(shared_state, 'stop_requested', False) or (deadline and (time.monotonic() - transfer_start > deadline)):
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

        transfer_elapsed = time.monotonic() - transfer_start
        if os.path.getsize(assembled_path) != total_size:
            raise DownloadValidationError("Merged Range download has the wrong byte size")
        val_start = time.monotonic()
        probe_downloaded_video(assembled_path)
        val_elapsed = time.monotonic() - val_start
        atomic_replace_file(assembled_path, dest_path)
        speed_kb = (total_size / 1024.0) / transfer_elapsed if transfer_elapsed > 0 else 0
        logger.info(
            f"[TRANSFER] Range {workers} workers hoàn tất: {total_size / (1024*1024):.2f} MB trong {transfer_elapsed:.2f}s "
            f"({speed_kb:.1f} KB/s) | [VALIDATE] ffprobe: {val_elapsed:.2f}s"
        )
        return True
    except Exception as e:
        logger.warning(f"Parallel Range download error for {sanitize_url(url)}: {e}")
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
    logger.info(f"Đang bóc tách Xiaohongshu: {sanitize_url(url)}")
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

        res.encoding = 'utf-8'
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
                                    logger.info(f"Đã tìm thấy luồng video XHS GỐC SẠCH KHÔNG LOGO: {sanitize_url(test_origin_url)}")
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
            logger.info(f"Đang tải video XHS GỐC KHÔNG WATERMARK bằng đa luồng: {sanitize_url(origin_video_url)}")
            if download_parallel_range(origin_video_url, target_path, workers=6):
                logger.info(f"Tải thành công video XHS GỐC KHÔNG WATERMARK: {target_path}")
                return True, target_path, title, ""
            # Thử lại bằng stream thông thường nếu parallel lỗi
            if download_file_stream(origin_video_url, target_path, headers=headers, timeout=(10, 60)):
                logger.info(f"Tải thành công video XHS GỐC: {target_path}")
                return True, target_path, title, ""

        # 4. Dự phòng: Quét các luồng stream backup
        for v_url in backup_stream_urls:
            logger.info(f"Thử tải luồng backup stream: {sanitize_url(v_url)}")
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

        # Đối với Douyin: ByteDance chặn 100% các request yt-dlp nếu không có cookies (HTTP 403 Forbidden).
        # Nếu không có cookie, dừng ngay lập tức thay vì để yt-dlp treo retry 60s - 120s vô ích.
        if any(k in lower_url for k in ["douyin.com", "iesdouyin.com"]):
            cookie_file = configured_cookie_file()
            if not cookie_file:
                logger.warning("Bỏ qua tầng yt-dlp cho Douyin vì không có file cookie hợp lệ (tránh treo timeout 60s).")
                return False, "", "", err or "Bóc tách Douyin thất bại qua các tầng giải mã (Montague/Viesnap, Direct, SO9)"

    # 2. Nhánh Xiaohongshu
    elif any(k in lower_url for k in ["xiaohongshu.com", "xhslink.com"]):
        return download_xiaohongshu(url, output_dir, prefix)

    # 3. Chuẩn hóa URL cho yt-dlp fallback
    clean_target_url = url
    if "douyin.com" in lower_url:
        v_id = extract_douyin_video_id(url)
        if v_id:
            clean_target_url = f"https://www.douyin.com/video/{v_id}"

    logger.info(f"Sử dụng Universal Downloader cho: {sanitize_url(clean_target_url)}")
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
            raw_err = proc.stderr or ""
            if "403" in raw_err or "Forbidden" in raw_err or "Fresh cookies" in raw_err:
                clean_err = "Douyin chặn truy cập trực tiếp (HTTP 403 / cần cookie)"
            elif "timed out" in raw_err.lower() or "timeout" in raw_err.lower():
                clean_err = "Quá thời gian chờ tải video từ máy chủ nguồn"
            else:
                clean_err = raw_err[:250].strip() if raw_err else "Không tìm thấy file sau khi tải"
            return False, "", "", clean_err
    except subprocess.TimeoutExpired:
        return False, "", "", "Tải video quá lâu (>5 phút)"
    except Exception as e:
        return False, "", "", str(e)
