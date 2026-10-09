import asyncio
import logging
import random
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

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
    apply_search_filters_and_sorting,
)
from backend.providers.xiaohongshu.parser import (
    note_url,
    parse_count_text,
    parse_detail,
    resolve_xhs_urls,
    validate_image_url,
)
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.browser_service import MESSAGES, BrowserService
from backend.services.media_parser import extract_hashtags, parse_duration_seconds

logger = logging.getLogger(__name__)


def query_card_terms(query: str) -> set[str]:
    """Extract semantic search terms from query without creating cross-boundary junk bigrams."""
    cjk_seqs = re.findall(r"[\u4e00-\u9fff]+", query)
    latin_words = [w.lower() for w in re.findall(r"[a-zA-Z0-9]+", query) if len(w) >= 2]
    terms: set[str] = set(latin_words)

    KNOWN_VOCAB = (
        "造景", "贴纸", "镊子", "咕卡", "手账", "手帐", "剥离器", "起边器", "揭贴器",
        "夹取", "工具", "开箱", "盲盒", "书桌", "改造", "布置", "文具", "桌面", "收纳",
        "穿搭", "测评", "试色", "打包", "电钻", "录音", "日常", "沉浸式", "治愈",
        "卧室", "房间", "室内", "家居", "家装", "装饰", "软装",
    )
    for seq in cjk_seqs:
        matched_any = False
        for vocab in KNOWN_VOCAB:
            if vocab in seq:
                terms.add(vocab)
                matched_any = True
        if not matched_any:
            if len(seq) == 2:
                terms.add(seq)
            elif len(seq) > 2 and len(seq) % 2 == 0:
                for i in range(0, len(seq), 2):
                    terms.add(seq[i : i + 2])
            else:
                for i in range(len(seq) - 1):
                    terms.add(seq[i : i + 2])
    return terms


def detail_card_priority(
    candidate: dict[str, object], terms: set[str], frequencies: Counter[str]
) -> tuple[int, int, int]:
    title = str(candidate.get("title") or "")
    matches = {term for term in terms if term in title}
    return (0 if matches else 1, min((frequencies[term] for term in matches), default=9999), -len(matches))


def require_search_results(url: str, query: str) -> None:
    current = urlsplit(url)
    if current.path.rstrip("/") != "/search_result" or not any(
        query.strip().casefold() == value.strip().casefold()
        for value in parse_qs(current.query).get("keyword", [])
    ):
        raise SelectorChangedError(
            "Trang hiện tại không phải kết quả đúng từ khóa. "
            "Tool dừng để không thu nhầm bài đề xuất. "
            "Hãy mở trang tìm kiếm trong Chrome riêng và kiểm tra đăng nhập."
        )

# Enhanced public DOM selectors based on yangsijie666/xiaohongshu-crawler
DETAIL_SCRIPT = """() => {
  const video = document.querySelector('.player-container video, video, .player-container video source');
  const meta = (name) => document.querySelector(`meta[property="${name}"],meta[name="${name}"]`)?.content || null;
  
  const titleEl = document.querySelector('#detail-title, .note-content .title, .title, h1');
  const descEl = document.querySelector('#detail-desc .note-text, #detail-desc, .note-content .desc, .desc, .content');
  
  const authorNameEl = document.querySelector('.author-container .username, .interaction-container .username, .author-wrapper .username, .interaction-container .author .name, .note-container .author .name, .author-wrapper .name, .author .name');
  const authorLinkEl = document.querySelector('.author-container a[href*="/user/profile/"], .interaction-container a[href*="/user/profile/"], .interaction-container .author a, .note-container .author a, .author-wrapper a, .author a');
  
  const likeEl = document.querySelector('.interact-container .like-wrapper .count, .engage-bar .like-wrapper .count, [class*="like-wrapper"] .count, .like-wrapper .count');
  const collectEl = document.querySelector('.interact-container .collect-wrapper .count, .engage-bar .collect-wrapper .count, [class*="collect-wrapper"] .count, .collect-wrapper .count');
  const commentEl = document.querySelector('.interact-container .chat-wrapper .count, .engage-bar .chat-wrapper .count, [class*="chat-wrapper"] .count, .chat-wrapper .count');
  
  const detailImg = document.querySelector('.swiper-slide img, .media-container img, .player-container img, .carousel-container img, .note-slider-img, [class*="cover"] img')?.src;
  const poster = video?.poster || document.querySelector('video')?.getAttribute('poster') || detailImg || meta('og:image');
  
  const has_video = Boolean(
    (video && (video.getBoundingClientRect().width > 0 || video.videoWidth > 0 || video.src)) ||
    document.querySelector('.player-container, video, [class*="player-container"]')
  );
  
  return {
    has_video: has_video,
    title: titleEl?.innerText?.trim() || meta('og:title'),
    description: descEl?.innerText?.trim() || meta('og:description') || meta('description'),
    poster: poster,
    duration: video && Number.isFinite(video.duration) && video.duration > 0 ? video.duration : null,
    author_name: authorNameEl?.innerText?.trim() || null,
    author_url: authorLinkEl?.href || null,
    like_text: likeEl?.innerText?.trim() || null,
    collect_text: collectEl?.innerText?.trim() || null,
    comment_text: commentEl?.innerText?.trim() || null,
  };
}"""


