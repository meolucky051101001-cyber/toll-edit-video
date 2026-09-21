from typing import Any

from fastapi import APIRouter, Query

from backend.core.queue import system_queue, system_worker_pool

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/queue-status")
def get_queue_status() -> dict[str, Any]:
    """Returns real-time status of the asynchronous job queue and worker pool."""
    return system_worker_pool.get_status()


@router.get("/dead-letter")
def get_dead_letter_queue(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Inspects failed tasks quarantined in the Dead Letter Queue."""
    return system_queue.inspect_dead_letter(limit=limit)
