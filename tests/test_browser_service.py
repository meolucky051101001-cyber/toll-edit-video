from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.services.browser_service import BrowserService


@pytest.mark.anyio
async def test_xhs_inspect_and_probe_authenticated(tmp_path):
    service = BrowserService(profile=tmp_path / "xhs_profile", headless=True)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[{"name": "web_session", "value": "valid_token"}])
    mock_context.storage_state = AsyncMock()

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="小红书 - 发现美好世界")

    # Mock locators
    def mock_locator(selector: str):
        loc = AsyncMock()
        # body inner text
        loc.inner_text = AsyncMock(return_value="公开内容")
        # avatar / profile element present in DOM
        if "avatar" in selector or "profile" in selector or "Me" in selector:
            loc.count = AsyncMock(return_value=1)
        elif "captcha" in selector or "verify" in selector:
            loc.count = AsyncMock(return_value=0)
        elif "login" in selector:
            loc.count = AsyncMock(return_value=0)
        else:
            loc.count = AsyncMock(return_value=1)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.frames = []
    mock_page.evaluate = AsyncMock(return_value=True)
    service.context = mock_context

    # Verify inspect() classifies as authenticated
    state = await service.inspect(mock_page)
    assert state == "authenticated"
    assert service.diagnostics["is_authenticated"] is True

    # Verify probe_live() outputs HTTP 200: authenticated
    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    service.ensure_open = AsyncMock(return_value=mock_page)

    is_healthy, desc = await service.probe_live()
    assert is_healthy is True
    assert desc == "HTTP 200: authenticated"
    assert service.last_http_status == 200


@pytest.mark.anyio
async def test_xhs_guest_page_with_sidebar_and_stale_cookie_not_authenticated(tmp_path):
    """Verifies that generic sidebar items (我/Me) and guest web_session cookies do not cause false authenticated state."""
    service = BrowserService(profile=tmp_path / "xhs_guest_sidebar", headless=True)
    mock_context = MagicMock()
    # Real XHS sets web_session even for guest visitors
    mock_context.cookies = AsyncMock(return_value=[{"name": "web_session", "value": "guest_cookie_val"}])

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="小红书 - 你的生活兴趣社区")

    def mock_locator(selector: str):
        loc = AsyncMock()
        loc.inner_text = AsyncMock(return_value="发现 / 我")
        # Sidebar has generic "我" or "Me", but NO user profile URL with ID and NO user avatar
        if "/user/profile/" in selector or "avatar" in selector:
            loc.count = AsyncMock(return_value=0)
        # Login button is prominently visible on guest page
        elif "button:has-text('登录')" in selector or ".login-btn" in selector or "登录" in selector:
            loc.count = AsyncMock(return_value=1)
        # No blocking modal or captcha
        elif "modal" in selector or "container" in selector or "captcha" in selector or "verify" in selector:
            loc.count = AsyncMock(return_value=0)
        else:
            loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.frames = []
    # Client state confirms not logged in (loggedIn: false)
    mock_page.evaluate = AsyncMock(return_value=False)
    service.context = mock_context

    state = await service.inspect(mock_page)
    # Must be page_available (guest), NEVER authenticated
    assert state == "page_available"
    assert service.diagnostics["is_authenticated"] is False
    assert service.diagnostics["has_login_button"] is True


@pytest.mark.anyio
async def test_xhs_inspect_overridden_by_js_logged_in_false(tmp_path):
    """Verifies that if window.__INITIAL_STATE__.user.loggedIn is false, session is never classified as authenticated."""
    service = BrowserService(profile=tmp_path / "xhs_js_override", headless=True)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[{"name": "web_session", "value": "some_token"}])

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="小红书")

    def mock_locator(selector: str):
        loc = AsyncMock()
        loc.inner_text = AsyncMock(return_value="")
        # Suppose a DOM avatar is present
        if "avatar" in selector or "profile" in selector:
            loc.count = AsyncMock(return_value=1)
        else:
            loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.frames = []
    # Server/client state explicitly reports false
    mock_page.evaluate = AsyncMock(return_value=False)
    service.context = mock_context

    state = await service.inspect(mock_page)
    assert state == "page_available"
    assert service.diagnostics["is_authenticated"] is False


