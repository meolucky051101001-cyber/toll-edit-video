import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("download.queue")


class DownloadQueueManager:
    """Bounded queue manager with fixed worker pool and backpressure handling for video downloads."""

    def __init__(
        self,
        handler: Callable[[str], Awaitable[None]],
        capacity: int = 100,
        worker_count: int = 2,
    ):
        self.handler = handler
        self.capacity = max(1, capacity)
        self.worker_count = max(1, worker_count)
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self._running: bool = False

    @property
    def queue(self) -> asyncio.Queue[str]:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.capacity)
        return self._queue

    @property
    def is_running(self) -> bool:
        return self._running

    def qsize(self) -> int:
        return self.queue.qsize()

    async def start(self) -> None:
        """Starts the fixed worker pool in the active event loop."""
        if self._running:
            return

        # Ensure queue belongs to current event loop
        loop = asyncio.get_running_loop()
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self.capacity)

        self._running = True
        self._workers = []
        for i in range(self.worker_count):
            worker_task = loop.create_task(
                self._worker_loop(worker_id=i + 1),
                name=f"download-worker-{i + 1}",
            )
            self._workers.append(worker_task)

        logger.info(
            "DownloadQueueManager started with %d workers (queue capacity: %d).",
            self.worker_count,
            self.capacity,
        )

    async def stop(self) -> None:
        """Gracefully cancels all workers and drains queue tasks."""
        if not self._running:
            return

        self._running = False
        logger.info("Stopping DownloadQueueManager (%d active workers)...", len(self._workers))

        for worker in self._workers:
            if not worker.done():
                worker.cancel()

        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

        logger.info("DownloadQueueManager stopped.")

    async def enqueue(self, job_id: str, timeout: float = 0.5) -> bool:
        """Enqueues a job_id with bounded backpressure.

        Returns True if enqueued successfully, or False if queue is saturated within timeout.
        """
        try:
            await asyncio.wait_for(self.queue.put(job_id), timeout=timeout)
            return True
        except (asyncio.TimeoutError, asyncio.QueueFull):
            logger.warning(
                "Backpressure triggered: download queue is full (size=%d, capacity=%d). Rejected job %s.",
                self.qsize(),
                self.capacity,
                job_id,
            )
            return False

    def _ensure_running(self) -> None:
        """Lazy auto-start for environments without lifespan management (e.g. ad-hoc unit tests)."""
        if not self._running:
            try:
                loop = asyncio.get_running_loop()
                self._running = True
                self._workers = []
                for i in range(self.worker_count):
                    t = loop.create_task(
                        self._worker_loop(worker_id=i + 1),
                        name=f"download-worker-{i + 1}",
                    )
                    self._workers.append(t)
            except RuntimeError:
                pass

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker routine that pulls jobs sequentially from the bounded queue."""
        logger.debug("Download worker %d started.", worker_id)
        while self._running:
            try:
                job_id = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Unexpected error waiting on queue in worker %d", worker_id)
                await asyncio.sleep(0.1)
                continue

            try:
                await self.handler(job_id)
            except asyncio.CancelledError:
                logger.debug("Download worker %d task cancelled for job %s", worker_id, job_id)
                break
            except Exception:
                logger.exception(
                    "Fault isolation: worker %d encountered unhandled exception processing job %s",
                    worker_id,
                    job_id,
                )
            finally:
                self.queue.task_done()

        logger.debug("Download worker %d exited.", worker_id)
