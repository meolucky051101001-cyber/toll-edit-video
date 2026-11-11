"""Browser lock with real task ownership tracking.

asyncio.Lock.locked() only says *someone* holds the lock, not that the
current caller does. Helpers that skip re-acquisition must check task
identity, otherwise they mutate the browser while another task owns it.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType


class OwnedLock:
    """asyncio.Lock-compatible wrapper that records the owning task."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None

    def locked(self) -> bool:
        return self._lock.locked()

    def owned_by_current_task(self) -> bool:
        return self._owner is not None and self._owner is asyncio.current_task()

    async def acquire(self) -> bool:
        await self._lock.acquire()
        self._owner = asyncio.current_task()
        return True

    def release(self) -> None:
        self._owner = None
        self._lock.release()

    async def __aenter__(self) -> OwnedLock:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    @asynccontextmanager
    async def serialized(self) -> AsyncIterator[None]:
        """Acquire the lock unless the current task already owns it.

        For methods that run both standalone and nested inside a caller
        that already holds the lock (asyncio.Lock is not reentrant).
        """
        if self.owned_by_current_task():
            yield
            return
        async with self:
            yield