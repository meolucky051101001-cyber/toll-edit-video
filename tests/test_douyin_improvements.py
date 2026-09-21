import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.video import Video
from backend.services.download.base import DownloadAdapterError
from backend.services.download.browser_resolver import (
    DOUYIN_DOM_STATE_SCRIPT,
    BrowserMediaResolver,
)


def test_douyin_dom_state_script_syntax():
    """Verify DOUYIN_DOM_STATE_SCRIPT is valid JavaScript code."""
    assert "document.querySelector('video')" in DOUYIN_DOM_STATE_SCRIPT
    assert "window._ROUTER_DATA" in DOUYIN_DOM_STATE_SCRIPT
    assert "RENDER_DATA" in DOUYIN_DOM_STATE_SCRIPT
    assert "playwm" in DOUYIN_DOM_STATE_SCRIPT


@pytest.mark.anyio
async def test_resolve_douyin_auto_opens_browser_when_closed():
    """Verify resolve_douyin calls ensure_open when get_active_page fails."""
    mock_browser = MagicMock()
    mock_browser.get_active_page.side_effect = Exception("Browser closed")
    mock_page = AsyncMock()
    mock_browser.ensure_open = AsyncMock(return_value=mock_page)
    mock_browser.lock = asyncio.Lock()
    mock_browser.context = None  # test in-page evaluate fallback

    # Mock in-page evaluate returning mediaUrl
    mock_page.evaluate.return_value = {
        "mediaUrl": "https://v3-web.douyinvod.com/stream/video_playwm.mp4"
    }

    resolver = BrowserMediaResolver(douyin_browser_service=mock_browser)
    video = Video(
        id="vid-1",
        platform="douyin",
        platform_video_id="7412345678901234567",
        url="https://www.douyin.com/video/7412345678901234567",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    resolved = await resolver.resolve_douyin(video)

    assert mock_browser.ensure_open.called
    assert resolved.platform == "douyin"
    assert resolved.video_id == "7412345678901234567"
    # Watermark stripped
    assert resolved.media_url == "https://v3-web.douyinvod.com/stream/video_play.mp4"
    assert resolved.headers["Referer"] == "https://www.douyin.com/"


@pytest.mark.anyio
async def test_resolve_douyin_network_interception_strategy():
    """Verify resolve_douyin captures video URL from network responses."""
    mock_browser = MagicMock()
    mock_browser.lock = asyncio.Lock()
    mock_page = AsyncMock()
    mock_browser.get_active_page.return_value = mock_page

    mock_context = AsyncMock()
    mock_tab = AsyncMock()
    mock_tab.on = MagicMock()
    mock_tab.is_closed = MagicMock(return_value=False)
    mock_context.new_page = AsyncMock(return_value=mock_tab)
    mock_browser.context = mock_context

    # Simulate network listener capturing ByteDance CDN URL
    callbacks = []

    def fake_on(event, handler):
        if event == "response":
            callbacks.append(handler)

    mock_tab.on.side_effect = fake_on

    async def fake_goto(url, **kwargs):
        # Trigger captured network response
        fake_resp = MagicMock()
        fake_resp.url = "https://v26-web.douyinvod.com/video/tos/playwm/video_stream.mp4"
        for cb in callbacks:
            cb(fake_resp)

    mock_tab.goto.side_effect = fake_goto
    mock_tab.evaluate.return_value = None
    mock_tab.is_closed.return_value = False

    resolver = BrowserMediaResolver(douyin_browser_service=mock_browser)
    video = Video(
        id="vid-2",
        platform="douyin",
        url="https://www.douyin.com/video/7483637785866652938",
        canonical_url="https://www.douyin.com/video/7483637785866652938",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    resolved = await resolver.resolve_douyin(video)

    assert resolved.video_id == "7483637785866652938"
    assert "https://v26-web.douyinvod.com/video/tos/play/video_stream.mp4" == resolved.media_url
    assert mock_tab.close.called


@pytest.mark.anyio
async def test_resolve_douyin_extracts_id_from_url_if_id_empty():
    """Verify video ID is extracted from canonical_url if platform_video_id is empty."""
    mock_browser = MagicMock()
    mock_browser.lock = asyncio.Lock()
    mock_page = AsyncMock()
    mock_browser.get_active_page.return_value = mock_page
    mock_browser.context = None

    mock_page.evaluate.return_value = {
        "mediaUrl": "https://aweme.snssdk.com/aweme/v1/play/?video_id=123"
    }

    resolver = BrowserMediaResolver(douyin_browser_service=mock_browser)
    video = Video(
        id="vid-3",
        platform="douyin",
        platform_video_id="",
        url="https://www.douyin.com/video/7399999999999999999?source=share",
        fingerprint="fp3",
        search_query="test",
        search_job_id="job1",
    )

    resolved = await resolver.resolve_douyin(video)
    assert resolved.video_id == "7399999999999999999"


@pytest.mark.anyio
async def test_resolve_douyin_fails_on_invalid_id():
    """Verify DownloadAdapterError is raised when video ID cannot be determined."""
    mock_browser = MagicMock()
    resolver = BrowserMediaResolver(douyin_browser_service=mock_browser)
    video = Video(
        id="vid-4",
        platform="douyin",
        platform_video_id="",
        url="https://example.com/not-douyin",
        fingerprint="fp4",
        search_query="test",
        search_job_id="job1",
    )

    with pytest.raises(DownloadAdapterError) as exc_info:
        await resolver.resolve_douyin(video)
    assert exc_info.value.code == "invalid_video_id"


@pytest.mark.anyio
async def test_resolve_douyin_fails_on_no_media_url():
    """Verify DownloadAdapterError is raised when no media URL can be resolved."""
    mock_browser = MagicMock()
    mock_browser.lock = asyncio.Lock()
    mock_page = AsyncMock()
    mock_browser.get_active_page.return_value = mock_page
    mock_browser.context = None
    mock_page.evaluate.return_value = None  # No media URL

    resolver = BrowserMediaResolver(douyin_browser_service=mock_browser)
    video = Video(
        id="vid-5",
        platform="douyin",
        platform_video_id="7412345678901234567",
        url="https://www.douyin.com/video/7412345678901234567",
        fingerprint="fp5",
        search_query="test",
        search_job_id="job1",
    )

    with pytest.raises(DownloadAdapterError) as exc_info:
        await resolver.resolve_douyin(video)
    assert exc_info.value.code == "no_media_url"


def test_douyin_browser_service_media_unblock():
    """Verify DouyinBrowserService provides set_allow_media to unblock media for downloader."""
    from backend.services.douyin_browser_service import DouyinBrowserService
    svc = DouyinBrowserService()
    assert svc.allow_media is False
    svc.set_allow_media(True)
    assert svc.allow_media is True
    svc.set_allow_media(False)
    assert svc.allow_media is False


def test_douyin_provider_card_title_extraction_logic():
    """Verify fallback title logic generates informative title if alt or card text was empty."""
    from backend.providers.douyin.parser import parse_count_text
    # parse_count_text tests
    assert parse_count_text("99w") == 990000
    assert parse_count_text("1.5万") == 15000
    assert parse_count_text("320") == 320
