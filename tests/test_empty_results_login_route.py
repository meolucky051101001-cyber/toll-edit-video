"""Empty results page must route to manual re-login, not a dead-end error.

Douyin/Xiaohongshu search pages are client-rendered shells: when the session
is dead the tab spins forever with zero anchors and zero API responses while
the state classifier still reports "page_available". Failing that as
"structure changed" strands the user; raising waiting_for_login instead sends
them through the manual re-login flow, after which the search resumes.
"""
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import pytest

from backend.providers.base import SelectorChangedError, UserActionRequired
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters


def _empty_page(*, url: str):
    page = AsyncMock()
    page.url = url
    page.wait_for_selector = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.content = AsyncMock(return_value="<html></html>")
    page.is_closed = MagicMock(return_value=False)
    page.remove_listener = MagicMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    loc = MagicMock()
    loc.evaluate_all = AsyncMock(return_value=[])
    loc.count = AsyncMock(return_value=0)
    loc.first = MagicMock()
    loc.first.count = AsyncMock(return_value=0)
    loc.first.is_visible = AsyncMock(return_value=False)
    loc.filter = MagicMock(return_value=loc)
    page.locator = MagicMock(return_value=loc)
    return page


def _mock_browser(*, authenticated: bool):
    browser = MagicMock()
    browser.lock = AsyncMock()
    browser.base_url = "https://www.douyin.com"
    browser.is_currently_headless = True
    browser.diagnostics = {"is_authenticated": authenticated}
    browser.ensure_headless_session = AsyncMock()
    browser.release_blocked_page = AsyncMock()
    browser.inspect = AsyncMock(return_value="page_available")
    browser.elevate_to_headful = AsyncMock()
    return browser


@pytest.mark.anyio
async def test_douyin_empty_unauthenticated_page_routes_to_login():
    browser = _mock_browser(authenticated=False)
    page = _empty_page(url="")
    browser.ensure_open = AsyncMock(return_value=page)
    browser.page = page

    async def mock_goto(url, *args, **kwargs):
        page.url = url  # no API responses ever arrive: dead session shell

    page.goto = AsyncMock(side_effect=mock_goto)
    page.on = MagicMock()
    provider = DouyinProvider(browser=browser, auto_elevate=True)
    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=TimeoutError)):
        with pytest.raises(UserActionRequired) as exc_info:
            await provider.search("decor bàn học", 20, filters)
    assert exc_info.value.state == "waiting_for_login"
    browser.elevate_to_headful.assert_called_once_with(persist=False)


@pytest.mark.anyio
async def test_douyin_empty_authenticated_page_keeps_selector_error():
    browser = _mock_browser(authenticated=True)
    page = _empty_page(url="")
    browser.ensure_open = AsyncMock(return_value=page)
    browser.page = page

    async def mock_goto(url, *args, **kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=mock_goto)
    page.on = MagicMock()
    provider = DouyinProvider(browser=browser, auto_elevate=True)
    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")
    with patch("asyncio.wait_for", new=AsyncMock(side_effect=TimeoutError)):
        with pytest.raises(SelectorChangedError):
            await provider.search("decor bàn học", 20, filters)
    browser.elevate_to_headful.assert_not_called()


@pytest.mark.anyio
async def test_xhs_empty_unauthenticated_page_routes_to_login():
    browser = _mock_browser(authenticated=False)
    browser.base_url = "https://www.rednote.com"
    direct_url = (
        "https://www.rednote.com/search_result"
        f"?keyword={quote('decor bàn học')}&source=web_explore_feed&type=51"
    )
    page = _empty_page(url=direct_url)
    browser.ensure_open = AsyncMock(return_value=page)
    browser.page = page
    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(UserActionRequired) as exc_info:
        await provider.search("decor bàn học", 20, SearchFilters())
    assert exc_info.value.state == "waiting_for_login"
    browser.elevate_to_headful.assert_called_once_with(persist=False)


@pytest.mark.anyio
async def test_xhs_empty_authenticated_page_keeps_selector_error():
    browser = _mock_browser(authenticated=True)
    browser.base_url = "https://www.rednote.com"
    direct_url = (
        "https://www.rednote.com/search_result"
        f"?keyword={quote('decor bàn học')}&source=web_explore_feed&type=51"
    )
    page = _empty_page(url=direct_url)
    browser.ensure_open = AsyncMock(return_value=page)
    browser.page = page
    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    with pytest.raises(SelectorChangedError):
        await provider.search("decor bàn học", 20, SearchFilters())
    browser.elevate_to_headful.assert_not_called()
