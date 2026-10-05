from unittest.mock import Mock

import pytest

from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService


@pytest.mark.parametrize(
    "service_type",
    [BrowserService, DouyinBrowserService],
    ids=["xiaohongshu", "douyin"],
)
def test_authenticated_browser_status_snapshot_does_not_raise(service_type):
    service = service_type()
    service.state = "authenticated"
    service.page = Mock()
    service.page.is_closed.return_value = False
    service.page.url = "https://example.invalid/"

    snapshot = service.snapshot()

    assert snapshot["state"] == "authenticated"
    assert "Đã đăng nhập" in snapshot["message"]


@pytest.mark.parametrize(
    "service_type",
    [BrowserService, DouyinBrowserService],
    ids=["xiaohongshu", "douyin"],
)
def test_unknown_browser_state_has_safe_status_message(service_type):
    service = service_type()
    service.state = "unexpected-state"
    service.page = Mock()
    service.page.is_closed.return_value = False
    service.page.url = "https://example.invalid/"

    snapshot = service.snapshot()

    assert snapshot["state"] == "unexpected-state"
    assert "không xác định" in snapshot["message"].lower()
