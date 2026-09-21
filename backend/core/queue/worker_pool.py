import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from backend.core.queue.base import BaseJobQueue

logger = logging.getLogger("queue.worker_pool")


class AdaptiveWorkerPool:
    """Dynamically scales worker coroutines to process queued background tasks."""

    def __init__(
        self,
        queue: BaseJobQueue,
        min_workers: int = 2,
        max_workers: int = 8,
    ):
        self.queue = queue
        self.min_workers = min_workers
        self.max_workers = max_workers
        self.handlers: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._next_worker_id = 0
        self._running = False
        self._supervisor_task: asyncio.Task | None = None
        self._idle_ticks = 0

    def register_handler(
        self, task_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        self.handlers[task_type] = handler
        logger.info("Registered queue handler for task type: %s", task_type)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for _ in range(self.min_workers):
            self._spawn_worker()
        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        logger.info("Started AdaptiveWorkerPool with %d initial workers (max: %d)", self.min_workers, self.max_workers)

    def _spawn_worker(self) -> int:
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        task = asyncio.create_task(self._worker_loop(worker_id))
        self._workers[worker_id] = task
        logger.debug("Spawned worker #%d (total active: %d)", worker_id, len(self._workers))
        return worker_id

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
        for worker in self._workers.values():
            worker.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        logger.info("Stopped AdaptiveWorkerPool")

    async def _supervisor_loop(self) -> None:
        """Dynamically scales workers up during high load and down when idle."""
        while self._running:
            try:
                await asyncio.sleep(2.0)
                stats = self.queue.get_stats()
                pending = stats.get("pending", 0)

                # Scale up if backlog exceeds current active worker capacity
                if pending > len(self._workers) and len(self._workers) < self.max_workers:
                    workers_to_add = min(self.max_workers - len(self._workers), (pending + 1) // 2)
                    for _ in range(workers_to_add):
                        self._spawn_worker()
                    self._idle_ticks = 0
                    logger.info("High queue backlog (%d pending). Scaled up to %d workers", pending, len(self._workers))
                elif pending == 0 and len(self._workers) > self.min_workers:
                    self._idle_ticks += 1
                    # After 3 consecutive idle checks (~6s), scale down one worker
                    if self._idle_ticks >= 3:
                        worker_id = next(iter(self._workers))
                        task = self._workers.pop(worker_id)
                        task.cancel()
                        self._idle_ticks = 0
                        logger.debug("Queue idle. Scaled down to %d workers", len(self._workers))
                else:
                    self._idle_ticks = 0

                # Clean up any dead tasks
                dead_ids = [wid for wid, t in self._workers.items() if t.done()]
                for wid in dead_ids:
                    self._workers.pop(wid, None)
                    if self._running and len(self._workers) < self.min_workers:
                        self._spawn_worker()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Supervisor loop error: %s", exc)

    async def _worker_loop(self, worker_id: int) -> None:
        logger.debug("Worker #%d started", worker_id)
        while self._running:
            try:
                item = await self.queue.dequeue(timeout=1.0)
                if item is None:
                    continue

                handler = self.handlers.get(item.task_type)
                if not handler:
                    logger.error("No handler registered for task type: %s", item.task_type)
                    await self.queue.dead_letter(item.id, f"Unregistered task type '{item.task_type}'")
                    continue

                try:
                    await handler(item.payload)
                    await self.queue.ack(item.id)
                except Exception as exc:
                    logger.warning("Worker #%d failed processing task %s: %s", worker_id, item.id, exc)
                    await self.queue.nack(item.id, str(exc))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected worker #%d error: %s", worker_id, exc)
                await asyncio.sleep(0.5)

    def get_status(self) -> dict[str, Any]:
        return {
            "is_running": self._running,
            "active_workers": len(self._workers),
            "min_workers": self.min_workers,
            "max_workers": self.max_workers,
            "registered_task_types": list(self.handlers.keys()),
            "queue_stats": self.queue.get_stats(),
        }