@pytest.mark.anyio
async def test_xhs_inspect_and_probe_unauthenticated_guest(tmp_path):
    service = BrowserService(profile=tmp_path / "xhs_guest", headless=True)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[])

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="小红书 - 发现美好世界")

    def mock_locator(selector: str):
        loc = AsyncMock()
        loc.inner_text = AsyncMock(return_value="公开内容")
        if "avatar" in selector or "profile" in selector or "Me" in selector:
            loc.count = AsyncMock(return_value=0)
        elif "captcha" in selector or "verify" in selector:
            loc.count = AsyncMock(return_value=0)
        elif "login" in selector:
            loc.count = AsyncMock(return_value=0)
        elif "note-item" in selector:
            loc.count = AsyncMock(return_value=5)
        else:
            loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.frames = []
    service.context = mock_context

    # Verify inspect() classifies as page_available (guest)
    state = await service.inspect(mock_page)
    assert state == "page_available"
    assert service.diagnostics["is_authenticated"] is False

    # Verify probe_live() outputs HTTP 200: page_available
    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    service.ensure_open = AsyncMock(return_value=mock_page)

    is_healthy, desc = await service.probe_live()
    assert is_healthy is True
    assert desc == "HTTP 200: page_available"


@pytest.mark.anyio
async def test_xhs_inspect_and_probe_captcha_challenge(tmp_path):
    service = BrowserService(profile=tmp_path / "xhs_captcha", headless=True)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[])

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="安全验证")

    def mock_locator(selector: str):
        loc = AsyncMock()
        loc.inner_text = AsyncMock(return_value="安全验证 拖动滑块")
        if "captcha" in selector or "verify" in selector:
            loc.count = AsyncMock(return_value=1)
        else:
            loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.frames = []
    service.context = mock_context

    state = await service.inspect(mock_page)
    assert state == "verification_required"

    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    service.ensure_open = AsyncMock(return_value=mock_page)

    is_healthy, desc = await service.probe_live()
    assert is_healthy is True
    assert desc == "HTTP 200: verification_required"


@pytest.mark.anyio
async def test_xhs_inspect_and_probe_stale_cookie_with_login_modal(tmp_path):
    service = BrowserService(profile=tmp_path / "xhs_stale", headless=True)
    # Stale/expired cookie exists in context
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[{"name": "web_session", "value": "expired_token"}])

    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.url = "https://www.xiaohongshu.com/explore"
    mock_page.title = AsyncMock(return_value="登录")

    def mock_locator(selector: str):
        loc = AsyncMock()
        loc.inner_text = AsyncMock(return_value="请先登录")
        # No user element in DOM
        if "avatar" in selector or "profile" in selector or "Me" in selector:
            loc.count = AsyncMock(return_value=0)
        elif "captcha" in selector:
            loc.count = AsyncMock(return_value=0)
        # Login modal is present and non-dismissible
        elif "login" in selector:
            loc.count = AsyncMock(return_value=1)
        else:
            loc.count = AsyncMock(return_value=0)
        loc.first = loc
        loc.all = AsyncMock(return_value=[])
        loc.click = AsyncMock(side_effect=Exception("No close button"))
        return loc

    mock_page.locator = MagicMock(side_effect=mock_locator)
    mock_page.keyboard = MagicMock(press=AsyncMock())
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.frames = []
    service.context = mock_context

    # Must classify as login_required, NOT authenticated or page_available
    state = await service.inspect(mock_page)
    assert state == "login_required"
    assert service.diagnostics["is_authenticated"] is False

    mock_resp_200 = MagicMock(status=200)
    mock_page.goto = AsyncMock(return_value=mock_resp_200)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    service.ensure_open = AsyncMock(return_value=mock_page)

    is_healthy, desc = await service.probe_live()
    assert is_healthy is True
    assert desc == "HTTP 200: login_required"


@pytest.mark.anyio
async def test_xhs_probe_network_error_503_or_timeout(tmp_path):
    service = BrowserService(profile=tmp_path / "xhs_err", headless=True)
    mock_context = MagicMock()
    mock_page = AsyncMock()
    mock_page.is_closed = MagicMock(return_value=False)
    mock_context.new_page = AsyncMock(return_value=mock_page)
    service.context = mock_context
    service.ensure_open = AsyncMock(return_value=mock_page)

    # 1. 503 response
    mock_resp_503 = MagicMock(status=503)
    mock_page.goto = AsyncMock(return_value=mock_resp_503)
    service.inspect = AsyncMock(return_value="network_error")

    ok_503, desc_503 = await service.probe_live()
    assert ok_503 is False
    assert "503" in desc_503

    # 2. Network Timeout exception
    mock_page.goto = AsyncMock(side_effect=RuntimeError("Timeout 20000ms exceeded"))
    ok_timeout, desc_timeout = await service.probe_live()
    assert ok_timeout is False
    assert "error" in desc_timeout
