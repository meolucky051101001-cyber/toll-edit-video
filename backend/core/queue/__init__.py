from backend.core.queue.base import BaseJobQueue, QueueItem
from backend.core.queue.memory_queue import InMemoryPriorityQueue, QueueFullError
from backend.core.queue.worker_pool import AdaptiveWorkerPool

# Global singleton queue and worker pool instances
system_queue = InMemoryPriorityQueue(max_capacity=5000)
system_worker_pool = AdaptiveWorkerPool(queue=system_queue, min_workers=2, max_workers=8)

__all__ = [
    "BaseJobQueue",
    "QueueItem",
    "InMemoryPriorityQueue",
    "QueueFullError",
    "AdaptiveWorkerPool",
    "system_queue",
    "system_worker_pool",
]
