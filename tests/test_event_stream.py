import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.events import event_generator, sse_stream
from backend.core.database import Base
from backend.core.events import EventBus
from backend.models.search_job import SearchJob
from backend.models.video import Video
from backend.services.download.base import BaseDownloadAdapter
from backend.services.download.service import DownloadService
from backend.services.search_service import SearchService


@pytest.fixture
def clean_bus():
    bus = EventBus(max_subscriber_queue=10)
    yield bus
    bus.clear()


@pytest.mark.anyio
async def test_event_bus_basic_publish_subscribe(clean_bus):
    sub1 = clean_bus.subscribe()
    sub2 = clean_bus.subscribe()

    assert clean_bus.subscriber_count == 2

    # Publish an event
    await clean_bus.publish("test:greeting", {"msg": "hello world"})

    ev1 = await asyncio.wait_for(sub1.get(), timeout=1.0)
    ev2 = await asyncio.wait_for(sub2.get(), timeout=1.0)

    assert ev1.id == 1
    assert ev1.event == "test:greeting"
    assert ev1.data == {"msg": "hello world"}

    assert ev2.id == 1
    assert ev2.event == "test:greeting"
    assert ev2.data == {"msg": "hello world"}

    await sub1.aclose()
    await sub2.aclose()

    assert clean_bus.subscriber_count == 0


@pytest.mark.anyio
async def test_event_bus_threadsafe_publish_nowait(clean_bus):
    sub = clean_bus.subscribe()

    clean_bus.publish_nowait("sync:ping", {"val": 99})

    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev.event == "sync:ping"
    assert ev.data == {"val": 99}

    await sub.aclose()


@pytest.mark.anyio
async def test_event_bus_queue_overflow_drops_oldest(clean_bus):
    # Bus has max_subscriber_queue = 10
    sub = clean_bus.subscribe()

    # Publish 15 events without consuming
    for i in range(15):
        clean_bus.publish_nowait("bulk:event", {"num": i})

    # The subscriber queue should have dropped the oldest 5 items and preserved the latest 10
    ev_first = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_first.data["num"] == 5  # 0..4 were dropped!

    await sub.aclose()


@pytest.mark.anyio
async def test_sse_generator_handshake_and_events():
    bus = EventBus()
    req = AsyncMock()
    gen = event_generator(req, bus=bus)

    # 1. First yielded chunk is initial connected handshake
    handshake = await gen.__anext__()
    assert b"event: connected" in handshake
    assert b'"status": "connected"' in handshake
    assert bus.subscriber_count == 1

    # 2. Publish search:update
    await bus.publish("search:update", {"job_id": "job-sse-1", "status": "searching"})
    ev1_bytes = await gen.__anext__()
    ev1_text = ev1_bytes.decode("utf-8")
    assert "event: search:update" in ev1_text
    assert '"job_id": "job-sse-1"' in ev1_text
    assert '"status": "searching"' in ev1_text

    # 3. Publish download:update
    await bus.publish("download:update", {"download_id": "dl-1", "status": "downloading", "progress_percent": 45.5})
    ev2_bytes = await gen.__anext__()
    ev2_text = ev2_bytes.decode("utf-8")
    assert "event: download:update" in ev2_text
    assert '"download_id": "dl-1"' in ev2_text
    assert '"progress_percent": 45.5' in ev2_text

    # 4. Closing generator unregisters subscriber
    await gen.aclose()
    assert bus.subscriber_count == 0


@pytest.mark.anyio
async def test_sse_generator_keepalive_timeout():
    bus = EventBus()
    req = AsyncMock()
    gen = event_generator(req, bus=bus)

    # Read handshake
    await gen.__anext__()

    # Simulate timeout on wait_for
    async def mock_wait_for(coro, timeout=None):
        coro.close()
        raise asyncio.TimeoutError

    with patch("asyncio.wait_for", side_effect=mock_wait_for):
        keepalive = await gen.__anext__()
        assert keepalive == b": keepalive\n\n"

    await gen.aclose()


@pytest.mark.anyio
async def test_sse_endpoint_response_headers_and_status():
    req = MagicMock()
    test_bus = EventBus()
    req.app.state.event_bus = test_bus

    response = await sse_stream(req)
    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache, no-transform"
    assert response.headers["Connection"] == "keep-alive"
    assert response.headers["X-Accel-Buffering"] == "no"


@pytest.mark.anyio
async def test_search_service_dispatches_events(clean_bus):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    sub = clean_bus.subscribe()

    with sessions() as db:
        job = SearchJob(
            id="job-sse-test",
            original_query="test query",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="pending",
        )
        db.add(job)
        db.commit()

    service = SearchService(
        sessions=sessions,
        event_bus=clean_bus,
    )

    # 1. Update status to searching
    service.update("job-sse-test", status="searching", processed_count=3)
    ev1 = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev1.event == "search:update"
    assert ev1.data["job_id"] == "job-sse-test"
    assert ev1.data["status"] == "searching"
    assert ev1.data["processed_count"] == 3

    # 2. Cancel job
    await service.cancel("job-sse-test")
    ev2 = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev2.event == "search:update"
    assert ev2.data["job_id"] == "job-sse-test"
    assert ev2.data["status"] == "cancelled"

    await sub.aclose()


@pytest.mark.anyio
async def test_download_service_dispatches_events(clean_bus, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    sub = clean_bus.subscribe()

    class DummyAdapter(BaseDownloadAdapter):
        @property
        def name(self) -> str:
            return "Dummy"

        @property
        def version(self) -> str:
            return "1.0"

        async def health_check(self) -> bool:
            return True

        async def resolve(self, video: Video):
            raise NotImplementedError

    service = DownloadService(
        sessions=sessions,
        xhs_adapter=DummyAdapter(),
        douyin_adapter=DummyAdapter(),
        download_dir=tmp_path,
        event_bus=clean_bus,
    )

    with sessions() as db:
        v = Video(
            id="vid-sse-1",
            platform="xiaohongshu",
            platform_video_id="sse_1",
            url="https://www.xiaohongshu.com/explore/sse1",
            canonical_url="https://www.xiaohongshu.com/explore/sse1",
            fingerprint="fp-sse",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    # 1. Request download -> queued event
    job_out = service.request_download("vid-sse-1")
    ev_queued = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_queued.event == "download:update"
    assert ev_queued.data["download_id"] == job_out.id
    assert ev_queued.data["status"] == "queued"

    # 2. Update status to validating
    service._update_job_status(job_out.id, "validating")
    ev_val = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_val.event == "download:update"
    assert ev_val.data["download_id"] == job_out.id
    assert ev_val.data["status"] == "validating"

    # 3. Update status to downloading and progress
    service._update_job_status(job_out.id, "downloading")
    ev_dl = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_dl.data["status"] == "downloading"

    service._update_job_progress(job_out.id, bytes_downloaded=5000, total_bytes=10000)
    ev_prog = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_prog.event == "download:update"
    assert ev_prog.data["bytes_downloaded"] == 5000
    assert ev_prog.data["total_bytes"] == 10000
    assert ev_prog.data["progress_percent"] == 50.0

    # 4. Fail job
    service._fail_job(job_out.id, "test_error", "Lỗi kiểm thử")
    ev_fail = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev_fail.event == "download:update"
    assert ev_fail.data["status"] == "failed"
    assert ev_fail.data["error_code"] == "test_error"

    await sub.aclose()
