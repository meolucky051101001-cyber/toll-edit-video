import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import quote, unquote, urlsplit

from playwright.async_api import Error, Page
from playwright.async_api import TimeoutError as BrowserTimeout
from pydantic import HttpUrl, ValidationError

from backend.providers.base import (
    NetworkError,
    ProviderError,
    ProviderTimeoutError,
    SearchProvider,
    SelectorChangedError,
    UserActionRequired,
)
from backend.providers.douyin.parser import (
    parse_count_text,
    parse_detail,
    validate_image_url,
    video_url,
)
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.douyin_browser_service import MESSAGES, DouyinBrowserService
from backend.services.media_parser import extract_hashtags, parse_duration_seconds

logger = logging.getLogger(__name__)



DETAIL_SCRIPT = """() => {
  const video = document.querySelector('video');
  const meta = (name) => document.querySelector(`meta[property="${name}"],meta[name="${name}"]`)?.content || null;

  const titleEl = document.querySelector('[data-e2e="video-desc"], h1, .title, [class*="video-info-detail"]');
  const descEl = document.querySelector('[data-e2e="video-desc"], .desc, .caption');

  const authorNameEl = document.querySelector('[data-e2e="video-author-name"], .author-name, .account-name, .author-info .name, a[href*="/user/"] span');
  const authorLinkEl = document.querySelector('a[href*="/user/"], [data-e2e="video-author-avatar"] a');

  const likeEl = document.querySelector('[data-e2e="like-count"], [data-e2e="video-player-digg"] span, [class*="like"] .count, [class*="digg"] .count');
  const commentEl = document.querySelector('[data-e2e="comment-count"], [class*="comment"] .count');
  const favoriteEl = document.querySelector('[data-e2e="favorite-count"], [class*="favorite"] .count, [class*="collect"] .count');
  const shareEl = document.querySelector('[data-e2e="share-count"], [class*="share"] .count');

  const hashtags = Array.from(document.querySelectorAll('a[href*="/hashtag/"], a[href*="/tag/"], [data-e2e="video-desc"] a'))
    .map(el => el.innerText.trim())
    .filter(text => text.startsWith('#') || text.length > 0);

  const durationSeconds = video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null;
  const caption = descEl ? descEl.innerText.trim() : null;
  const likeText = likeEl ? likeEl.innerText.trim() : null;
  const commentText = commentEl ? commentEl.innerText.trim() : null;
  const favoriteText = favoriteEl ? favoriteEl.innerText.trim() : null;
  const shareText = shareEl ? shareEl.innerText.trim() : null;
  const poster = meta('og:image') || (video && video.poster ? video.poster : null);

  return {
    has_video: Boolean(video),
    title: titleEl ? titleEl.innerText.trim() : null,
    caption: caption,
    description: caption,
    author_name: authorNameEl ? authorNameEl.innerText.trim() : null,
    author_url: authorLinkEl ? authorLinkEl.href : null,
    like_count: likeText,
    like_text: likeText,
    comment_count: commentText,
    comment_text: commentText,
    favorite_count: favoriteText,
    collect_text: favoriteText,
    share_count: shareText,
    share_text: shareText,
    duration_seconds: durationSeconds,
    duration: durationSeconds,
    thumbnail_url: poster,
    poster: poster,
    hashtags: hashtags,
    keywords: [],
    published_at: null,
  };
}"""


