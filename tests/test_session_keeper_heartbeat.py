import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.session_keeper import SessionKeepAliveService


class MockBrowser:
    def __init__(self, page_eval_result: dict | None = None, is_logged_in_val: bool = True):
        self.lock = asyncio.Lock()
        self.page = MagicMock()
        self.page.url = "https://www.douyin.com/search/test"
        self.page.is_closed = MagicMock(return_value=False)
        self.page.evaluate = AsyncMock(return_value=page_eval_result or {"ok": True, "status": 200})
        self._logged_in = is_logged_in_val

    async def inspect(self, page) -> str:
        return "authenticated" if self._logged_in else "page_available"


@pytest.mark.anyio
async def test_heartbeat_success(tmp_path: Path):
    """Verifies that a 200 HTTP status updates last_heartbeat and returns True."""
    keeper = SessionKeepAliveService(douyin_profile=tmp_path / "douyin", xhs_profile=tmp_path / "xhs")
    browser = MockBrowser(page_eval_result={"ok": True, "status": 200})

    assert keeper.last_heartbeat.get("douyin") is None

    result = await keeper.ping_platform("douyin", browser)
    assert result is True
    assert keeper.last_heartbeat.get("douyin") is not None


@pytest.mark.anyio
async def test_heartbeat_network_failure_does_not_update_heartbeat(tmp_path: Path):
    """Verifies that network error / 500 status does NOT update last_heartbeat and returns False."""
    keeper = SessionKeepAliveService(douyin_profile=tmp_path / "douyin", xhs_profile=tmp_path / "xhs")
    browser = MockBrowser(page_eval_result={"ok": False, "status": 500, "error": "Server error"})

    result = await keeper.ping_platform("douyin", browser)
    assert result is False
    assert keeper.last_heartbeat.get("douyin") is None


@pytest.mark.anyio
async def test_heartbeat_network_exception_does_not_update_heartbeat(tmp_path: Path):
    """Verifies that an evaluation exception does NOT update last_heartbeat."""
    keeper = SessionKeepAliveService(douyin_profile=tmp_path / "douyin", xhs_profile=tmp_path / "xhs")
    browser = MockBrowser()
    browser.page.evaluate.side_effect = Exception("Connection refused")

    result = await keeper.ping_platform("xiaohongshu", browser)
    assert result is False
    assert keeper.last_heartbeat.get("xiaohongshu") is None


@pytest.mark.anyio
async def test_heartbeat_no_browser_service(tmp_path: Path):
    """Verifies that missing browser_service does not record a fake success."""
    keeper = SessionKeepAliveService(douyin_profile=tmp_path / "douyin", xhs_profile=tmp_path / "xhs")
    result = await keeper.ping_platform("douyin", None)
    assert result is False
    assert keeper.last_heartbeat.get("douyin") is None


@pytest.mark.anyio
async def test_heartbeat_busy_browser_skips(tmp_path: Path):
    """Verifies that when the browser lock is locked, the ping skips cleanly without updating."""
    keeper = SessionKeepAliveService(douyin_profile=tmp_path / "douyin", xhs_profile=tmp_path / "xhs")
    browser = MockBrowser()
    await browser.lock.acquire()
    try:
        result = await keeper.ping_platform("douyin", browser)
        assert result is False
        assert keeper.last_heartbeat.get("douyin") is None
    finally:
        browser.lock.release()
