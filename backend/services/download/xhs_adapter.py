import httpx

from backend.core.config import settings
from backend.models.video import Video
from backend.providers.xiaohongshu.parser import has_xhs_token, note_url
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter, DownloadAdapterError

DEFAULT_XHS_URL = "http://127.0.0.1:5556"
XHS_COMMIT = "cc7c78088afc09082f54ea6263a9fd07c2fa510f"


class XhsDownloadAdapter(BaseDownloadAdapter):
    def __init__(self, base_url: str = DEFAULT_XHS_URL, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "XHS-Downloader"

    @property
    def version(self) -> str:
        return f"xhs-downloader:{XHS_COMMIT[:7]}"

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{self.base_url}/")
                return res.status_code in (200, 307, 308)
        except Exception:
            return False

    async def resolve(self, video: Video) -> ResolvedMedia:
        # 1. Determine target URL, strictly prioritizing valid token URL
        expected_id = (video.platform_video_id or "").strip().lower()
        if not expected_id and video.canonical_url:
            canon = note_url(video.canonical_url)
            if canon:
                expected_id = canon.rsplit("/", 1)[1].lower()

        target_url = video.canonical_url or video.url
        if video.share_url and has_xhs_token(video.share_url):
            cand_canon = note_url(video.share_url)
            if cand_canon and (not expected_id or cand_canon.rsplit("/", 1)[1].lower() == expected_id):
                target_url = video.share_url

        # 2. Extract active cookie (configured in settings or browser session)
        cookie = (settings.xhs_cookie or "").strip()
        if not cookie:
            try:
                from backend.services.cookie_service import get_browser_session_cookie

                cookie = get_browser_session_cookie("xiaohongshu")
            except Exception:
                pass

        # 3. Query upstream local service with cookie injection
        payload = {"url": target_url, "download": False}
        if cookie:
            payload["cookie"] = cookie
        endpoint = f"{self.base_url}/xhs/detail"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(endpoint, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise DownloadAdapterError(
                "service_unavailable",
                f"Dịch vụ tải Xiaohongshu cục bộ ({self.base_url}) chưa được khởi động.",
            )
        except Exception:
            raise DownloadAdapterError("upstream_network_error", "Lỗi kết nối tới upstream Xiaohongshu.")

        if res.status_code != 200:
            raise DownloadAdapterError(
                "upstream_error",
                f"Dịch vụ Xiaohongshu trả mã lỗi HTTP {res.status_code}.",
            )

        try:
            res_json = res.json()
        except Exception:
            raise DownloadAdapterError("upstream_invalid_json", "Dịch vụ Xiaohongshu không trả dữ liệu JSON hợp lệ.")

        data = res_json.get("data")
        if not data or not isinstance(data, dict):
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Xiaohongshu thiếu dữ liệu chi tiết (data).",
            )

        # 3. Verify Note ID (strict, missing ID must fail)
        resp_id = str(data.get("作品ID") or "").strip().lower()
        if not resp_id:
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Xiaohongshu thiếu ID bài viết (作品ID).",
            )

        if expected_id and resp_id != expected_id:
            raise DownloadAdapterError(
                "mismatched_video_id",
                f"ID bài viết trả về ({resp_id}) không khớp với video yêu cầu ({expected_id}).",
            )

        # 4. Verify Content Type (strict: video, or live photo with video stream)
        post_type = str(data.get("作品类型") or "").strip()
        if not post_type:
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Xiaohongshu thiếu loại tác phẩm (作品类型).",
            )

        has_video_key = bool(
            data.get("originVideoKey")
            or (data.get("video") if isinstance(data.get("video"), dict) else {}).get("consumer", {}).get("originVideoKey")
        )
        is_live_photo = bool(data.get("live_photo") or data.get("is_live_photo"))
        has_stream = bool(data.get("stream"))

        if post_type not in ("视频", "video") and not has_video_key and not is_live_photo and not has_stream:
            raise DownloadAdapterError(
                "not_a_video",
                f"Nội dung này là bài {post_type}, không phải video.",
            )

        # 5. Extract Media URL (prioritizing uncompressed origin CDN url from originVideoKey)
        media_url: str | None = None
        origin_key = (
            data.get("originVideoKey")
            or (data.get("video") if isinstance(data.get("video"), dict) else {}).get("consumer", {}).get("originVideoKey")
        )
        if origin_key and isinstance(origin_key, str) and origin_key.strip():
            media_url = f"https://sns-video-bd.xhscdn.com/{origin_key.strip()}"

        # If not origin_key, check Live Photo video streams (codec priority ["EF4", "EF6", "EF5", "EF7"])
        if not media_url:
            def _extract_stream_url(stream_dict: dict) -> str | None:
                if not isinstance(stream_dict, dict):
                    return None
                for codec in ["EF4", "EF6", "EF5", "EF7"]:
                    streams = stream_dict.get(codec)
                    if isinstance(streams, list) and streams:
                        def _stream_rank(s: dict) -> tuple[int, int]:
                            w = int(s.get("width") or 0)
                            h = int(s.get("height") or 0)
                            br = int(s.get("avg_bitrate") or 0)
                            return (w * h, br)

                        sorted_streams = sorted([s for s in streams if isinstance(s, dict)], key=_stream_rank, reverse=True)
                        if sorted_streams:
                            top = sorted_streams[0]
                            cand = top.get("master_url") or (top.get("backup_urls") or [None])[0]
                            if cand and isinstance(cand, str) and cand.startswith("http"):
                                return cand
                return None

            if isinstance(data.get("stream"), dict):
                media_url = _extract_stream_url(data["stream"])

            if not media_url and isinstance(data.get("image_list"), list):
                for img in data["image_list"]:
                    if isinstance(img, dict) and isinstance(img.get("stream"), dict):
                        cand = _extract_stream_url(img["stream"])
                        if cand:
                            media_url = cand
                            break

        if not media_url:
            dl_addr = data.get("下载地址")
            if isinstance(dl_addr, list) and dl_addr:
                cand = str(dl_addr[0]).strip()
                if cand:
                    media_url = cand
            elif isinstance(dl_addr, str) and dl_addr.strip():
                cand = dl_addr.strip().split()[0]
                if cand:
                    media_url = cand

        if not media_url:
            raise DownloadAdapterError(
                "no_media_url",
                "Không tìm thấy liên kết media trong phản hồi của Xiaohongshu.",
            )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.xiaohongshu.com/",
        }
        if cookie:
            headers["Cookie"] = cookie

        return ResolvedMedia(
            platform="xiaohongshu",
            video_id=resp_id,
            media_url=media_url,
            headers=headers,
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )
