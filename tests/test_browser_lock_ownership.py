"""H1: browser lock helpers must respect real task ownership.

locked() == True is NOT proof the current caller owns the lock. These tests
pin the fixed behavior: a foreign owner blocks nested helpers, the owning
task can call them without deadlocking, and concurrent opens launch once.
"""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from backend.providers.douyin.provider import DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters
from backend.services.browser_lock import OwnedLock
from backend.services.browser_service import BrowserService
from backend.services.douyin_browser_service import DouyinBrowserService

SERVICES = [BrowserService, DouyinBrowserService]


@pytest.mark.anyio
async def test_wait_for_acquire_records_the_calling_task():
    # api/browser.py acquires via asyncio.wait_for(lock.acquire(), timeout).
    lock = OwnedLock()
    await asyncio.wait_for(lock.acquire(), timeout=1)
    assert lock.locked() is True
    assert lock.owned_by_current_task() is True
    lock.release()
    assert lock.locked() is False
    assert lock.owned_by_current_task() is False


@pytest.mark.anyio
async def test_foreign_owner_is_locked_but_not_owned():
    lock = OwnedLock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        async with lock:
            entered.set()
            await release.wait()

    task = asyncio.create_task(owner())
    await entered.wait()
    assert lock.locked() is True
    assert lock.owned_by_current_task() is False
    release.set()
    await task


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_ensure_open_waits_for_a_foreign_lock_owner(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service._ensure_open_unlocked = AsyncMock(return_value=Mock())
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        async with service.lock:
            entered.set()
            await release.wait()

    async def caller():
        await entered.wait()
        await service.ensure_open()

    owner_task = asyncio.create_task(owner())
    caller_task = asyncio.create_task(caller())
    await entered.wait()
    await asyncio.sleep(0.05)
    # The old locked() bypass ran _ensure_open_unlocked here, mutating the
    # browser while the owning task was still using it.
    service._ensure_open_unlocked.assert_not_awaited()
    release.set()
    await asyncio.gather(owner_task, caller_task)
    service._ensure_open_unlocked.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_nested_ensure_open_from_the_owner_does_not_deadlock(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    page = Mock()
    service._ensure_open_unlocked = AsyncMock(return_value=page)
    async with service.lock:
        result = await asyncio.wait_for(service.ensure_open(), timeout=1)
    assert result is page


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_concurrent_ensure_open_launches_only_once(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    launches = 0

    async def fake_unlocked(headless_override=None, initial_url=None):
        nonlocal launches
        if service.page and not service.page.is_closed():
            return service.page
        launches += 1
        await asyncio.sleep(0.02)
        page = Mock()
        page.is_closed.return_value = False
        service.page = page
        return page

    service._ensure_open_unlocked = fake_unlocked
    pages = await asyncio.gather(*(service.ensure_open() for _ in range(3)))
    assert launches == 1
    assert all(p is pages[0] for p in pages)


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_ensure_headless_session_never_closes_a_context_in_use(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.is_currently_headless = False
    service.context = Mock()
    closed = asyncio.Event()

    async def fake_close():
        closed.set()

    service._close = AsyncMock(side_effect=fake_close)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        async with service.lock:
            entered.set()
            await release.wait()

    async def caller():
        await entered.wait()
        await service.ensure_headless_session()

    owner_task = asyncio.create_task(owner())
    caller_task = asyncio.create_task(caller())
    await entered.wait()
    await asyncio.sleep(0.05)
    assert not closed.is_set(), "closed the headful context while another task owned the lock"
    release.set()
    await asyncio.gather(owner_task, caller_task)
    assert closed.is_set()
    service._close.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_ensure_headless_session_nested_in_owner_lock(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.is_currently_headless = False
    service.context = Mock()
    service._close = AsyncMock()
    async with service.lock:
        await asyncio.wait_for(service.ensure_headless_session(), timeout=1)
    service._close.assert_awaited_once()
    assert service.force_headful_session is False


@pytest.mark.anyio
@pytest.mark.parametrize("service_type", SERVICES)
async def test_elevate_to_headful_waits_for_a_foreign_lock_owner(service_type, tmp_path):
    service = service_type(profile=tmp_path)
    service.is_currently_headless = True
    service._close = AsyncMock()
    service._ensure_open_unlocked = AsyncMock(return_value=Mock())
    service.inspect = AsyncMock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def owner():
        async with service.lock:
            entered.set()
            await release.wait()

    async def caller():
        await entered.wait()
        await service.elevate_to_headful(persist=False)

    owner_task = asyncio.create_task(owner())
    caller_task = asyncio.create_task(caller())
    await entered.wait()
    await asyncio.sleep(0.05)
    service._close.assert_not_awaited()
    service._ensure_open_unlocked.assert_not_awaited()
    release.set()
    await asyncio.gather(owner_task, caller_task)
    service._close.assert_awaited_once()
    service._ensure_open_unlocked.assert_awaited_once()


class _StopSearch(Exception):
    pass


@pytest.mark.anyio
@pytest.mark.parametrize("provider_type", [DouyinProvider, XiaohongshuProvider])
async def test_search_mutates_the_session_only_while_owning_the_lock(provider_type):
    browser = Mock()
    browser.lock = OwnedLock()
    ownership: list[bool] = []

    async def headless():
        ownership.append(browser.lock.owned_by_current_task())
        raise _StopSearch

    async def release():
        ownership.append(browser.lock.owned_by_current_task())
        raise _StopSearch

    browser.ensure_headless_session = AsyncMock(side_effect=headless)
    browser.release_blocked_page = AsyncMock(side_effect=release)
    provider = provider_type(browser)
    with pytest.raises(_StopSearch):
        await provider.search("test query", 5, SearchFilters())
    assert ownership, "search never touched the session"
    assert all(ownership), "session was mutated without owning the browser lock"