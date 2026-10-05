from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error

from backend.services.browser_service import BrowserService


def test_search_url_uses_saved_session_domain_without_losing_keyword(tmp_path):
    service = BrowserService(profile=tmp_path)
    service.base_url = "https://www.rednote.com"
    assert service.session_navigation_url("https://www.xiaohongshu.com/search_result?keyword=abc&type=51") == "https://www.rednote.com/search_result?keyword=abc&type=51"
    assert service.session_navigation_url("https://example.invalid/") == "https://www.rednote.com/explore"


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [Error("net::ERR_HTTP_RESPONSE_CODE_FAILURE"), Mock(status=503)])
async def test_http_failure_preserves_session_cookies_and_tries_other_domain(tmp_path, failure):
    service = BrowserService(profile=tmp_path)
    service.context = Mock()
    service.context.clear_cookies = AsyncMock()
    first_page = Mock()
    first_page.url = "chrome-error://chromewebdata/"
    first_page.goto = AsyncMock(side_effect=failure) if isinstance(failure, Error) else AsyncMock(return_value=failure)
    first_page.close = AsyncMock()
    alternate_page = Mock()
    alternate_page.url = "https://www.rednote.com/search_result?keyword=abc"
    alternate_page.goto = AsyncMock(return_value=Mock(status=200))
    service.page = first_page
    service.context.new_page = AsyncMock(return_value=alternate_page)
    await service.navigate_preserving_session("https://www.xiaohongshu.com/search_result?keyword=abc")
    assert first_page.goto.await_count == 1
    first_page.close.assert_awaited_once()
    assert alternate_page.goto.await_args.args[0] == "https://www.rednote.com/search_result?keyword=abc"
    assert service.page is alternate_page
    assert service.last_http_status == 200
    service.context.clear_cookies.assert_not_awaited()


@pytest.mark.anyio
async def test_both_domains_unavailable_report_error_without_deleting_login(tmp_path):
    service = BrowserService(profile=tmp_path)
    service.context = Mock()
    service.context.clear_cookies = AsyncMock()
    service.page = Mock()
    service.page.goto = AsyncMock(return_value=Mock(status=503))
    service.page.close = AsyncMock()
    alternate_page = Mock()
    alternate_page.goto = AsyncMock(return_value=Mock(status=503))
    service.context.new_page = AsyncMock(return_value=alternate_page)
    with pytest.raises(Error, match="HTTP 503"):
        await service.navigate_preserving_session("https://www.rednote.com/explore")
    assert alternate_page.goto.await_count == 1
    service.context.clear_cookies.assert_not_awaited()


@pytest.mark.anyio
async def test_chrome_error_page_is_not_accepted_as_success(tmp_path):
    service = BrowserService(profile=tmp_path)
    service.page = Mock()
    service.page.url = "chrome-error://chromewebdata/"
    service.page.goto = AsyncMock(return_value=None)
    with pytest.raises(Error, match="trusted platform page"):
        await service.navigate_preserving_session("https://www.rednote.com/explore")
    assert service.page.goto.await_count == 2
