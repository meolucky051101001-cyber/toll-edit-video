import asyncio

import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.core.queue import (
    AdaptiveWorkerPool,
    InMemoryPriorityQueue,
    QueueFullError,
)


@pytest.mark.anyio
async def test_priority_queue_ordering():
    q = InMemoryPriorityQueue(max_capacity=100)

    # Enqueue in mixed order: standard (5), urgent (1), low (10)
    await q.enqueue("task_standard", {"val": "normal"}, priority=5)
    await q.enqueue("task_urgent", {"val": "vip"}, priority=1)
    await q.enqueue("task_low", {"val": "bg"}, priority=10)

    # Should dequeue priority 1 first
    first = await q.dequeue()
    assert first is not None
    assert first.task_type == "task_urgent"
    assert first.priority == 1
    await q.ack(first.id)

    # Then priority 5
    second = await q.dequeue()
    assert second is not None
    assert second.task_type == "task_standard"
    await q.ack(second.id)

    # Finally priority 10
    third = await q.dequeue()
    assert third is not None
    assert third.task_type == "task_low"
    await q.ack(third.id)


@pytest.mark.anyio
async def test_queue_max_capacity_backpressure():
    small_q = InMemoryPriorityQueue(max_capacity=2)

    await small_q.enqueue("t1", {})
    await small_q.enqueue("t2", {})

    with pytest.raises(QueueFullError, match="giới hạn tối đa"):
        await small_q.enqueue("t3", {})


@pytest.mark.anyio
async def test_nack_and_dead_letter_queue_routing():
    q = InMemoryPriorityQueue(max_capacity=50)

    task_id = await q.enqueue("failing_task", {"info": "poison"}, max_retries=2)

    # Attempt 1
    item = await q.dequeue()
    assert item is not None
    assert item.id == task_id
    await q.nack(item.id, "Lỗi lần 1")

    # Wait for retry backoff
    await asyncio.sleep(0.3)

    # Attempt 2 (reaches max_retries=2)
    item_retry = await q.dequeue()
    assert item_retry is not None
    assert item_retry.id == task_id
    await q.nack(item_retry.id, "Lỗi lần 2 - hết lượt")

    # Item should now be in Dead Letter Queue
    stats = q.get_stats()
    assert stats["dead_letter"] == 1
    assert stats["in_flight"] == 0
    assert stats["pending"] == 0

    dlq = q.inspect_dead_letter()
    assert len(dlq) == 1
    assert dlq[0]["id"] == task_id
    assert "Lỗi lần 2" in dlq[0]["last_error"]


@pytest.mark.anyio
async def test_worker_pool_processing_and_graceful_shutdown():
    q = InMemoryPriorityQueue(max_capacity=100)
    pool = AdaptiveWorkerPool(queue=q, min_workers=2, max_workers=4)

    processed = []

    async def my_handler(payload):
        processed.append(payload["msg"])

    pool.register_handler("say_hello", my_handler)
    await pool.start()

    await q.enqueue("say_hello", {"msg": "hello_1"})
    await q.enqueue("say_hello", {"msg": "hello_2"})
    await q.enqueue("say_hello", {"msg": "hello_3"})

    # Allow workers to pick up items
    for _ in range(50):
        if len(processed) >= 3:
            break
        await asyncio.sleep(0.05)

    assert len(processed) == 3
    assert "hello_1" in processed
    assert "hello_2" in processed
    assert "hello_3" in processed

    status = pool.get_status()
    assert status["is_running"] is True
    assert status["active_workers"] == 2

    await pool.stop()
    status_stopped = pool.get_status()
    assert status_stopped["is_running"] is False
    assert status_stopped["active_workers"] == 0


def test_system_api_endpoints():
    with TestClient(app) as client:
        res = client.get("/api/system/queue-status")
        assert res.status_code == 200
        data = res.json()
        assert "queue_stats" in data
        assert "active_workers" in data
        assert data["queue_stats"]["max_capacity"] >= 5000

        res_dlq = client.get("/api/system/dead-letter")
        assert res_dlq.status_code == 200
        assert isinstance(res_dlq.json(), list)
