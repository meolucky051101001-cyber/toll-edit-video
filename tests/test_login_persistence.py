import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.core.config import Settings


@pytest.mark.anyio
async def test_xhs_inspect_recognizes_session_cookies_without_dom_avatar(tmp_path: Path):
    svc = BrowserService(profile=tmp_path)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[
        {"name": "web_session", "value": "030037ada85203699669", "domain": ".rednote.com"},
        {"name": "id_token", "value": "VjEAAKYBlQc69to0QYHy", "domain": ".rednote.com"},
    ])
    mock_context.storage_state = AsyncMock()
    svc.context = mock_context

    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://www.rednote.com/search_result?keyword=decor"
    mock_page.title = AsyncMock(return_value="rednote search")
    
    # Body has no login keyword, DOM avatar not found
    mock_locator = MagicMock()
    mock_locator.inner_text = AsyncMock(return_value="Decor ideas and trends")
    mock_locator.all = AsyncMock(return_value=[])
    mock_locator.count = AsyncMock(return_value=0)
    mock_locator.first = mock_locator
    mock_locator.input_value = AsyncMock(return_value="decor")
    mock_page.locator.return_value = mock_locator

    state = await svc.inspect(mock_page)

    assert state == "page_available"
    assert svc.diagnostics["is_authenticated"] is True
    # storage_state should be auto-persisted
    mock_context.storage_state.assert_called_once()


@pytest.mark.anyio
async def test_xhs_inspect_auto_dismisses_login_modal(tmp_path: Path):
    svc = BrowserService(profile=tmp_path)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[])
    mock_context.storage_state = AsyncMock()
    svc.context = mock_context

    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://www.rednote.com/search_result?keyword=decor"
    mock_page.title = AsyncMock(return_value="rednote search")

    # Setup close button click
    close_btn = MagicMock()
    close_btn.count = AsyncMock(return_value=1)
    close_btn.click = AsyncMock()
    close_btn.first = close_btn

    # Setup login modal that disappears after close
    modal_loc = MagicMock()
    modal_counts = [1, 0]  # First visible, then closed
    modal_loc.count = AsyncMock(side_effect=lambda: modal_counts.pop(0) if modal_counts else 0)

    # Note items visible
    note_loc = MagicMock()
    note_loc.count = AsyncMock(return_value=12)

    def locator_router(sel, *args, **kwargs):
        if "close-button" in sel or "icon-btn-wrapper" in sel:
            return close_btn
        if "login-modal" in sel or "login-container" in sel:
            return modal_loc
        if "note-item" in sel or "explore" in sel:
            return note_loc
        dummy = MagicMock()
        dummy.inner_text = AsyncMock(return_value="")
        dummy.count = AsyncMock(return_value=0)
        dummy.all = AsyncMock(return_value=[])
        dummy.first = dummy
        return dummy

    mock_page.locator.side_effect = locator_router
    mock_page.wait_for_timeout = AsyncMock()

    state = await svc.inspect(mock_page)

    assert state == "page_available"
    close_btn.click.assert_called_once()


@pytest.mark.anyio
async def test_douyin_inspect_recognizes_session_cookies(tmp_path: Path):
    svc = DouyinBrowserService(profile=tmp_path)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[
        {"name": "sessionid", "value": "abc123456", "domain": ".douyin.com"},
    ])
    mock_context.storage_state = AsyncMock()
    svc.context = mock_context

    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://www.douyin.com/search/test"
    mock_page.title = AsyncMock(return_value="Douyin")

    dummy = MagicMock()
    dummy.inner_text = AsyncMock(return_value="Search results")
    dummy.count = AsyncMock(return_value=0)
    dummy.all = AsyncMock(return_value=[])
    dummy.first = dummy
    mock_page.locator.return_value = dummy

    state = await svc.inspect(mock_page)

    assert state == "page_available"
    assert svc.diagnostics["is_authenticated"] is True
    mock_context.storage_state.assert_called_once()


@pytest.mark.anyio
async def test_douyin_inspect_auto_dismisses_login_panel(tmp_path: Path):
    svc = DouyinBrowserService(profile=tmp_path)
    mock_context = MagicMock()
    mock_context.cookies = AsyncMock(return_value=[])
    mock_context.storage_state = AsyncMock()
    svc.context = mock_context

    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://www.douyin.com/search/test"
    mock_page.title = AsyncMock(return_value="Douyin")

    close_btn = MagicMock()
    close_btn.count = AsyncMock(return_value=1)
    close_btn.click = AsyncMock()
    close_btn.first = close_btn

    panel_loc = MagicMock()
    panel_counts = [1, 0]
    panel_loc.count = AsyncMock(side_effect=lambda: panel_counts.pop(0) if panel_counts else 0)

    card_loc = MagicMock()
    card_loc.count = AsyncMock(return_value=8)

    def locator_router(sel, *args, **kwargs):
        if "captcha" in sel:
            d = MagicMock()
            d.count = AsyncMock(return_value=0)
            return d
        if "login-panel-close" in sel or "dy-account-close" in sel:
            return close_btn
        if "login-panel" in sel or "login-mask" in sel:
            return panel_loc
        if "video" in sel or "search-result-card" in sel:
            return card_loc
        dummy = MagicMock()
        dummy.inner_text = AsyncMock(return_value="")
        dummy.count = AsyncMock(return_value=0)
        dummy.all = AsyncMock(return_value=[])
        dummy.first = dummy
        return dummy

    mock_page.locator.side_effect = locator_router
    mock_page.wait_for_timeout = AsyncMock()

    state = await svc.inspect(mock_page)

    assert state == "page_available"
    close_btn.click.assert_called_once()


@pytest.mark.anyio
async def test_browser_close_persists_auth_state(tmp_path: Path):
    svc = BrowserService(profile=tmp_path)
    mock_context = MagicMock()
    mock_context.storage_state = AsyncMock()
    mock_context.close = AsyncMock()
    svc.context = mock_context

    await svc._close()

    mock_context.storage_state.assert_called_once()
    mock_context.close.assert_called_once()
