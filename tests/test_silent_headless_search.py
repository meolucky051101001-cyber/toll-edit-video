import asyncio
from unittest.mock import AsyncMock, patch

from playwright.async_api import Page, async_playwright

from backend.core.config import Settings
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService


def test_config_search_headless_silent_default():
    cfg = Settings(openai_api_key="test-key")
    assert getattr(cfg, "search_headless_silent", False) is True


def test_browser_services_reset_headful_session(tmp_path):
    # Xiaohongshu
    xhs_browser = BrowserService(tmp_path / "xhs", headless=True)
    xhs_browser.force_headful_session = True
    xhs_browser.hybrid_headless = False
    xhs_browser.reset_headful_session()
    assert xhs_browser.force_headful_session is False
    assert xhs_browser.hybrid_headless is True

    # Douyin
    dy_browser = DouyinBrowserService(tmp_path / "dy", headless=True)
    dy_browser.force_headful_session = True
    dy_browser.hybrid_headless = False
    dy_browser.reset_headful_session()
    assert dy_browser.force_headful_session is False
    assert dy_browser.hybrid_headless is True


def test_xhs_provider_silent_search_passes_headless_override():
    async def scenario():
        class MockBrowser:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.ensure_open_kwargs = None
                self.base_url = "https://www.rednote.com"

            async def ensure_open(self, headless_override=None, initial_url=None):
                self.ensure_open_kwargs = {
                    "headless_override": headless_override,
                    "initial_url": initial_url,
                }
                mock_page = AsyncMock(spec=Page)
                mock_page.url = "https://www.rednote.com/search_result?keyword=test"
                mock_page.is_closed.return_value = False
                return mock_page

            async def inspect(self, page):
                return "page_available"

            def reset_headful_session(self):
                pass

        mock_browser = MockBrowser()
        provider = XiaohongshuProvider(mock_browser, auto_elevate=False)

        # Mock page items
        with patch.object(provider, "gate", AsyncMock()):
            # Mock require_search_results
            with patch("backend.providers.xiaohongshu.provider.require_search_results"):
                with patch.object(Page, "wait_for_selector", AsyncMock()):
                    try:
                        await provider.search("test", 1, SearchFilters())
                    except Exception:
                        pass
        assert mock_browser.ensure_open_kwargs is not None
        assert mock_browser.ensure_open_kwargs["headless_override"] is True

    asyncio.run(scenario())


def test_douyin_provider_silent_search_passes_headless_override():
    async def scenario():
        class MockBrowser:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.ensure_open_kwargs = None
                self.base_url = "https://www.douyin.com"

            async def ensure_open(self, initial_url=None, headless_override=None):
                self.ensure_open_kwargs = {
                    "headless_override": headless_override,
                    "initial_url": initial_url,
                }
                mock_page = AsyncMock(spec=Page)
                mock_page.url = "https://www.douyin.com/search/test?type=video"
                mock_page.is_closed.return_value = False
                return mock_page

            async def inspect(self, page):
                return "page_available"

            def reset_headful_session(self):
                pass

        mock_browser = MockBrowser()
        provider = DouyinProvider(mock_browser, auto_elevate=False)

        with patch.object(provider, "gate", AsyncMock()):
            try:
                await provider.search("test", 1, SearchFilters())
            except Exception:
                pass
        assert mock_browser.ensure_open_kwargs is not None
        assert mock_browser.ensure_open_kwargs["headless_override"] is True

    asyncio.run(scenario())


def test_douyin_provider_filters_live_stream_cards():
    async def scenario():
        class StubSession:
            def __init__(self, page, context, base_url):
                self.page = page
                self.context = context
                self.base_url = base_url
                self.lock = asyncio.Lock()

            async def ensure_open(self, initial_url=None, headless_override=None):
                return self.page

            async def inspect(self, page):
                return "page_available"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            async def handler(route):
                html = """
                <title>Douyin search</title>
                <ul>
                    <li class="card">
                        <div class="live-tag">直播中</div>
                        <a href="/video/1111111111111111111"></a>
                        <p>Live stream video</p>
                    </li>
                    <li class="card">
                        <a href="/video/2222222222222222222"></a>
                        <p>Real normal video</p>
                    </li>
                </ul>
                """
                await route.fulfill(status=200, body=html, content_type="text/html")

            await context.route("**/*", handler)
            page = await context.new_page()
            await page.goto("https://www.douyin.com/search/%E6%96%87%E5%85%B7?type=video")

            session = StubSession(page, context, "https://www.douyin.com")
            provider = DouyinProvider(session)
            results = await provider.search("文具", 5, SearchFilters())

            # Only video 2222222222222222222 should be included, live stream card must be filtered
            assert len(results) == 1
            assert results[0].platform_video_id == "2222222222222222222"
            assert "Real normal video" in results[0].title

            await browser.close()

    asyncio.run(scenario())
