"""L6: the browser resolver must time out instead of stalling behind a search."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.models.video import Video
from backend.services.browser_lock import OwnedLock
from backend.services.download import browser_resolver as br
from backend.services.download.base import DownloadAdapterError
from backend.services.download.browser_resolver import BrowserMediaResolver


def _video() -> Video:
    return Video(
        id="v1",
        platform="xiaohongshu",
        platform_video_id="661234567890",
        canonical_url="https://www.xiaohongshu.com/explore/661234567890",
        search_query="test",
        search_job_id="job1",
    )


def _browser(lock):
    browser = MagicMock()
    browser.lock = lock
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={"mediaUrl": "https://sns-video-bd.xhscdn.com/v"})
    browser.get_active_page.return_value = page
    return browser


@pytest.mark.anyio
@pytest.mark.parametrize("lock_factory", [asyncio.Lock, OwnedLock], ids=["asyncio", "owned"])
async def test_resolver_lock_timeout_raises_browser_busy(monkeypatch, lock_factory):
    monkeypatch.setattr(br, "BROWSER_LOCK_TIMEOUT_SECONDS", 0.1)
    browser = _browser(lock_factory())
    await browser.lock.acquire()  # a search holds the lock
    resolver = BrowserMediaResolver(xhs_browser_service=browser)

    with pytest.raises(DownloadAdapterError) as ei:
        await asyncio.wait_for(resolver.resolve(_video()), timeout=2)

    assert ei.value.code == "browser_busy"
    assert browser.lock.locked(), "timeout must not steal or leak the held lock"
    browser.lock.release()


@pytest.mark.anyio
@pytest.mark.parametrize("lock_factory", [asyncio.Lock, OwnedLock], ids=["asyncio", "owned"])
async def test_resolver_acquires_and_releases_lock(monkeypatch, lock_factory):
    monkeypatch.setattr(br, "BROWSER_LOCK_TIMEOUT_SECONDS", 1.0)
    browser = _browser(lock_factory())
    resolver = BrowserMediaResolver(xhs_browser_service=browser)

    resolved = await resolver.resolve(_video())

    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/v"
    assert not browser.lock.locked(), "resolver must release the lock after resolving"


@pytest.mark.anyio
async def test_resolver_without_lock_still_resolves():
    browser = _browser(None)
    resolver = BrowserMediaResolver(xhs_browser_service=browser)

    resolved = await resolver.resolve(_video())

    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/v"