class DouyinProvider(SearchProvider):
    def __init__(self, browser: DouyinBrowserService, auto_elevate: bool = False):
        self.browser = browser
        self.auto_elevate = auto_elevate

    async def close(self) -> None:
        if hasattr(self.browser, "release_blocked_page"):
            await self.browser.release_blocked_page()

    async def gate(self, page: Page, partial: list[VideoResult] | None = None) -> None:
        state = await self.browser.inspect(page)
        if state in {"login_required", "verification_required", "restricted"}:
            if self.auto_elevate and getattr(self.browser, "is_currently_headless", False):
                try:
                    await self.browser.elevate_to_headful()
                except Exception:
                    pass
            raise UserActionRequired(
                "waiting_for_login" if state == "login_required" else "waiting_for_user",
                MESSAGES[state],
                partial=partial,
            )
        if state == "closed":
            raise UserActionRequired("waiting_for_user", MESSAGES[state], partial=partial)
        if state == "network_error":
            raise NetworkError(MESSAGES[state])

    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        async with self.browser.lock:
            if hasattr(self.browser, "release_blocked_page"):
                await self.browser.release_blocked_page()
            results: list[VideoResult] = []
            try:
                page = await self.browser.ensure_open()
                # Pre-check gate only if already restricted or verification required
                initial_state = await self.browser.inspect(page)
                if initial_state in {"verification_required", "restricted"}:
                    await self.gate(page, results)
                base = getattr(self.browser, "base_url", "https://www.douyin.com")

                direct_url = f"{base}/search/{quote(query)}?type=video"
                await page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector('a[href*="/video/"], [data-e2e="search-box"]', timeout=2000)
                except Error:
                    await page.wait_for_timeout(1000)
                await self.gate(page, results)
                current_url = urlsplit(page.url)
                unquoted_path = unquote(current_url.path)
                on_search_page = unquoted_path.startswith("/search")
                if not on_search_page:
                    raise SelectorChangedError(
                        "Trang tìm kiếm Douyin bị chuyển hướng khỏi kết quả tìm kiếm. Dừng để không lưu nhầm video đề xuất."
                    )
                path_parts = unquoted_path.strip("/").split("/")
                if len(path_parts) >= 2 and path_parts[0] == "search":
                    current_kw = path_parts[1].split("?")[0].strip().lower()
                    expected_kw = query.strip().lower()
                    if current_kw != expected_kw:
                        raise SelectorChangedError(
                            f"Trang tìm kiếm Douyin bị chuyển hướng sang từ khóa khác ({current_kw!r} != {expected_kw!r}). Dừng để không lưu nhầm kết quả."
                        )

                candidates: dict[str, dict[str, str | None]] = {}
                max_scrolls = max(15, int(limit * 1.2))
                target_candidates = min(max(limit * 2, 20), 50)
                prev_count = 0
                stale_scrolls = 0

                for _ in range(max_scrolls):
                    await self.gate(page, results)
                    items = await page.locator('a[href*="/video/"]').evaluate_all(
                        """els => {
                            const res = [];
                            for (const e of els) {
                                const card = e.closest('li, [class*="search-result-card"], [class*="card"], [class*="item"], [class*="search-result"]');
                                if (!card) continue;
                                if (card.closest('.guess-words, [data-e2e="search_guess_words"], [class*="recommend"], [class*="hot-search"], [class*="ad-"]')) continue;
                                const img = card ? (card.querySelector('img') || e.querySelector('img')) : e.querySelector('img');
                                const cover = img ? (img.currentSrc || img.src || img.getAttribute('data-src')) : null;
                                const title = card ? (card.querySelector('[class*="title"], [class*="desc"], p, h3')?.innerText?.trim() || null) : null;
                                const author = card ? (card.querySelector('[class*="author"], [class*="name"], .author')?.innerText?.trim() || null) : null;
                                const authorLink = card ? (card.querySelector('a[href*="/user/"]')?.href || null) : null;
                                const likeText = card ? (card.querySelector('[class*="like"], [class*="count"]')?.innerText?.trim() || null) : null;
                                const durationEl = card ? (
                                    card.querySelector('[class*="duration"], [class*="time-tag"], [class*="time_tag"], span.time') ||
                                    Array.from(card.querySelectorAll('span, div')).find(s => /^\\d{1,2}:\\d{2}(:\\d{2})?$/.test(s.innerText ? s.innerText.trim() : ''))
                                ) : null;
                                const durationText = durationEl ? durationEl.innerText.trim() : null;
                                res.push({
                                    href: e.href,
                                    cover: cover,
                                    title: title,
                                    author: author,
                                    author_link: authorLink,
                                    like_text: likeText,
                                    duration_text: durationText,
                                });
                            }
                            return res;
                        }"""
                    )
                    for item in items:
                        link = item.get("href")
                        if not link:
                            continue
                        canonical = video_url(link)
                        if canonical:
                            vid_id = canonical.rsplit("/", 1)[1]
                            cover = item.get("cover")
                            if vid_id not in candidates or (cover and not candidates[vid_id].get("cover")):
                                candidates[vid_id] = {
                                    "link": canonical,
                                    "cover": cover,
                                    "title": item.get("title"),
                                    "author": item.get("author"),
                                    "author_link": item.get("author_link"),
                                    "like_text": item.get("like_text"),
                                    "duration_text": item.get("duration_text"),
                                }
                    video_count = sum(1 for c in candidates.values() if c.get("title"))
                    if video_count >= limit or len(candidates) >= target_candidates:
                        break

                    # Auto dismiss any guest / login modal on scroll
                    try:
                        modal = page.locator(
                            ".dy-account-close:visible, [data-e2e='modal-close-icon']:visible, [class*='login-mask'] [class*='close']:visible, [aria-label='关闭']:visible"
                        ).first
                        if await modal.count() > 0:
                            await modal.click(timeout=800)
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass

                    scroll_px = random.randint(800, 1150)
                    await page.mouse.wheel(0, scroll_px)
                    await page.wait_for_timeout(random.randint(250, 400))

                    if len(candidates) == prev_count:
                        stale_scrolls += 1
                        if stale_scrolls in (2, 3):
                            try:
                                await page.keyboard.press("PageDown")
                                await page.wait_for_timeout(250)
                            except Exception:
                                pass
                        if stale_scrolls >= 4:
                            break
                    else:
                        stale_scrolls = 0
                        prev_count = len(candidates)

                if not candidates:
                    raise SelectorChangedError(
                        "Trang Douyin chưa có liên kết video để đọc; có thể không có kết quả hoặc cấu trúc trang đã đổi."
                    )

                needs_detail: list[tuple[str, dict[str, str | None]]] = []
                for vid_id, cand in list(candidates.items()):
                    canonical = str(cand["link"])
                    candidate_cover = cand.get("cover")
                    title = cand.get("title")
                    if title:
                        card_likes = parse_count_text(str(cand.get("like_text"))) if cand.get("like_text") else None
                        if card_likes is None or card_likes >= filters.minimum_likes:
                            duration_sec = parse_duration_seconds(cand.get("duration_text"))
                            tags = extract_hashtags(str(title))
                            vr = VideoResult(
                                platform="douyin",
                                platform_video_id=vid_id,
                                url=cast(HttpUrl, canonical),
                                share_url=canonical,
                                title=str(title)[:500],
                                thumbnail_url=validate_image_url(str(candidate_cover)) if candidate_cover else None,
                                author_name=str(cand.get("author"))[:100] if cand.get("author") else None,
                                author_url=str(cand.get("author_link")) if cand.get("author_link") else None,
                                like_count=card_likes,
                                duration_seconds=duration_sec,
                                hashtags=tags,
                                is_mock=False,
                            )
                            results.append(vr)
                            if on_batch:
                                await on_batch([vr])
                            if len(results) >= limit:
                                break
                    else:
                        needs_detail.append((vid_id, cand))

                if len(results) < limit and needs_detail:
                    assert self.browser.context is not None
                    detail = await self.browser.context.new_page()
                    should_close_detail = True
                    try:
                        for vid_id, cand in needs_detail[: max(limit - len(results), 2)]:
                            link = str(cand["link"])
                            candidate_cover = cand.get("cover")
                            try:
                                await detail.goto(link, wait_until="domcontentloaded", timeout=6000)
                                await self.gate(detail, results)
                                try:
                                    await detail.wait_for_selector(
                                        "video, [data-e2e='video-desc'], .video-info-detail",
                                        timeout=1500,
                                    )
                                except Error:
                                    pass

                                try:
                                    await detail.locator("video").evaluate_all(
                                        "els => els.forEach(v => v.pause())"
                                    )
                                except Error:
                                    pass

                                actual_canonical = video_url(detail.url)
                                actual_id = actual_canonical.rsplit("/", 1)[1] if actual_canonical else None
                                effective_link = actual_canonical or detail.url
                                effective_id = actual_id or vid_id

                                raw_data = await detail.evaluate(DETAIL_SCRIPT)
                                try:
                                    result = parse_detail(
                                        raw_data,
                                        effective_link,
                                        candidate_cover=candidate_cover,
                                        share_url=effective_link,
                                        platform_video_id=effective_id,
                                    )
                                    if result:
                                        if (result.like_count or 0) >= filters.minimum_likes:
                                            results.append(result)
                                            if on_batch:
                                                await on_batch([result])
                                            if len(results) >= limit:
                                                break
                                except ValidationError as e:
                                    logger.warning(
                                        "Validation failed for Douyin candidate %s: %s",
                                        effective_id,
                                        e,
                                    )
                                    continue

                            except UserActionRequired:
                                should_close_detail = False
                                if hasattr(self.browser, "blocked_page"):
                                    self.browser.blocked_page = detail
                                if hasattr(self.browser, "search_page"):
                                    self.browser.search_page = getattr(self.browser, "page", None)
                                if hasattr(self.browser, "page"):
                                    self.browser.page = detail
                                raise
                            except (ProviderError, asyncio.CancelledError):
                                raise
                            except Exception:
                                pass
                    finally:
                        if should_close_detail:
                            if hasattr(self.browser, "search_page") and getattr(self.browser, "search_page", None):
                                self.browser.page = self.browser.search_page
                            if hasattr(self.browser, "blocked_page"):
                                self.browser.blocked_page = None
                            await detail.close()

                if not results:
                    raise SelectorChangedError(
                        "Chưa xác minh được video Douyin trong các bài đã mở. Không tạo kết quả thay thế."
                    )
                return results
            except BrowserTimeout:
                raise ProviderTimeoutError("Douyin phản hồi quá chậm. Thử lại sau.") from None
            except Error:
                raise NetworkError(
                    "Trình duyệt Douyin đã đóng hoặc mất kết nối. Mở lại từ Cài đặt hoặc Giao diện tìm kiếm."
                ) from None
