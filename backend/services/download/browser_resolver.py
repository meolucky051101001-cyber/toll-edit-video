import asyncio
import logging
import re
from typing import Any

from backend.models.video import Video
from backend.providers.xiaohongshu.parser import has_xhs_token, note_url
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import DownloadAdapterError

logger = logging.getLogger("download.browser_resolver")

XHS_RESOLVE_SCRIPT = """
async (args) => {
    const { targetId, xsecToken } = args;
    try {
        const map = window.__INITIAL_STATE__?.note?.noteDetailMap || {};
        const note = map[targetId]?.note;
        if (note) {
            const originKey = note.video?.consumer?.origin_video_key;
            if (originKey) {
                return { mediaUrl: `https://sns-video-bd.xhscdn.com/${originKey}`, id: targetId };
            }
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

DOUYIN_RESOLVE_SCRIPT = """
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

DOUYIN_DOM_STATE_SCRIPT = """
() => {
    try {
        // 1. Direct <video> element on page
        const video = document.querySelector('video');
        if (video) {
            const src = video.currentSrc || video.src || video.querySelector('source')?.src;
            if (src && src.startsWith('http') && !src.startsWith('blob:')) {
                return { mediaUrl: src.replace('playwm', 'play') };
            }
        }

        // 2. window._ROUTER_DATA
        const rdata = window._ROUTER_DATA;
        if (rdata && rdata.loaderData) {
            for (const key of Object.keys(rdata.loaderData)) {
                const item = rdata.loaderData[key]?.videoInfoRes?.item_list?.[0];
                if (item && item.video) {
                    const list = item.video.play_addr?.url_list || item.video.play_addr_h264?.url_list || [];
                    if (list.length > 0) {
                        return { mediaUrl: list[0].replace('playwm', 'play') };
                    }
                }
            }
        }

        // 3. window.__INIT_DATA__ or RENDER_DATA script tag
        const renderScript = document.getElementById('RENDER_DATA');
        if (renderScript && renderScript.textContent) {
            const raw = decodeURIComponent(renderScript.textContent);
            const parsed = JSON.parse(raw);
            for (const key of Object.keys(parsed)) {
                const item = parsed[key]?.aweme?.detail || parsed[key]?.item_list?.[0];
                if (item && item.video) {
                    const list = item.video.play_addr?.url_list || [];
                    if (list.length > 0) {
                        return { mediaUrl: list[0].replace('playwm', 'play') };
                    }
                }
            }
        }
    } catch (e) {}

    return null;
}
"""


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
        browser = self.xhs_browser
        if browser is None:
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

        async def _eval_xhs():
            try:
                page = browser.get_active_page()
            except Exception:
                raise DownloadAdapterError("browser_closed", "Cửa sổ trình duyệt Xiaohongshu chưa mở.")
            try:
                return await page.evaluate(XHS_RESOLVE_SCRIPT, {"targetId": note_id, "xsecToken": token})
            except Exception as e:
                raise DownloadAdapterError("browser_evaluate_failed", f"Lỗi thực thi trong trang Xiaohongshu: {e}")

        if hasattr(browser, "lock") and browser.lock:
            async with browser.lock:
                result = await _eval_xhs()
        else:
            result = await _eval_xhs()

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
        browser = self.douyin_browser
        if browser is None:
            raise DownloadAdapterError("browser_unavailable", "Dịch vụ trình duyệt Douyin chưa được cấu hình.")

        aweme_id = (video.platform_video_id or "").strip()
        if not aweme_id and (video.canonical_url or video.url):
            target_str = video.canonical_url or video.url or ""
            match = re.search(r"/video/(\d{15,25})", target_str)
            if match:
                aweme_id = match.group(1)

        if not aweme_id:
            raise DownloadAdapterError("invalid_video_id", "Không xác định được ID video Douyin.")

        video_url = f"https://www.douyin.com/video/{aweme_id}"

        async def _resolve_internal():
            # 1. Ensure browser is open
            try:
                page = browser.get_active_page()
            except Exception:
                try:
                    page = await browser.ensure_open()
                except Exception as e:
                    raise DownloadAdapterError("browser_unavailable", f"Không thể mở trình duyệt Douyin: {e}")

            # Enable media requests if supported
            if hasattr(browser, "set_allow_media"):
                browser.set_allow_media(True)

            media_url = None
            captured_urls = []

            # 2. Strategy A: Tab navigation with network interception & DOM/state evaluation
            context = getattr(browser, "context", None)
            if context:
                tab = None
                try:
                    tab = await context.new_page()

                    def handle_response(resp):
                        u = resp.url
                        if any(k in u for k in ("douyinvod.com", "/aweme/v1/play", "video/tos", ".snssdk.com/video")):
                            clean = u.replace("playwm", "play")
                            captured_urls.append(clean)

                    tab.on("response", handle_response)
                    try:
                        await tab.goto(video_url, wait_until="domcontentloaded", timeout=12000)
                        # Wait for player or state initialization
                        for _ in range(6):
                            if captured_urls:
                                media_url = captured_urls[0]
                                break
                            dom_res = await tab.evaluate(DOUYIN_DOM_STATE_SCRIPT)
                            if dom_res and dom_res.get("mediaUrl"):
                                media_url = dom_res["mediaUrl"]
                                break
                            await asyncio.sleep(0.4)
                    except Exception as nav_err:
                        logger.warning("Douyin tab navigation/evaluation error for %s: %s", aweme_id, nav_err)
                finally:
                    if tab and not tab.is_closed():
                        try:
                            await tab.close()
                        except Exception:
                            pass
                    if hasattr(browser, "set_allow_media"):
                        browser.set_allow_media(False)

            if media_url:
                return {"mediaUrl": media_url}

            # 3. Strategy B: In-page evaluate on active page (fallback)
            try:
                res = await page.evaluate(DOUYIN_DOM_STATE_SCRIPT)
                if res and res.get("mediaUrl"):
                    return res
                return await page.evaluate(DOUYIN_RESOLVE_SCRIPT, {"targetId": aweme_id})
            except Exception as e:
                logger.warning("Douyin in-page fallback evaluate error for %s: %s", aweme_id, e)
                return None

        if hasattr(browser, "lock") and browser.lock:
            async with browser.lock:
                result = await _resolve_internal()
        else:
            result = await _resolve_internal()

        if not result or not isinstance(result, dict) or not result.get("mediaUrl"):
            raise DownloadAdapterError("no_media_url", "Không thể lấy liên kết media qua phiên trình duyệt Douyin.")

        clean_url = result["mediaUrl"].replace("playwm", "play")
        return ResolvedMedia(
            platform="douyin",
            video_id=aweme_id,
            media_url=clean_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Referer": "https://www.douyin.com/",
            },
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )
