import re

import httpx

from backend.core.config import settings
from backend.models.video import Video
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter, DownloadAdapterError

DEFAULT_DOUYIN_URL = "http://127.0.0.1:5555"
DOUYIN_COMMIT = "42784ffc83a72a516bfe952153ad7e2a3998d16c"


class DouyinDownloadAdapter(BaseDownloadAdapter):
    def __init__(self, base_url: str = DEFAULT_DOUYIN_URL, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "Douyin_TikTok_Download_API"

    @property
    def version(self) -> str:
        return f"douyin-download-api:{DOUYIN_COMMIT[:7]}"

    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(f"{self.base_url}/docs")
                return res.status_code == 200
        except Exception:
            return False

    async def resolve(self, video: Video) -> ResolvedMedia:
        # 1. Determine expected video ID and canonical URL
        expected_id = (video.platform_video_id or "").strip()
        if not expected_id and video.canonical_url:
            match = re.search(r"/video/(\d{15,25})", video.canonical_url)
            if match:
                expected_id = match.group(1)

        target_url = video.canonical_url or video.url
        if expected_id and "/video/" not in target_url:
            target_url = f"https://www.douyin.com/video/{expected_id}"

        # 2. Extract active cookie (configured in settings or browser session)
        cookie = (settings.douyin_cookie or "").strip()
        if not cookie:
            try:
                from backend.services.cookie_service import get_browser_session_cookie

                cookie = get_browser_session_cookie("douyin")
            except Exception:
                pass

        # 3. Query upstream local service
        clean_base = self.base_url.rstrip("/")
        if clean_base.endswith("/api"):
            endpoint = f"{clean_base}/hybrid/video_data"
        else:
            endpoint = f"{clean_base}/api/hybrid/video_data"

        params = {"url": target_url, "minimal": "true"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.get(endpoint, params=params)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise DownloadAdapterError(
                "service_unavailable",
                f"Dịch vụ tải Douyin cục bộ ({self.base_url}) chưa được khởi động.",
            )
        except Exception:
            raise DownloadAdapterError("upstream_network_error", "Lỗi kết nối tới upstream Douyin.")

        if res.status_code != 200:
            raise DownloadAdapterError(
                "upstream_error",
                f"Dịch vụ Douyin trả mã lỗi HTTP {res.status_code}.",
            )

        try:
            res_json = res.json()
        except Exception:
            raise DownloadAdapterError("upstream_invalid_json", "Dịch vụ Douyin không trả dữ liệu JSON hợp lệ.")

        data = res_json.get("data")
        if not data or not isinstance(data, dict):
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Douyin thiếu dữ liệu chi tiết (data).",
            )

        # 3. Verify Video ID (strict, missing ID must fail)
        resp_id = str(data.get("video_id") or data.get("aweme_id") or "").strip()
        if not resp_id:
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Douyin thiếu ID video (video_id).",
            )

        if expected_id and resp_id != expected_id:
            raise DownloadAdapterError(
                "mismatched_video_id",
                f"ID video trả về ({resp_id}) không khớp với video yêu cầu ({expected_id}).",
            )

        # 4. Verify Content Type (strict, missing type must fail)
        data_type = str(data.get("type") or "").strip().lower()
        if not data_type:
            raise DownloadAdapterError(
                "invalid_response",
                "Phản hồi từ Douyin thiếu loại nội dung (type).",
            )
        if data_type != "video":
            raise DownloadAdapterError(
                "not_a_video",
                f"Nội dung này là dạng {data_type}, không phải video.",
            )

        # 5. Verify nested aweme_detail if present
        if "aweme_detail" in data:
            detail = data["aweme_detail"]
            if isinstance(detail, dict):
                nested_aweme_id = str(detail.get("aweme_id") or "").strip()
                if nested_aweme_id:
                    if expected_id and nested_aweme_id != expected_id:
                        raise DownloadAdapterError(
                            "mismatched_video_id",
                            f"ID trong aweme_detail ({nested_aweme_id}) không khớp với video yêu cầu ({expected_id}).",
                        )
                    if resp_id and nested_aweme_id != resp_id:
                        raise DownloadAdapterError(
                            "mismatched_video_id",
                            f"ID trong aweme_detail ({nested_aweme_id}) không khớp với video_id ({resp_id}).",
                        )

        # 6. Extract Media URL (prioritizing max-quality unwatermarked stream: pixel area width*height first, then bitrate)
        video_data = data.get("video_data")
        if not isinstance(video_data, dict):
            video_data = {}

        media_url: str | None = None

        def _extract_highest_quality_video_url(video_obj: dict) -> str | None:
            if not isinstance(video_obj, dict):
                return None
            bitrates = video_obj.get("bit_rate")
            if isinstance(bitrates, list) and bitrates:
                def _rank(b: dict) -> tuple[int, int]:
                    pa = b.get("play_addr") or {}
                    w = int(pa.get("width") or 0) if isinstance(pa, dict) else 0
                    h = int(pa.get("height") or 0) if isinstance(pa, dict) else 0
                    br = int(b.get("bit_rate") or 0)
                    return (w * h, br)

                sorted_b = sorted([b for b in bitrates if isinstance(b, dict)], key=_rank, reverse=True)
                for b in sorted_b:
                    play_addr = b.get("play_addr", {})
                    if isinstance(play_addr, dict):
                        urls = play_addr.get("url_list", [])
                        if isinstance(urls, list) and urls:
                            cand = str(urls[0]).strip().replace("playwm", "play")
                            if cand.startswith("http"):
                                return cand

            # Fallback to play_addr directly
            play_addr = video_obj.get("play_addr", {})
            if isinstance(play_addr, dict):
                urls = play_addr.get("url_list", [])
                if isinstance(urls, list) and urls:
                    cand = str(urls[0]).strip().replace("playwm", "play")
                    if cand.startswith("http"):
                        return cand

            # Fallback to download_addr directly (from extension technique)
            dl_addr = video_obj.get("download_addr", {})
            if isinstance(dl_addr, dict):
                urls = dl_addr.get("url_list", [])
                if isinstance(urls, list) and urls:
                    cand = str(urls[0]).strip().replace("playwm", "play")
                    if cand.startswith("http"):
                        return cand
            return None

        # Check aweme_detail video for highest resolution stream first
        if "aweme_detail" in data and isinstance(data["aweme_detail"], dict):
            detail = data["aweme_detail"]
            video_obj = detail.get("video", {})
            if isinstance(video_obj, dict):
                media_url = _extract_highest_quality_video_url(video_obj)

            # Support embedded video inside images list (e.g. dynamic/live photo post)
            if not media_url:
                images = detail.get("images")
                if isinstance(images, list):
                    for img in images:
                        if isinstance(img, dict) and isinstance(img.get("video"), dict):
                            cand = _extract_highest_quality_video_url(img["video"])
                            if cand:
                                media_url = cand
                                break

        if not media_url:
            media_url = (
                video_data.get("nwm_video_url_HQ")
                or video_data.get("nwm_video_url")
                or video_data.get("wm_video_url_HQ")
                or video_data.get("wm_video_url")
            )

        if not media_url:
            raise DownloadAdapterError(
                "no_media_url",
                "Không tìm thấy liên kết media trong phản hồi của Douyin.",
            )

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.douyin.com/",
        }
        if cookie:
            headers["Cookie"] = cookie

        return ResolvedMedia(
            platform="douyin",
            video_id=resp_id,
            media_url=str(media_url),
            headers=headers,
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )
