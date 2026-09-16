import asyncio
from unittest.mock import patch

import pytest
from playwright.async_api import Page, async_playwright
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models import SearchJob, Video
from backend.providers.base import SearchFilters, SelectorChangedError, UserActionRequired
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import VideoResult
from backend.services import translator
from backend.services.search_service import SearchService


# ---------------------------------------------------------------------------
# F1 (P1): Query Translation and Disambiguation
# ---------------------------------------------------------------------------
def test_f1_translation_disambiguation():
    with patch.object(translator, "fast_online_translate", return_value=None):
        # 1. "nuôi mèo" -> Must produce pet care, never orphan modifiers ("沉浸式", "少女心")
        plan_cat = translator.plan_query("nuôi mèo")
        assert any("养猫" in q for q in plan_cat.tiered_queries), f"Expected 养猫 in {plan_cat.tiered_queries}"
        assert not any(q in ("沉浸式", "少女心") for q in plan_cat.tiered_queries)

        # 2. "sửa xe máy" -> Must produce vehicle repair, never orphan modifiers
        plan_moto = translator.plan_query("sửa xe máy")
        assert any(
            any(k in q for k in ["修车", "机车维修", "修摩托车"]) for q in plan_moto.tiered_queries
        ), f"Expected repair term in {plan_moto.tiered_queries}"
        assert not any(q in ("沉浸式", "少女心") for q in plan_moto.tiered_queries)

        # 3. "unbox đồng hồ" -> Watch unboxing, attribute 'dong' (winter) must NOT trigger
        plan_watch = translator.plan_query("unbox đồng hồ")
        assert "dong" not in plan_watch.mandatory_attributes, f"Unexpected 'dong' attribute in {plan_watch.mandatory_attributes}"
        assert not any("冬季" in q for q in plan_watch.tiered_queries), f"Found 冬季 in watch query: {plan_watch.tiered_queries}"
        assert any(
            any(k in q for k in ["手表", "腕表"]) for q in plan_watch.tiered_queries
        ), f"Expected watch term in {plan_watch.tiered_queries}"

        # 4. "phối đồ mùa đông giá rẻ" -> Multi-attribute AND constraint: must have BOTH winter and budget
        plan_winter_budget = translator.plan_query("phối đồ mùa đông giá rẻ")
        top_queries = plan_winter_budget.tiered_queries[:3]
        for q in top_queries:
            has_winter = any(w in q for w in ["冬", "冬季", "冬日"])
            has_budget = any(b in q for b in ["平价", "百元", "学生党", "平价好物"])
            assert has_winter and has_budget, f"Query '{q}' failed multi-attribute AND constraint (winter AND budget)"

        # 5. Unmapped query fallback -> Preserves raw query, no orphan modifiers
        plan_unmapped = translator.plan_query("chế tạo tên lửa")
        assert plan_unmapped.tiered_queries == ["chế tạo tên lửa"]


# ---------------------------------------------------------------------------
# F2 (P1): XHS Leaf Card Isolation (Prevent Photo A inheriting Video B's icon)
# ---------------------------------------------------------------------------
def test_f2_xhs_leaf_card_isolation():
    async def scenario():
        class StubSession:
            def __init__(self, page, context, base_url):
                self.page = page
                self.context = context
                self.base_url = base_url
                self.lock = asyncio.Lock()

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                return "page_available"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            id_a = "a" * 24
            id_b = "b" * 24

            async def handler(route):
                # Container .note-list wraps both Photo A and Video B
                html = '<title>文具</title><div class="note-list">'
                html += f'<section class="note-item"><a href="/explore/{id_a}"></a><span class="title">PHOTO A</span></section>'
                html += f'<section class="note-item"><a href="/explore/{id_b}"></a><span class="title">VIDEO B</span><span class="video-icon"></span></section></div>'
                await route.fulfill(status=200, body=html, content_type="text/html")

            await context.route("**/*", handler)
            page = await context.new_page()

            async def no_wait(self, timeout):
                return None

            with patch.object(Page, "wait_for_timeout", no_wait):
                await page.goto("https://www.xiaohongshu.com/search_result?keyword=%E6%96%87%E5%85%B7")
                provider = XiaohongshuProvider(StubSession(page, context, "https://www.xiaohongshu.com"))
                results = await provider.search("文具", 1, SearchFilters())

            assert len(results) == 1
            # Must strictly return Video B and NOT Photo A!
            assert results[0].platform_video_id == id_b
            assert results[0].title == "VIDEO B"

            await browser.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F3 (P1): Detail Redirect ID Consistency (Douyin & XHS)
# ---------------------------------------------------------------------------
def test_f3_detail_redirect_id_consistency():
    async def scenario():
        class StubSession:
            def __init__(self, page, context, base_url):
                self.page = page
                self.context = context
                self.base_url = base_url
                self.lock = asyncio.Lock()
                self.search_page = page
                self.blocked_page = None

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                return "page_available"

            def get_active_page(self):
                return self.blocked_page or self.page

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            id_a = "1234567890123456789"
            id_b = "2234567890123456789"

            async def handler(route):
                url = route.request.url
                if "/search/" in url:
                    # Search returns a card with ID A, without title (triggers detail inspect)
                    html = f'<title>文具</title><div class="search-result-card"><a href="https://www.douyin.com/video/{id_a}"></a></div>'
                    return await route.fulfill(status=200, body=html, content_type="text/html")
                elif f"/video/{id_a}" in url:
                    # Detail page for ID A redirects to ID B
                    return await route.fulfill(
                        status=200,
                        body=f'<script>location.replace("https://www.douyin.com/video/{id_b}")</script>',
                        content_type="text/html",
                    )
                elif f"/video/{id_b}" in url:
                    html = '<html><head><title>Video B Detail</title></head><body><h1 data-e2e="video-desc">Actual Title B</h1><video src="http://example.invalid/vid.mp4"></video></body></html>'
                    return await route.fulfill(status=200, body=html, content_type="text/html")
                await route.fulfill(status=200, body="OK", content_type="text/plain")

            await context.route("**/*", handler)
            page = await context.new_page()

            async def no_wait(self, timeout):
                return None

            with patch.object(Page, "wait_for_timeout", no_wait):
                await page.goto("https://www.douyin.com/search/%E6%96%87%E5%85%B7?type=video")
                session = StubSession(page, context, "https://www.douyin.com")
                provider = DouyinProvider(session)
                results = await provider.search("文具", 1, SearchFilters())

            assert len(results) == 1
            # Metadata B must be saved under ID B, NOT ID A!
            assert results[0].platform_video_id == id_b
            assert id_b in str(results[0].url)
            assert results[0].title == "Actual Title B"

            await browser.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F4 (P1): Detail CAPTCHA Target & Lifecycle Handling
# ---------------------------------------------------------------------------
def test_f4_detail_captcha_targeting():
    async def scenario():
        class StubSession:
            def __init__(self, page, context, base_url):
                self.page = page
                self.context = context
                self.base_url = base_url
                self.lock = asyncio.Lock()
                self.search_page = page
                self.blocked_page = None

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                content = await page.content()
                if "安全验证" in content or "CAPTCHA" in content:
                    return "verification_required"
                return "page_available"

            def get_active_page(self):
                return self.blocked_page or self.page

            async def release_blocked_page(self):
                if self.blocked_page is not None and not self.blocked_page.is_closed():
                    try:
                        await self.blocked_page.close()
                    except Exception:
                        pass
                self.blocked_page = None
                if self.search_page is not None and not self.search_page.is_closed():
                    self.page = self.search_page

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            id_a = "1234567890123456789"

            async def handler(route):
                url = route.request.url
                if "/search/" in url:
                    html = f'<title>文具</title><div class="search-result-card"><a href="https://www.douyin.com/video/{id_a}"></a></div>'
                    return await route.fulfill(status=200, body=html, content_type="text/html")
                elif f"/video/{id_a}" in url:
                    html = '<html><head><title>Captcha Verification</title></head><body>安全验证 拖动滑块 CAPTCHA</body></html>'
                    return await route.fulfill(status=200, body=html, content_type="text/html")
                await route.fulfill(status=200, body="OK", content_type="text/plain")

            await context.route("**/*", handler)
            page = await context.new_page()

            async def no_wait(self, timeout):
                return None

            with patch.object(Page, "wait_for_timeout", no_wait):
                await page.goto("https://www.douyin.com/search/%E6%96%87%E5%85%B7?type=video")
                session = StubSession(page, context, "https://www.douyin.com")
                provider = DouyinProvider(session)

                with pytest.raises(UserActionRequired):
                    await provider.search("文具", 1, SearchFilters())

            # Verification: active page and session.page must target the blocked detail page!
            assert session.blocked_page is not None
            assert not session.blocked_page.is_closed()
            assert session.page == session.blocked_page
            assert session.get_active_page() == session.blocked_page
            assert f"/video/{id_a}" in session.page.url

            # Release blocked page: detail closed and search page restored
            await session.release_blocked_page()
            assert session.blocked_page is None
            assert session.page == session.search_page
            assert not session.search_page.is_closed()

            await browser.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F5 (P1): Douyin Unrelated Search Redirect Strictly Rejected
