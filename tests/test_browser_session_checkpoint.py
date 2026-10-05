import asyncio
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import async_playwright

from backend.core.config import settings
from backend.services.browser_service import BrowserService
from backend.services.browser_session import (
    restore_missing_cookies,
    save_headful_preference,
    save_session_checkpoint,
    visible_challenge_evidence,
)
from backend.services.cookie_service import persist_auth_cookies
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.services.session_keeper import SessionKeepAliveService


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
def test_refresh_verification_uses_the_active_tab_and_preserves_cookies(platform, client, monkeypatch):
    from backend.app.main import app
    browser = Mock()
    browser.lock = asyncio.Lock()
    browser.state = "verification_required"
    page = Mock()
    page.reload = AsyncMock()
    browser.get_active_page.return_value = page
    browser.inspect = AsyncMock()
    browser.snapshot.return_value = {"state": "page_available"}
    browser.context.clear_cookies = AsyncMock()
    monkeypatch.setattr(app.state, "douyin_browser" if platform == "douyin" else "browser", browser)
    response = client.post(f"/api/browser/{platform}/refresh_verification")
    assert response.status_code == 200
    page.reload.assert_awaited_once_with(wait_until="domcontentloaded", timeout=30000)
    browser.inspect.assert_awaited_once_with(page)
    browser.context.clear_cookies.assert_not_awaited()


@pytest.mark.parametrize("platform", ["douyin", "xiaohongshu"])
def test_refresh_verification_refuses_to_reload_authenticated_search(platform, client, monkeypatch):
    from backend.app.main import app
    browser = Mock()
    browser.lock = asyncio.Lock()
    browser.state = "authenticated"
    page = Mock()
    page.reload = AsyncMock()
    browser.get_active_page.return_value = page
    monkeypatch.setattr(app.state, "douyin_browser" if platform == "douyin" else "browser", browser)
    assert client.post(f"/api/browser/{platform}/refresh_verification").status_code == 409
    page.reload.assert_not_awaited()


def test_cookie_expiry_is_preserved_and_expired_cookie_is_not_restored():
    future = time.time() + 600
    cookies = [
        {"name": "sessionid", "value": "active", "expires": -1},
        {"name": "token", "value": "active", "expires": future},
        {"name": "expired", "value": "old", "expires": time.time() - 1},
    ]
    assert persist_auth_cookies(cookies) == cookies[:2]
    assert cookies[0]["expires"] == -1


@pytest.mark.anyio
async def test_checkpoint_does_not_replace_native_cookies_with_older_tokens():
    context = Mock()
    current = {"name": "sessionid", "value": "fresh", "domain": ".douyin.com", "path": "/"}
    context.cookies = AsyncMock(return_value=[current])
    context.add_cookies = AsyncMock()
    await restore_missing_cookies(context, [{**current, "value": "stale"}])
    context.add_cookies.assert_not_awaited()


@pytest.mark.anyio
async def test_atomic_checkpoint_failure_preserves_previous_state(tmp_path, monkeypatch):
    old = tmp_path / "auth_state.json"
    old.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    before = old.read_bytes()
    context = Mock()
    context.storage_state = AsyncMock(return_value={"cookies": [], "origins": []})
    def reject_replace(*_args):
        raise OSError("disk unavailable")
    monkeypatch.setattr("backend.services.browser_session.os.replace", reject_replace)
    with pytest.raises(OSError):
        await save_session_checkpoint(context, tmp_path)
    assert old.read_bytes() == before
    assert list(tmp_path.glob("auth-*.tmp")) == []


@pytest.mark.anyio
async def test_real_chromium_restores_session_cookie_after_context_restart(tmp_path):
    async with async_playwright() as driver:
        context = await driver.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            headless=True,
            channel=settings.xhs_browser_channel,
        )
        await context.add_cookies([{"name": "sessionid", "value": "synthetic-roundtrip",
                                  "domain": ".example.test", "path": "/", "expires": -1}])
        await save_session_checkpoint(context, tmp_path)
        await context.close()
        saved = json.loads((tmp_path / "auth_state.json").read_text("utf-8"))
        assert saved["cookies"][0]["expires"] == -1
        restored = await driver.chromium.launch_persistent_context(
            str(tmp_path / "profile"),
            headless=True,
            channel=settings.xhs_browser_channel,
        )
        await restore_missing_cookies(restored, saved["cookies"])
        assert any(c["name"] == "sessionid" and c["value"] == "synthetic-roundtrip" for c in await restored.cookies())
        await restored.close()


