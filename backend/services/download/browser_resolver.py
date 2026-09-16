import logging
from typing import Any

from backend.models.video import Video
from backend.providers.xiaohongshu.parser import has_xhs_token, note_url
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import DownloadAdapterError

logger = logging.getLogger("download.browser_resolver")


class BrowserMediaResolver:
    """Resolves media URLs directly using in-page execution (Playwright page.evaluate)

    Inspired by Social Bulk Downloader's MAIN execution world technique, which uses
    the browser's active cookies, session, and native security tokens without needing
    external signature generators.
    """

    def __init__(
        self,
        xhs_browser_service: Any | None = None,
        douyin_browser_service: Any | None = None,
    ):
        self.xhs_browser = xhs_browser_service
        self.douyin_browser = douyin_browser_service

    @property
    def name(self) -> str:
        return "BrowserInPageResolver"

    @property
    def version(self) -> str:
        return "browser:in-page-v1"

    async def resolve(self, video: Video) -> ResolvedMedia:
        platform = (video.platform or "").strip().lower()
        if platform == "xiaohongshu":
            return await self.resolve_xiaohongshu(video)
        elif platform == "douyin":
            return await self.resolve_douyin(video)
        else:
            raise DownloadAdapterError("unsupported_platform", f"Nền tảng '{platform}' không hỗ trợ phân giải trình duyệt.")

    async def resolve_xiaohongshu(self, video: Video) -> ResolvedMedia:
        if not self.xhs_browser:
            raise DownloadAdapterError("browser_unavailable", "Dịch vụ trình duyệt Xiaohongshu chưa được cấu hình.")

        note_id = (video.platform_video_id or "").strip()
        if not note_id and video.canonical_url:
            canon = note_url(video.canonical_url)
            if canon:
                note_id = canon.rsplit("/", 1)[1]

        if not note_id:
            raise DownloadAdapterError("invalid_video_id", "Không xác định được ID bài viết Xiaohongshu.")

        token = ""
        if video.share_url and has_xhs_token(video.share_url):
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(video.share_url).query)
            tokens = qs.get("xsec_token", [])
            if tokens:
                token = tokens[0]

        try:
            page = self.xhs_browser.get_active_page()
        except Exception:
            raise DownloadAdapterError("browser_closed", "Cửa sổ trình duyệt Xiaohongshu chưa mở.")

        # In-page evaluate: 1. Check window.__INITIAL_STATE__; 2. Call in-page feed API
        js_code = """
        async (args) => {
            const { targetId, xsecToken } = args;
            // 1. Try reading DOM state
            try {
                const map = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
                const note = map[targetId]?.note;
                if (note) {
                    const originKey = note.video?.consumer?.origin_video_key;
                    if (originKey) {
                        return { mediaUrl: `https://sns-video-bd.xhscdn.com/${originKey}`, id: targetId };
                    }
                    // Check live photo streams
                    const streams = note.stream || (note.image_list && note.image_list[0]?.stream);
                    if (streams) {
                        for (const codec of ["EF4", "EF6", "EF5", "EF7"]) {
                            const list = streams[codec];
                            if (Array.isArray(list) && list.length > 0) {
                                const top = list.sort((a, b) => ((b.width||0)*(b.height||0)) - ((a.width||0)*(a.height||0)))[0];
                                const cand = top?.master_url || (top?.backup_urls && top.backup_urls[0]);
                                if (cand) return { mediaUrl: cand, id: targetId };
                            }
                        }
                    }
                }
            } catch (e) {}

            // 2. Try in-page fetch using browser session
            try {
                const res = await fetch("/api/sns/web/v1/feed", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    credentials: "include",
                    body: JSON.stringify({
                        source_note_id: targetId,
                        image_formats: ["jpg", "webp", "avif"],
                        extra: { need_body_topic: "1" },
                        xsec_source: "pc_user",
                        xsec_token: xsecToken || ""
                    })
                });
                if (res.ok) {
                    const data = await res.json();
                    const card = data?.data?.items?.[0]?.note_card;
                    const originKey = card?.video?.consumer?.origin_video_key;
                    if (originKey) {
                        return { mediaUrl: `https://sns-video-bd.xhscdn.com/${originKey}`, id: targetId };
                    }
                }
            } catch (e) {}

            return null;
        }
        """
        try:
            result = await page.evaluate(js_code, {"targetId": note_id, "xsecToken": token})
        except Exception as e:
            raise DownloadAdapterError("browser_evaluate_failed", f"Lỗi thực thi trong trang Xiaohongshu: {e}")

        if not result or not isinstance(result, dict) or not result.get("mediaUrl"):
            raise DownloadAdapterError("no_media_url", "Không thể lấy liên kết media qua phiên trình duyệt Xiaohongshu.")

        return ResolvedMedia(
            platform="xiaohongshu",
            video_id=note_id,
            media_url=result["mediaUrl"],
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.xiaohongshu.com/",
            },
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )

    async def resolve_douyin(self, video: Video) -> ResolvedMedia:
        if not self.douyin_browser:
            raise DownloadAdapterError("browser_unavailable", "Dịch vụ trình duyệt Douyin chưa được cấu hình.")

        aweme_id = (video.platform_video_id or "").strip()
        if not aweme_id and video.canonical_url:
            import re

            match = re.search(r"/video/(\d{15,25})", video.canonical_url)
            if match:
                aweme_id = match.group(1)

        if not aweme_id:
            raise DownloadAdapterError("invalid_video_id", "Không xác định được ID video Douyin.")

        try:
            page = self.douyin_browser.get_active_page()
        except Exception:
            raise DownloadAdapterError("browser_closed", "Cửa sổ trình duyệt Douyin chưa mở.")

        # In-page evaluate: Fetch /aweme/v1/web/aweme/detail/?aweme_id=... with credentials: include
        js_code = """
        async (args) => {
            const { targetId } = args;
            try {
                const params = new URLSearchParams({
                    device_platform: "webapp",
                    aid: "6383",
                    channel: "channel_pc_web",
                    aweme_id: targetId,
                    pc_client_type: "1",
                    version_code: "190500",
                    version_name: "19.5.0"
                });
                const res = await fetch(`https://www.douyin.com/aweme/v1/web/aweme/detail/?${params.toString()}`, {
                    method: "GET",
                    headers: { "User-Agent": navigator.userAgent },
                    credentials: "include"
                });
                if (res.ok) {
                    const data = await res.json();
                    const detail = data?.aweme_detail;
                    if (detail && detail.video) {
                        const bitrates = detail.video.bit_rate || [];
                        let best = null;
                        for (const b of bitrates) {
                            const resArea = (b?.play_addr?.width || 0) * (b?.play_addr?.height || 0);
                            const bestArea = (best?.play_addr?.width || 0) * (best?.play_addr?.height || 0);
                            if (resArea > bestArea || (resArea === bestArea && (b?.bit_rate || 0) > (best?.bit_rate || 0))) {
                                best = b;
                            }
                        }
                        const urls = best?.play_addr?.url_list || detail.video.play_addr?.url_list || [];
                        if (urls.length > 0) {
                            return { mediaUrl: urls[0].replace("playwm", "play"), id: targetId };
                        }
                    }
                }
            } catch (e) {}

            return null;
        }
        """
        try:
            result = await page.evaluate(js_code, {"targetId": aweme_id})
        except Exception as e:
            raise DownloadAdapterError("browser_evaluate_failed", f"Lỗi thực thi trong trang Douyin: {e}")

        if not result or not isinstance(result, dict) or not result.get("mediaUrl"):
            raise DownloadAdapterError("no_media_url", "Không thể lấy liên kết media qua phiên trình duyệt Douyin.")

        return ResolvedMedia(
            platform="douyin",
            video_id=aweme_id,
            media_url=result["mediaUrl"],
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
            },
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )
