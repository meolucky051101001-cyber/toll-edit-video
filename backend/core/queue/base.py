import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class QueueItem:
    task_type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    priority: int = 5  # Lower number = higher priority (1=Urgent, 5=Standard, 10=Background)
    retry_count: int = 0
    max_retries: int = 3
    backoff_factor: float = 1.5
    created_at: float = field(default_factory=time.time)
    last_error: str | None = None
    status: str = "pending"  # pending, processing, completed, failed, dead_letter

    def __lt__(self, other: "QueueItem") -> bool:
        # Heap comparison: prioritize by priority then by created_at (FIFO within same priority)
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.created_at < other.created_at


class BaseJobQueue(ABC):
    @abstractmethod
    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        priority: int = 5,
        max_retries: int = 3,
        item_id: str | None = None,
    ) -> str:
        """Enqueues a task item. Returns task ID."""
        pass

    @abstractmethod
    async def dequeue(self, timeout: float | None = None) -> QueueItem | None:
        """Pops the highest priority available task item."""
        pass

    @abstractmethod
    async def ack(self, item_id: str) -> None:
        """Acknowledges successful task completion."""
        pass

    @abstractmethod
    async def nack(self, item_id: str, error: str | None = None) -> None:
        """Marks task failure, increments retry count, and requeues or moves to DLQ."""
        pass

    @abstractmethod
    async def dead_letter(self, item_id: str, reason: str) -> None:
        """Directly relocates an item to the Dead Letter Queue."""
        pass

    @abstractmethod
    def get_stats(self) -> dict[str, int]:
        """Returns statistics of pending, processing, completed, failed, and dlq items."""
        pass

    @abstractmethod
    async def cancel(self, item_id: str) -> bool:
        """Cancels a pending task."""
        pass