@pytest.mark.anyio
@pytest.mark.parametrize("service_type,domain", [(BrowserService, "www.rednote.com"), (DouyinBrowserService, "www.douyin.com")])
async def test_elevation_opens_blocked_tab_directly_not_home(service_type, domain, tmp_path):
    service = service_type(profile=tmp_path)
    service.is_currently_headless = True
    service.page = Mock(url=f"https://{domain}/")
    service.page.is_closed.return_value = False
    service.blocked_page = Mock(url=f"https://{domain}/search/challenge?q=abc")
    service.blocked_page.is_closed.return_value = False
    service._close = AsyncMock()
    service.ensure_open = AsyncMock(return_value=Mock())
    service.inspect = AsyncMock()
    await service.elevate_to_headful()
    service.ensure_open.assert_awaited_once_with(headless_override=False, initial_url=service.blocked_page.url)


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", [BrowserService, DouyinBrowserService])
async def test_visible_window_focuses_blocked_tab(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.is_currently_headless = False
    service.page = Mock()
    service.page.is_closed.return_value = False
    service.blocked_page = Mock()
    service.blocked_page.is_closed.return_value = False
    service.blocked_page.bring_to_front = AsyncMock()
    service.ensure_open = AsyncMock(return_value=service.page)
    service.inspect = AsyncMock()
    await service.open()
    service.blocked_page.bring_to_front.assert_awaited_once()
    service.inspect.assert_awaited_with(service.blocked_page)


@pytest.mark.anyio
async def test_hidden_xhs_captcha_frame_does_not_block_search():
    page = Mock()
    locator = Mock()
    locator.all = AsyncMock(return_value=[])
    page.locator.return_value = locator
    element = Mock()
    element.is_visible = AsyncMock(return_value=False)
    frame = Mock(url="https://security.example.test/captcha/sdk")
    frame.frame_element = AsyncMock(return_value=element)
    page.frames = [frame]
    assert await visible_challenge_evidence(page) is None


@pytest.mark.anyio
async def test_heartbeat_never_touches_a_pending_captcha(tmp_path):
    browser = Mock()
    browser.lock = asyncio.Lock()
    browser.state = "verification_required"
    page = Mock()
    page.is_closed.return_value = False
    page.evaluate = AsyncMock()
    browser.get_active_page.return_value = page
    keeper = SessionKeepAliveService(douyin_profile=tmp_path, xhs_profile=tmp_path)
    assert await keeper.ping_platform("douyin", browser) is False
    page.evaluate.assert_not_awaited()


@pytest.mark.anyio
async def test_http_200_heartbeat_is_not_evidence_of_login(tmp_path):
    browser = Mock()
    browser.lock = asyncio.Lock()
    browser.state = "page_available"
    page = Mock(url="https://www.rednote.com/search_result?keyword=abc")
    page.is_closed.return_value = False
    page.evaluate = AsyncMock(return_value={"ok": True, "status": 200})
    browser.get_active_page.return_value = page
    browser.inspect = AsyncMock(return_value="page_available")
    keeper = SessionKeepAliveService(douyin_profile=tmp_path, xhs_profile=tmp_path)
    assert await keeper.ping_platform("xiaohongshu", browser) is False
    assert keeper.last_heartbeat == {}


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", [BrowserService, DouyinBrowserService])
async def test_network_probe_does_not_hide_a_challenge_on_the_active_tab(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.state = "verification_required"
    service.diagnostics = {"captcha_visible": True, "path": "/search/abc"}
    original_diagnostics = dict(service.diagnostics)
    service.ensure_open = AsyncMock()
    probe = Mock()
    probe.goto = AsyncMock(return_value=Mock(status=200))
    probe.is_closed.return_value = False
    probe.close = AsyncMock()
    service.context = Mock()
    service.context.new_page = AsyncMock(return_value=probe)
    async def inspect_probe(_page):
        service.state = "authenticated"
        service.diagnostics = {"captcha_visible": False}
        return "authenticated"
    service.inspect = AsyncMock(side_effect=inspect_probe)
    reachable, description = await service.probe_live()
    assert reachable is True
    assert "verification_required" in description
    assert service.state == "verification_required"
    assert service.diagnostics == original_diagnostics


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", [BrowserService, DouyinBrowserService])
async def test_probe_skips_busy_browser(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.ensure_open = AsyncMock()
    async with service.lock:
        assert await service.probe_live() == (False, "browser_busy")
    service.ensure_open.assert_not_awaited()


@pytest.mark.parametrize("visible_window,state", [(True, "page_available"), (False, "verification_required")])
def test_verification_resources_are_not_blocked(visible_window, state, tmp_path):
    service = DouyinBrowserService(profile=tmp_path)
    service.is_currently_headless = not visible_window
    service.state = state
    for url, resource_type in [
        ("https://security.douyin.com/challenge.mp4", "media"),
        ("https://security.douyin.com/challenge.woff2", "font"),
        ("https://hm.baidu.com/script.js", "script"),
    ]:
        assert service.should_block_resource(url, resource_type) is False


def test_background_search_can_still_block_large_media(tmp_path):
    service = DouyinBrowserService(profile=tmp_path)
    service.is_currently_headless = True
    service.state = "page_available"
    assert service.should_block_resource("https://video.douyin.com/stream.mp4", "media") is True
    assert service.should_block_resource("https://security.douyin.com/challenge.png", "image") is False


@pytest.mark.parametrize("service_type", [BrowserService, DouyinBrowserService])
def test_user_opened_mode_survives_service_restart(service_type, tmp_path):
    save_headful_preference(tmp_path)
    restarted = service_type(profile=tmp_path, headless=True)
    assert restarted.should_run_headless() is False
    assert restarted.sticky_headful is True


@pytest.mark.parametrize("service_type", [BrowserService, DouyinBrowserService])
def test_corrupt_mode_preference_has_safe_default(service_type, tmp_path):
    (tmp_path / "browser_preferences.json").write_text('{"prefer_headful":"false"}', encoding="utf-8")
    assert service_type(profile=tmp_path, headless=True).should_run_headless() is True
