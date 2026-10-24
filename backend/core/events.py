import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("events")

#: Every named SSE event type published through the EventBus. The /api/events
#: stream advertises this list in its `connected` payload so frontend listeners
#: can register themselves dynamically (a new backend event then needs no
#: frontend change). Keep in sync with publish()/publish_nowait() call sites.
EVENT_TYPES: frozenset[str] = frozenset({
    "search:update",
    "download:update",
    "dubbing:update",
    "video:metrics_update",
    "video:virality_update",
})


@dataclass(frozen=True)
class ServerEvent:
    id: int
    event: str
    data: dict[str, Any]
    timestamp: float = field(default_factory=time.time)

    def to_sse_bytes(self) -> bytes:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.event}\ndata: {payload}\n\n".encode("utf-8")


class EventSubscription:
    """Subscription handle that immediately registers with the EventBus upon creation."""

    def __init__(self, bus: "EventBus", queue: asyncio.Queue[ServerEvent]):
        self.bus = bus
        self.queue = queue
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> ServerEvent:
        if self._closed:
            raise StopAsyncIteration
        return await self.queue.get()

    async def get(self) -> ServerEvent:
        return await self.queue.get()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self.bus._subscribers.discard(self.queue)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.aclose()


class EventBus:
    """Thread-safe and async in-memory publish-subscribe event bus for Server-Sent Events (SSE)."""

    def __init__(self, max_subscriber_queue: int = 256):
        self.max_subscriber_queue = max_subscriber_queue
        self._subscribers: set[asyncio.Queue[ServerEvent]] = set()
        self._counter: int = 0
        self._id_lock = threading.Lock()
        # Loop captured the first time we publish from async context; used to
        # hand events raised from worker threads into the loop thread safely.
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _next_id(self) -> int:
        with self._id_lock:
            self._counter += 1
            return self._counter

    def _dispatch_to_subscribers(self, event: ServerEvent) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Discard oldest event for slow subscriber to avoid unbounded buffer growth
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    async def publish(self, event_type: str, data: dict[str, Any]) -> ServerEvent:
        self._loop = asyncio.get_running_loop()
        event = ServerEvent(
            id=self._next_id(),
            event=event_type,
            data=data,
        )
        self._dispatch_to_subscribers(event)
        return event

    def publish_nowait(self, event_type: str, data: dict[str, Any]) -> None:
        """Publishes an event synchronously or from background thread/worker contexts."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        event = ServerEvent(
            id=self._next_id(),
            event=event_type,
            data=data,
        )

        if loop and loop.is_running():
            # Same-thread dispatch is safe; remember the loop for later
            # cross-thread publishers.
            self._loop = loop
            self._dispatch_to_subscribers(event)
            return

        if self._loop is not None and self._loop.is_running():
            # asyncio.Queue is not thread-safe: marshal into the loop thread
            # instead of touching subscriber queues from a worker thread.
            self._loop.call_soon_threadsafe(self._dispatch_to_subscribers, event)
            return

        logger.warning(
            "Dropped event %r: no running event loop to dispatch on.", event_type
        )

    def subscribe(self) -> EventSubscription:
        """Subscribes immediately to events, registering an internal queue synchronously."""
        q: asyncio.Queue[ServerEvent] = asyncio.Queue(maxsize=self.max_subscriber_queue)
        self._subscribers.add(q)
        return EventSubscription(self, q)

    def clear(self) -> None:
        """Clears all subscribers and resets the event counter (primarily for testing)."""
        self._subscribers.clear()
        self._counter = 0


# Global singleton instance
event_bus = EventBus()


def get_event_bus() -> EventBus:
    return event_bus