class XiaohongshuProvider(SearchProvider):
    capture_origin = "live_provider"

    def __init__(self, browser: BrowserService, auto_elevate: bool = False):
        self.browser = browser
        self.auto_elevate = auto_elevate
        self.attempted_queries: set[str] = set()
        self.detail_cache: dict[str, VideoResult] = {}

    async def gate(self, page: Page, partial: list[VideoResult]) -> None:
        state = await self.browser.inspect(page)
        if state in {"login_required", "verification_required", "restricted"}:
            if state in {"login_required", "verification_required"}:
                if self.auto_elevate and getattr(self.browser, "is_currently_headless", False):
                    try:
                        await self.browser.elevate_to_headful()
                    except Exception:
                        pass
            raise UserActionRequired(
                "waiting_for_login" if state == "login_required" else "waiting_for_user",
                MESSAGES[state],
                partial,
            )
        if state == "closed":
            raise UserActionRequired("waiting_for_user", MESSAGES[state], partial)
        if state == "network_error":
            raise NetworkError(MESSAGES[state])

    async def search(
        self,
        query: str,
        limit: int,
        filters: SearchFilters,
        on_batch: Callable[[list[VideoResult]], Awaitable[None]] | None = None,
    ) -> list[VideoResult]:
        if hasattr(self.browser, "release_blocked_page"):
            await self.browser.release_blocked_page()
        if not self.auto_elevate:
            if hasattr(self.browser, "ensure_headless_session"):
                await self.browser.ensure_headless_session()
            elif hasattr(self.browser, "reset_headful_session"):
                self.browser.reset_headful_session()
        async with self.browser.lock:
            results: list[VideoResult] = []
            try:
                base = getattr(self.browser, "base_url", "https://www.rednote.com")
                direct_url = (
                    f"{base}/search_result?keyword="
                    f"{quote(query)}&source=web_explore_feed&type=51"
                )
                try:
                    page = await self.browser.ensure_open(
                        headless_override=True if not self.auto_elevate else None,
                        initial_url=direct_url,
                    )
                except TypeError:
                    try:
                        page = await self.browser.ensure_open(
                            headless_override=True if not self.auto_elevate else None,
                        )
                    except TypeError:
                        page = await self.browser.ensure_open()
                # Pre-check gate only if already restricted or verification required
                initial_state = await self.browser.inspect(page)
                if initial_state in {"verification_required", "restricted"}:
                    await self.gate(page, results)
                base = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}" if page.url and urlsplit(page.url).netloc else getattr(self.browser, "base_url", "https://www.rednote.com")
                direct_url = f"{base}/search_result?keyword={quote(query)}&source=web_explore_feed&type=51"
                current = urlsplit(page.url)
                on_current_results = False
                if current.path.rstrip("/") == "/search_result":
                    kw_params = [
                        unquote(str(val)).strip().lower()
                        for val in parse_qs(current.query).get("keyword", [])
                    ]
                    if query.strip().lower() in kw_params:
                        on_current_results = True

                if not on_current_results:
                    await page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_selector(
                            'section.note-item, div.note-item, .search-result-item, [class*="note-item"], a[href*="/explore/"]',
                            timeout=6000,
                        )
                    except Error:
                        await page.wait_for_timeout(1500)
                    # Auto dismiss guest modal / prompt if opened on direct navigation
                    try:
                        await page.keyboard.press("Escape")
                        close_btn = page.locator(
                            ".icon-btn-wrapper.close-button:visible, .close-button:visible, .close:visible, [aria-label='关闭']:visible, [aria-label='Close']:visible"
                        ).first
                        if await close_btn.count() > 0:
                            await close_btn.click(timeout=800)
                        await page.wait_for_timeout(400)
                    except Exception:
                        pass
                    await self.gate(page, results)
                    current = urlsplit(page.url)
                    on_current_results = False
                    if current.path.rstrip("/") == "/search_result":
                        kw_params = [
                            unquote(str(val)).strip().lower()
                            for val in parse_qs(current.query).get("keyword", [])
                        ]
                        if query.strip().lower() in kw_params:
                            on_current_results = True

                if not on_current_results:
                    if current.path.rstrip("/") != "/explore" and current.path.rstrip("/") != "":
                        await page.goto(f"{base}/explore", wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(1500)
                        await self.gate(page, results)
                    # XHS may render duplicate hidden inputs. Operate on the first visible one.
                    field = page.locator("input#search-input:visible").first
                    if not await field.is_visible():
                        raise SelectorChangedError(
                            "Không tìm thấy ô tìm kiếm Xiaohongshu trên trang. Cần kiểm tra lại cấu trúc trang."
                        )
                    await field.click(force=True)
                    await field.press("Control+A")
                    await field.fill(query)
                    self.attempted_queries.add(query)
                    await field.press("Enter")
                    await page.wait_for_timeout(2500)
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                    await self.gate(page, results)
                    current = urlsplit(page.url)
                    if not (
                        (current.path.rstrip("/") == "/search_result" and any(
                            query.strip().casefold() == str(val).strip().casefold()
                            for val in parse_qs(current.query).get("keyword", [])
                        ))
                    ):
                        raise SelectorChangedError(
                            "Chưa xác minh trang kết quả đúng từ khóa. Tool dừng để không lưu nhầm video đề xuất."
                        )

                require_search_results(page.url, query)

                # Click video filter tab if present
                on_video_tab = False
                video_tab = page.locator(
                    "button, div[role='tab'], [role='tab'], .tab, a"
                ).filter(has_text=re.compile(r"^(Videos?|视频)$", re.IGNORECASE)).first
                if await video_tab.count() > 0 and await video_tab.is_visible():
                    try:
                        await video_tab.click(timeout=2000)
                        await page.wait_for_timeout(600)
                        on_video_tab = True
                    except Error:
                        pass

                candidates: dict[str, dict[str, object]] = {}
                max_scrolls = min(max(5, int(limit * 0.8)), 30)
                target_candidates = min(max(limit * 2, 15), 250)
                prev_count = 0
                stale_scrolls = 0

                for _ in range(max_scrolls):
                    # Auto dismiss any guest popup that might have triggered on scroll
                    try:
                        guest_modal = page.locator(
                            ".login-container:visible, .login-modal:visible, .reds-modal.login-modal:visible"
                        )
                        if await guest_modal.count() > 0:
                            await page.keyboard.press("Escape")
                            close_btn = page.locator(
                                ".icon-btn-wrapper.close-button:visible, .close-button:visible, .close:visible, [aria-label='关闭']:visible, [aria-label='Close']:visible"
                            ).first
                            if await close_btn.count() > 0:
                                await close_btn.click(timeout=800)
                    except Exception:
                        pass
                    await self.gate(page, results)
                    require_search_results(page.url, query)
                    items = await page.locator(
                        'section.note-item, div.note-item, .search-result-item, [class*="note-item"], [class*="NoteItem"], a[href*="/explore/"], a[href*="/search_result/"], a[href*="/discovery/item/"]'
                    ).evaluate_all(
                        """(els, isVideoTab) => {
                            const seen = new Set();
                            const res = [];
                            for (const el of els) {
                                let card = el.closest('section.note-item, div.note-item, .search-result-item, [class*="note-item"], [class*="NoteItem"]') || el;
                                // Ignore injected recommendation boxes / unrelated suggestions
                                if (card.closest('.recommend-box, .guess-you-like, .related-search, [class*="recommend"], [class*="guess"], [class*="related-query"], [class*="advertise"]')) {
                                    continue;
                                }
                                if (card.querySelector('[class*="live"], .live-tag, [data-e2e*="live"]') || card.innerText.includes('直播中')) {
                                    continue;
                                }
                                // Verify this card doesn't wrap multiple notes. If it does, narrow down to the element holding only `el`
                                const noteAnchors = Array.from(card.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"], a[href*="/discovery/item/"]'));
                                const distinctNoteIds = new Set(
                                    noteAnchors.map(a => {
                                        const m = (a.getAttribute('href') || a.href || '').match(/(?:explore|search_result|discovery\\/item)\\/([0-9a-fA-F]{24})/);
                                        return m ? m[1] : null;
                                    }).filter(Boolean)
                                );
                                if (distinctNoteIds.size > 1) {
                                    let cur = el;
                                    while (cur && cur.parentElement && cur.parentElement !== card) {
                                        const pAnchors = cur.parentElement.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"], a[href*="/discovery/item/"]');
                                        const pIds = new Set(Array.from(pAnchors).map(a => {
                                            const m = (a.getAttribute('href') || a.href || '').match(/(?:explore|search_result|discovery\\/item)\\/([0-9a-fA-F]{24})/);
                                            return m ? m[1] : null;
                                        }).filter(Boolean));
                                        if (pIds.size > 1) break;
                                        cur = cur.parentElement;
                                    }
                                    card = cur;
                                }

                                if (seen.has(card)) continue;
                                seen.add(card);
                                
                                const allAnchors = Array.from(card.querySelectorAll('a'));
                                if (el.tagName === 'A') allAnchors.unshift(el);
                                
                                let note_id = null;
                                let token = null;
                                let coverHref = '';
                                
                                // Pass 1: Prioritize anchor that contains xsec_token (typically a.cover)
                                for (const a of allAnchors) {
                                    const rawHref = a.getAttribute('href') || a.href || '';
                                    const tm = rawHref.match(/xsec_token=([^&]+)/);
                                    if (tm && !token) {
                                        token = tm[1];
                                        coverHref = rawHref;
                                    }
                                    const m = rawHref.match(/(?:explore|search_result|discovery\\/item)\\/([0-9a-fA-F]{24})/);
                                    if (m && !note_id) {
                                        note_id = m[1];
                                    }
                                }
                                
                                // Pass 2: Check dataset or custom attributes on card
                                if (!token) {
                                    const dataToken = card.getAttribute('data-xsec-token') || card.getAttribute('data-token') || card.dataset?.xsecToken || card.dataset?.token;
                                    if (dataToken) token = dataToken;
                                }
                                
                                // Pass 3: If note_id not yet resolved, check other anchors
                                if (!note_id) {
                                    for (const a of allAnchors) {
                                        const rawHref = a.getAttribute('href') || a.href || '';
                                        const m = rawHref.match(/(?:explore|search_result|discovery\\/item)\\/([0-9a-fA-F]{24})/);
                                        if (m) {
                                            note_id = m[1];
                                            if (!coverHref) coverHref = rawHref;
                                            break;
                                        }
                                    }
                                }
                                if (!note_id) {
                                    const dataId = card.getAttribute('data-id') || card.getAttribute('data-note-id') || card.dataset?.id || card.dataset?.noteId;
                                    if (dataId && /^[0-9a-fA-F]{24}$/.test(dataId)) {
                                        note_id = dataId;
                                    }
                                }
                                
                                const img = card.querySelector('a.cover img, img:not(.author-avatar)') || card.querySelector('img');
                                const cover = img ? (img.getAttribute('data-src') || img.currentSrc || img.src) : null;
                                
                                const durationEl = Array.from(card.querySelectorAll('span, div')).find(s => /^\\d{1,2}:\\d{2}$/.test(s.innerText ? s.innerText.trim() : ''));
                                const durationText = durationEl ? durationEl.innerText.trim() : null;
                                const has_video_indicator = Boolean(
                                    durationText ||
                                    card.querySelector('.video-icon, .type-video, [class*="play-icon"], [class*="play-btn"], video, svg[class*="play"], [class*="duration"], [class*="time"]')
                                );
                                const is_video = Boolean(has_video_indicator);
                                
                                const title = card.querySelector('.title, [class*="title"], .desc, [class*="desc"]')?.innerText?.trim() || null;
                                const author = card.querySelector('.author, .author-wrapper, .name, [class*="author"] .name, .author .name')?.innerText?.trim() || null;
                                const authorLink = card.querySelector('a[href*="/user/profile/"]')?.href || null;
                                const likeText = card.querySelector('.like-wrapper .count, [class*="like"] .count, .like-wrapper')?.innerText?.trim() || null;
                                
                                const tagEls = Array.from(card.querySelectorAll('.tag, [class*="tag"], a[href*="/tag/"], a[href*="/search_result"]'));
                                const cardTags = tagEls.map(t => (t.innerText || '').trim().replace(/^#/, '')).filter(t => t.length > 0 && !t.includes(' '));
                                const rawCardTags = (card.innerText || '').match(/#([^\\s#]+)/g) || [];
                                for (const rh of rawCardTags) {
                                    const cleanRh = rh.replace(/^#/, '').trim();
                                    if (cleanRh && !cardTags.includes(cleanRh)) {
                                        cardTags.push(cleanRh);
                                    }
                                }

                                if (coverHref || note_id || title) {
                                    res.push({
                                        href: coverHref,
                                        note_id: note_id,
                                        cover: cover,
                                        is_video: is_video,
                                        token: token,
                                        title: title,
                                        author: author,
                                        author_link: authorLink,
                                        like_text: likeText,
                                        duration_text: durationText,
                                        hashtags: cardTags,
                                    });
                                }
                            }
                            return res;
                        }""",
                        on_video_tab,
                    )
                    for item in items:
                        note_id = item.get("note_id")
                        link = str(item.get("href") or "")
                        if not note_id:
                            canonical = note_url(link)
                            if canonical:
                                note_id = canonical.rsplit("/", 1)[1]
                        if not note_id:
                            continue

                        token = item.get("token")
                        if token:
                            target_link = f"https://www.rednote.com/discovery/item/{note_id}?xsec_token={token}&xsec_source=pc_search"
                        else:
                            target_link = f"{base}/explore/{note_id}"
                        canonical = f"{base}/explore/{note_id}"

                        cover = item.get("cover")
                        is_video = bool(item.get("is_video"))

                        if (
                            note_id not in candidates
                            or token
                            or (cover and not candidates[note_id].get("cover"))
                            or (is_video and not candidates[note_id].get("is_video"))
                        ):
                            candidates[note_id] = {
                                "link": target_link,
                                "canonical": canonical,
                                "note_id": note_id,
                                "token": token,
                                "cover": cover,
                                "is_video": is_video,
                                "title": item.get("title"),
                                "author": item.get("author"),
                                "author_link": item.get("author_link"),
                                "like_text": item.get("like_text"),
                                "duration_text": item.get("duration_text"),
                                "hashtags": item.get("hashtags") or [],
                            }
                    video_count = sum(1 for c in candidates.values() if c.get("is_video") and c.get("title"))
                    if video_count >= limit or len(candidates) >= target_candidates:
                        break

                    scroll_px = random.randint(800, 1150)
                    await page.mouse.wheel(0, scroll_px)
                    await page.wait_for_timeout(random.randint(700, 1100))

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
                        "Trang chưa có liên kết bài để đọc; có thể không có kết quả hoặc cấu trúc trang đã đổi."
                    )

                ordered = sorted(
                    candidates.values(),
                    key=lambda c: (
                        0 if c.get("token") else 1,
                        0 if c.get("is_video") else 1,
                    ),
                )
                needs_detail: list[dict[str, object]] = []
                query_terms = query_card_terms(query)
                term_frequencies = Counter(
                    term
                    for cand in ordered
                    if cand.get("is_video")
                    for term in query_terms
                    if term in str(cand.get("title") or "")
                )

                # Cards with partial query evidence need their actual note description.
                for cand in ordered:
                    note_id = str(cand["note_id"])
                    canonical = str(cand.get("canonical") or f"{base}/explore/{note_id}")
                    link = str(cand.get("link") or canonical)
                    candidate_cover = cand.get("cover")
                    title = cand.get("title")
                    is_card_video = bool(cand.get("is_video"))
                    matched_terms = {term for term in query_terms if term in str(title or "")}
                    if title and is_card_video and len(query_terms) >= 2:
                        if not matched_terms:
                            continue
                        has_full_intent = (
                            len(matched_terms) >= len(query_terms)
                            or (
                                any(t in str(title) for t in ("镊子", "夹取", "剥离器", "起边器", "揭贴器"))
                                and any(t in str(title) for t in ("贴纸", "咕卡", "手账", "手帐"))
                            )
                        )
                        if not has_full_intent:
                            needs_detail.append(cand)
                            continue
                    if title and is_card_video:
                        card_likes = parse_count_text(str(cand.get("like_text"))) if cand.get("like_text") else None
                        if card_likes is None or card_likes >= filters.minimum_likes:
                            duration_raw = cand.get("duration_text")
                            duration_sec = parse_duration_seconds(str(duration_raw) if duration_raw is not None else None)
                            tags = extract_hashtags(str(title))
                            raw_cand_tags = cand.get("hashtags")
                            cand_tags = raw_cand_tags if isinstance(raw_cand_tags, list) else []
                            for ct in cand_tags:
                                clean_ct = str(ct).strip().lstrip("#")
                                if clean_ct and clean_ct not in tags:
                                    tags.append(clean_ct)
                            vr = VideoResult(
                                platform="xiaohongshu",
                                platform_video_id=note_id,
                                url=cast(HttpUrl, canonical),
                                share_url=link,
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
                    elif not title:
                        # Card without title (e.g. synthetic test DOM) needs detail inspection
                        needs_detail.append(cand)
                    # Photo cards with title but is_card_video=False are photo notes, not videos

                # Inspect a bounded number of promising cards; a long search must not
                # turn into hundreds of detail navigations or exhaust the job timeout.
                needs_detail.sort(key=lambda cand: detail_card_priority(cand, query_terms, term_frequencies))
                remaining_needed = max(0, limit - len(results))
                detail_budget = min(len(needs_detail), max(6, remaining_needed * 3), 25)
                if len(results) < limit and needs_detail:
                    assert self.browser.context is not None
                    detail = await self.browser.context.new_page()
                    should_close_detail = True
                    try:
                        for cand in needs_detail[:detail_budget]:
                            if len(results) >= limit:
                                break
                            link = str(cand["link"])
                            canonical = str(cand.get("canonical") or link)
                            note_id = str(cand.get("note_id") or canonical.rsplit("/", 1)[1])
                            candidate_cover = cand.get("cover")
                            cached = self.detail_cache.get(note_id)
                            if cached is not None:
                                if (cached.like_count or 0) >= filters.minimum_likes:
                                    results.append(cached)
                                    if on_batch:
                                        await on_batch([cached])
                                    if len(results) >= limit:
                                        break
                                continue
                            try:
                                await detail.goto(link, wait_until="domcontentloaded", timeout=6000)
                                await self.gate(detail, results)
                                try:
                                    await detail.wait_for_selector(
                                        "#noteContainer, #detail-title, .note-content, .player-container, video",
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

                                try:
                                    canonical_url, effective_share_url, effective_id = resolve_xhs_urls(
                                        detail.url, link
                                    )
                                except ValueError:
                                    continue
                                # Redirects to another note or back to recommendations
                                # cannot supply metadata for this search card.
                                if effective_id != note_id or note_url(detail.url) is None:
                                    continue

                                raw_data = await detail.evaluate(DETAIL_SCRIPT)
                                cover_str = str(candidate_cover) if candidate_cover else None
                                try:
                                    result = parse_detail(
                                        raw_data,
                                        canonical_url,
                                        candidate_cover=cover_str,
                                        share_url=effective_share_url,
                                        platform_video_id=effective_id,
                                    )
                                    if result:
                                        self.detail_cache[note_id] = result
                                        if (result.like_count or 0) >= filters.minimum_likes:
                                            results.append(result)
                                            if on_batch:
                                                await on_batch([result])
                                            if len(results) >= limit:
                                                break
                                except ValidationError as e:
                                    logger.warning(
                                        "Validation failed for Xiaohongshu candidate %s: %s",
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
                        "Chưa xác minh được video trong các bài đã mở. Không tạo kết quả thay thế."
                    )
                return apply_search_filters_and_sorting(results, filters=filters, limit=limit)
            except BrowserTimeout:
                raise ProviderTimeoutError("Xiaohongshu phản hồi quá chậm. Thử lại sau.") from None
            except Error:
                raise NetworkError(
                    "Trình duyệt Xiaohongshu đã đóng hoặc mất kết nối. Mở lại từ Cài đặt."
                ) from None

    async def close(self) -> None:
        if hasattr(self.browser, "release_blocked_page"):
            await self.browser.release_blocked_page()

