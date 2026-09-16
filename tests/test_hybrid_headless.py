import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.base import UserActionRequired


def test_browser_service_should_run_headless(tmp_path: Path):
    # Default without auth file
    svc = BrowserService(profile=tmp_path, headless=False, hybrid_headless=True)
    assert svc.should_run_headless() is False

    # Force headless
    svc_forced = BrowserService(profile=tmp_path, headless=True, hybrid_headless=True)
    assert svc_forced.should_run_headless() is True

    # Disabled hybrid headless
    svc_disabled = BrowserService(profile=tmp_path, headless=False, hybrid_headless=False)
    assert svc_disabled.should_run_headless() is False

    # With valid auth file
    auth_file = tmp_path / "auth_state.json"
    auth_file.write_text(json.dumps({
        "grade": "high_quality",
        "cookies": [{"name": "web_session", "value": "xyz", "domain": ".xiaohongshu.com"}]
    }), encoding="utf-8")

    assert svc.should_run_headless() is True


def test_douyin_browser_service_should_run_headless(tmp_path: Path):
    svc = DouyinBrowserService(profile=tmp_path, headless=False, hybrid_headless=True)
    assert svc.should_run_headless() is False

    auth_file = tmp_path / "auth_state.json"
    auth_file.write_text(json.dumps({
        "grade": "valid",
        "cookies": [{"name": "sessionid", "value": "123", "domain": ".douyin.com"}]
    }), encoding="utf-8")

    assert svc.should_run_headless() is True


@pytest.mark.anyio
async def test_xhs_elevate_to_headful(tmp_path: Path):
    svc = BrowserService(profile=tmp_path, headless=False, hybrid_headless=True)
    svc.is_currently_headless = True
    mock_page = MagicMock()
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.is_closed.return_value = False
    mock_page.goto = AsyncMock()
    svc.page = mock_page

    with patch.object(svc, "_close", new_callable=AsyncMock) as mock_close, \
         patch.object(svc, "ensure_open", new_callable=AsyncMock) as mock_ensure, \
         patch.object(svc, "inspect", new_callable=AsyncMock) as mock_inspect:
        new_page = MagicMock()
        new_page.goto = AsyncMock()
        mock_ensure.return_value = new_page

        result = await svc.elevate_to_headful()
        assert svc.hybrid_headless is False
        mock_close.assert_called_once()
        mock_ensure.assert_called_once()
        new_page.goto.assert_called_once_with("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=15000)
        mock_inspect.assert_called_once_with(new_page)
        assert result == new_page


@pytest.mark.anyio
async def test_xhs_provider_gate_default_headless_no_elevation():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=False)
    page = MagicMock()

    with pytest.raises(UserActionRequired):
        await provider.gate(page, [])

    browser.elevate_to_headful.assert_not_called()


@pytest.mark.anyio
async def test_xhs_provider_gate_triggers_elevation_when_explicit():
    browser = MagicMock(spec=BrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="login_required")
    browser.elevate_to_headful = AsyncMock()

    provider = XiaohongshuProvider(browser=browser, auto_elevate=True)
    page = MagicMock()

    with pytest.raises(UserActionRequired):
        await provider.gate(page, [])

    browser.elevate_to_headful.assert_called_once()


@pytest.mark.anyio
async def test_douyin_provider_gate_default_headless_no_elevation():
    browser = MagicMock(spec=DouyinBrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="verification_required")
    browser.elevate_to_headful = AsyncMock()

    provider = DouyinProvider(browser=browser, auto_elevate=False)
    page = MagicMock()

    with pytest.raises(UserActionRequired):
        await provider.gate(page, [])

    browser.elevate_to_headful.assert_not_called()


@pytest.mark.anyio
async def test_douyin_provider_gate_triggers_elevation_when_explicit():
    browser = MagicMock(spec=DouyinBrowserService)
    browser.is_currently_headless = True
    browser.inspect = AsyncMock(return_value="verification_required")
    browser.elevate_to_headful = AsyncMock()

    provider = DouyinProvider(browser=browser, auto_elevate=True)
    page = MagicMock()

    with pytest.raises(UserActionRequired):
        await provider.gate(page, [])

    browser.elevate_to_headful.assert_called_once()


def test_snapshots_include_login_url(tmp_path: Path):
    xhs = BrowserService(profile=tmp_path)
    xhs_snap = xhs.snapshot()
    assert "login_url" in xhs_snap
    assert "xiaohongshu.com" in xhs_snap["login_url"] or "rednote.com" in xhs_snap["login_url"]

    douyin = DouyinBrowserService(profile=tmp_path)
    dy_snap = douyin.snapshot()
    assert "login_url" in dy_snap
    assert "douyin.com" in dy_snap["login_url"]

