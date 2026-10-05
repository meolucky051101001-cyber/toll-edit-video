import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend.api.browser import get_douyin_screenshot, get_screenshot
from backend.services.browser_service import BrowserService


@pytest.mark.parametrize("endpoint,attribute", [
    (get_screenshot, "browser"), (get_douyin_screenshot, "douyin_browser"),
])
@pytest.mark.parametrize("box", [None, {"x": 0, "y": 0, "width": 1, "height": 1}])
def test_qr_endpoint_never_returns_arbitrary_page_image(endpoint, attribute, box):
    page = Mock()
    page.locator.return_value.first.count = AsyncMock(return_value=0 if box is None else 1)
    page.locator.return_value.first.bounding_box = AsyncMock(return_value=box)
    page.screenshot = AsyncMock(return_value=b"page-image")
    browser = Mock()
    browser.get_active_page.return_value = page
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**{attribute: browser})))
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint(request, crop="qr"))
    assert error.value.status_code == 404
    page.screenshot.assert_not_awaited()


def test_xhs_open_focuses_and_rechecks_elevated_page(tmp_path):
    browser = BrowserService(profile=tmp_path)
    page = Mock()
    page.is_closed.return_value = False
    page.url = "https://www.xiaohongshu.com/explore"
    page.bring_to_front = AsyncMock()
    browser.page = page
    browser.elevate_to_headful = AsyncMock(return_value=page)
    browser.inspect = AsyncMock()
    asyncio.run(browser.open())
    page.bring_to_front.assert_awaited_once()
    browser.inspect.assert_awaited_once_with(page)
