import asyncio
import heapq
import logging
import time
from typing import Any

from backend.core.queue.base import BaseJobQueue, QueueItem

logger = logging.getLogger("queue.memory")


class QueueFullError(Exception):
    """Raised when the bounded queue reaches maximum capacity."""
    pass


class InMemoryPriorityQueue(BaseJobQueue):
    """Production-grade In-Memory Priority Job Queue with Dead Letter Queue and exponential backoff."""

    def __init__(self, max_capacity: int = 5000):
        self.max_capacity = max_capacity
        self._heap: list[QueueItem] = []
        self._in_flight: dict[str, QueueItem] = {}
        self._dead_letter_queue: list[QueueItem] = []
        self._lock = asyncio.Lock()
        self._notify_event = asyncio.Event()
        self._completed_count = 0
        self._failed_count = 0

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 5,
        max_retries: int = 3,
        item_id: str | None = None,
    ) -> str:
        async with self._lock:
            if len(self._heap) + len(self._in_flight) >= self.max_capacity:
                raise QueueFullError(f"Hàng đợi đã đạt giới hạn tối đa {self.max_capacity} tác vụ.")

            item = QueueItem(
                task_type=task_type,
                payload=payload,
                priority=priority,
                max_retries=max_retries,
            )
            if item_id:
                item.id = item_id

            heapq.heappush(self._heap, item)
            self._notify_event.set()
            logger.debug("Enqueued task %s (type=%s, priority=%d)", item.id, task_type, priority)
            return item.id

    async def dequeue(self, timeout: float | None = None) -> QueueItem | None:
        start_time = time.time()
        while True:
            async with self._lock:
                if self._heap:
                    item = heapq.heappop(self._heap)
                    item.status = "processing"
                    self._in_flight[item.id] = item
                    if not self._heap:
                        self._notify_event.clear()
                    return item

            # Wait for item to arrive if heap was empty
            remaining = None
            if timeout is not None:
                elapsed = time.time() - start_time
                remaining = max(0.0, timeout - elapsed)
                if remaining <= 0:
                    return None

            try:
                await asyncio.wait_for(self._notify_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def ack(self, item_id: str) -> None:
        async with self._lock:
            item = self._in_flight.pop(item_id, None)
            if item:
                item.status = "completed"
                self._completed_count += 1
                logger.debug("Acknowledged task %s", item_id)

    async def nack(self, item_id: str, error: str | None = None) -> None:
        async with self._lock:
            item = self._in_flight.pop(item_id, None)
            if not item:
                return

            item.retry_count += 1
            item.last_error = error

            if item.retry_count >= item.max_retries:
                item.status = "dead_letter"
                self._dead_letter_queue.append(item)
                self._failed_count += 1
                logger.warning(
                    "Task %s reached max retries (%d); moved to Dead Letter Queue: %s",
                    item_id, item.max_retries, error
                )
            else:
                # Exponential backoff delay
                delay = item.backoff_factor ** item.retry_count * 0.1
                logger.info("Task %s failed (retry %d/%d); will retry after %.2fs: %s", item_id, item.retry_count, item.max_retries, delay, error)

                async def _delayed_requeue(requeue_item: QueueItem, wait_sec: float):
                    await asyncio.sleep(wait_sec)
                    async with self._lock:
                        requeue_item.status = "pending"
                        heapq.heappush(self._heap, requeue_item)
                        self._notify_event.set()

                asyncio.create_task(_delayed_requeue(item, delay))

    async def dead_letter(self, item_id: str, reason: str) -> None:
        async with self._lock:
            item = self._in_flight.pop(item_id, None)
            if not item:
                # Check if still in heap
                for i, it in enumerate(self._heap):
                    if it.id == item_id:
                        item = self._heap.pop(i)
                        heapq.heapify(self._heap)
                        break

            if item:
                item.status = "dead_letter"
                item.last_error = reason
                self._dead_letter_queue.append(item)
                self._failed_count += 1
                logger.warning("Manually routed task %s to Dead Letter Queue: %s", item_id, reason)

    async def cancel(self, item_id: str) -> bool:
        async with self._lock:
            for i, it in enumerate(self._heap):
                if it.id == item_id:
                    self._heap.pop(i)
                    heapq.heapify(self._heap)
                    it.status = "cancelled"
                    return True
        return False

    def get_stats(self) -> dict[str, int]:
        return {
            "pending": len(self._heap),
            "in_flight": len(self._in_flight),
            "completed": self._completed_count,
            "failed": self._failed_count,
            "dead_letter": len(self._dead_letter_queue),
            "max_capacity": self.max_capacity,
        }

    def inspect_dead_letter(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "id": it.id,
                "task_type": it.task_type,
                "payload": it.payload,
                "retry_count": it.retry_count,
                "last_error": it.last_error,
                "created_at": it.created_at,
            }
            for it in self._dead_letter_queue[-limit:]
        ]