# ---------------------------------------------------------------------------
def test_f5_douyin_unrelated_search_redirect_rejected():
    async def scenario():
        class StubSession:
            def __init__(self, page, context, base_url):
                self.page = page
                self.context = context
                self.base_url = base_url
                self.lock = asyncio.Lock()

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                return "page_available"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            async def handler(route):
                url = route.request.url
                if "%E6%96%87%E5%85%B7" in url or "文具" in url:
                    # Douyin redirects search query to an unrelated keyword
                    return await route.fulfill(
                        status=302,
                        headers={"location": "https://www.douyin.com/search/football?type=video"},
                    )
                elif "football" in url:
                    html = '<title>football - 抖音</title><div class="search-result-card"><a href="/video/1234567890123456789"></a><p>Football video</p></div>'
                    return await route.fulfill(status=200, body=html, content_type="text/html")
                await route.fulfill(status=200, body="OK", content_type="text/plain")

            await context.route("**/*", handler)
            page = await context.new_page()

            async def no_wait(self, timeout):
                return None

            with patch.object(Page, "wait_for_timeout", no_wait):
                await page.goto("https://www.douyin.com/")
                session = StubSession(page, context, "https://www.douyin.com")
                provider = DouyinProvider(session)

                with pytest.raises(SelectorChangedError) as exc:
                    await provider.search("文具", 1, SearchFilters())
                assert "chuyển hướng" in str(exc.value)

            await browser.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F6 (P2): Streaming Checkpoint & Interrupted Status Recovery
# ---------------------------------------------------------------------------
def test_f6_streaming_checkpoint_and_interrupted_recovery():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    service = SearchService(sessions)

    # 1. Test recover_interrupted
    with sessions() as db:
        j_interrupted = SearchJob(
            original_query="đồ cute",
            platforms=["douyin"],
            requested_limit=10,
            processed_count=4,
            status="searching",
            is_mock=True,
        )
        j_completed = SearchJob(
            original_query="đồ cute",
            platforms=["douyin"],
            requested_limit=10,
            processed_count=10,
            status="searching",
            is_mock=True,
        )
        j_failed = SearchJob(
            original_query="đồ cute",
            platforms=["douyin"],
            requested_limit=10,
            processed_count=0,
            status="searching",
            is_mock=True,
        )
        db.add_all([j_interrupted, j_completed, j_failed])
        db.commit()
        id_int, id_comp, id_fail = j_interrupted.id, j_completed.id, j_failed.id

    service.recover_interrupted()

    with sessions() as db:
        job_int = db.get(SearchJob, id_int)
        assert job_int is not None
        assert job_int.status == "interrupted"
        assert "4/10" in (job_int.error_message or "")

        job_comp = db.get(SearchJob, id_comp)
        assert job_comp is not None
        assert job_comp.status == "completed"

        job_fail = db.get(SearchJob, id_fail)
        assert job_fail is not None
        assert job_fail.status == "failed"

    # 2. Test streaming on_batch persisting
    with sessions() as db:
        job_stream = SearchJob(
            original_query="văn phòng phẩm",
            platforms=["douyin"],
            requested_limit=5,
            is_mock=True,
        )
        db.add(job_stream)
        db.commit()
        stream_id = job_stream.id

    class StreamingMockProvider:
        async def search(self, query, limit, filters, on_batch=None):
            items = [
                VideoResult(
                    platform="douyin",
                    platform_video_id=f"stream-{i}",
                    url=f"https://www.douyin.com/video/741234567890123456{i}",
                    title=f"Stream video {i}",
                )
                for i in range(limit)
            ]
            if on_batch:
                for item in items:
                    await on_batch([item])
            return items

        async def close(self):
            pass

    async def run_stream():
        async def on_batch(batch):
            await service.persist_results(stream_id, "douyin", "văn phòng phẩm", batch, 5)

        provider = StreamingMockProvider()
        await provider.search("văn phòng phẩm", 3, SearchFilters(), on_batch=on_batch)

    asyncio.run(run_stream())

    with sessions() as db:
        saved_vids = db.scalars(select(Video).where(Video.search_job_id == stream_id)).all()
        assert len(saved_vids) == 3
        job_after = db.get(SearchJob, stream_id)
        assert job_after is not None
        assert job_after.processed_count == 3

    engine.dispose()
