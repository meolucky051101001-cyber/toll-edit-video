import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.config import settings
from backend.core.database import Base
from backend.models.download_job import DownloadJob
from backend.models.video import Video
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.base import BaseDownloadAdapter
from backend.services.download.queue_manager import DownloadQueueManager
from backend.services.download.service import DownloadService


class MockAdapter(BaseDownloadAdapter):
    def __init__(self, media_url="https://sns-video-bd.xhscdn.com/stream/mock.mp4"):
        self._media_url = media_url

    @property
    def name(self) -> str:
        return "MockQueueAdapter"

    @property
    def version(self) -> str:
        return "mock:1.0"

    async def health_check(self) -> bool:
        return True

    async def resolve(self, video: Video) -> ResolvedMedia:
        return ResolvedMedia(
            platform=video.platform,  # type: ignore[arg-type]
            video_id=video.platform_video_id or "vid",
            media_url=self._media_url,
            headers={"User-Agent": "TestUA"},
            format="mp4",
            adapter_name=self.name,
            adapter_version=self.version,
        )


@pytest.fixture
def queue_test_env(tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True, exist_ok=True)

    adapter = MockAdapter()
    service = DownloadService(
        sessions,
        xhs_adapter=adapter,
        douyin_adapter=adapter,
        download_dir=dl_dir,
        queue_capacity=5,
        worker_count=2,
        max_retries=2,
        retry_base_delay=0.1,
    )

    yield sessions, service, dl_dir


def test_download_queue_settings_defaults():
    assert settings.download_queue_capacity >= 10
    assert settings.download_worker_count >= 1
    assert settings.download_max_retries >= 1
    assert settings.download_retry_base_delay > 0


@pytest.mark.anyio
async def test_download_queue_manager_start_stop():
    handled = []

    async def dummy_handler(job_id: str):
        handled.append(job_id)

    mgr = DownloadQueueManager(handler=dummy_handler, capacity=3, worker_count=2)
    assert not mgr.is_running
    assert mgr.qsize() == 0

    await mgr.start()
    assert mgr.is_running
    assert len(mgr._workers) == 2

    # Enqueue items
    ok1 = await mgr.enqueue("job-1")
    ok2 = await mgr.enqueue("job-2")
    assert ok1 and ok2

    await asyncio.sleep(0.1)
    await mgr.stop()
    assert not mgr.is_running
    assert "job-1" in handled
    assert "job-2" in handled


@pytest.mark.anyio
async def test_bounded_queue_backpressure_rejection():
    async def slow_handler(job_id: str):
        await asyncio.sleep(2.0)

    mgr = DownloadQueueManager(handler=slow_handler, capacity=2, worker_count=1)
    # Put 2 items to fill capacity
    mgr.queue.put_nowait("fill-1")
    mgr.queue.put_nowait("fill-2")
    assert mgr.qsize() == 2

    # 3rd enqueue with timeout should fail immediately due to capacity limit
    success = await mgr.enqueue("overflow-3", timeout=0.05)
    assert success is False


def test_request_download_backpressure_marks_saturated(queue_test_env):
    sessions, service, dl_dir = queue_test_env

    # Fill queue to capacity (5)
    for i in range(service.queue_capacity):
        service.queue_manager.queue.put_nowait(f"dummy-{i}")

    with sessions() as db:
        v = Video(
            id="vid-sat-1",
            platform="xiaohongshu",
            platform_video_id="sat_1",
            url="https://www.xiaohongshu.com/explore/sat1",
            canonical_url="https://www.xiaohongshu.com/explore/sat1",
            fingerprint="fp-sat",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    # When queue is saturated, request_download returns job with queue_saturated failure
    out = service.request_download("vid-sat-1")
    assert out.status == "failed"
    assert out.error_code == "queue_saturated"


@pytest.mark.anyio
async def test_full_state_transitions_including_validating(queue_test_env, monkeypatch):
    sessions, service, dl_dir = queue_test_env
    statuses_seen = []

    # Intercept status updates to record state progression
    orig_update = service._update_job_status

    def tracking_update(job_id, status, adapter_version=None):
        statuses_seen.append(status)
        return orig_update(job_id, status, adapter_version=adapter_version)

    service._update_job_status = tracking_update

    # Mock valid tiny MP4 streaming response using real fixture bytes
    tiny_mp4 = Path("tests/fixtures/tiny_valid.mp4").read_bytes()

    class MockTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(tiny_mp4)), "Content-Type": "video/mp4"},
                content=tiny_mp4,
            )

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client",
        lambda timeout: httpx.AsyncClient(transport=MockTransport(), timeout=timeout),
    )

    with sessions() as db:
        v = Video(
            id="vid-state-1",
            platform="xiaohongshu",
            platform_video_id="state_1",
            url="https://www.xiaohongshu.com/explore/state1",
            canonical_url="https://www.xiaohongshu.com/explore/state1",
            fingerprint="fp-state",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    job = service.request_download("vid-state-1")
    assert job.status == "queued"

    # Start worker and process (job was already enqueued by request_download)
    await service.start()

    # Await completion
    for _ in range(120):
        await asyncio.sleep(0.05)
        out = service.get_job_by_id(job.id)
        if out and out.status in ("completed", "failed", "cancelled"):
            break

    await service.shutdown()

    out = service.get_job_by_id(job.id)
    assert out.status == "completed"
    assert "resolving" in statuses_seen
    assert "downloading" in statuses_seen
    assert "validating" in statuses_seen


@pytest.mark.anyio
async def test_transient_error_enters_retry_wait_and_succeeds(queue_test_env, monkeypatch):
    sessions, service, dl_dir = queue_test_env
    attempts = 0
    tiny_mp4 = Path("tests/fixtures/tiny_valid.mp4").read_bytes()

    class TransientFailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                # 1st attempt: transient 503 error
                return httpx.Response(503, content=b"Service Temporarily Overloaded")
            # 2nd attempt: success
            return httpx.Response(
                200,
                headers={"Content-Length": str(len(tiny_mp4)), "Content-Type": "video/mp4"},
                content=tiny_mp4,
            )

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client",
        lambda timeout: httpx.AsyncClient(transport=TransientFailingTransport(), timeout=timeout),
    )

    with sessions() as db:
        v = Video(
            id="vid-retry-1",
            platform="xiaohongshu",
            platform_video_id="retry_1",
            url="https://www.xiaohongshu.com/explore/retry1",
            canonical_url="https://www.xiaohongshu.com/explore/retry1",
            fingerprint="fp-retry",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    job = service.request_download("vid-retry-1")
    await service.start()

    for _ in range(120):
        await asyncio.sleep(0.05)
        out = service.get_job_by_id(job.id)
        if out and out.status in ("completed", "failed", "cancelled"):
            break

    await service.shutdown()

    out = service.get_job_by_id(job.id)
    assert out.status == "completed"
    assert attempts == 2
    assert out.retry_count == 1


@pytest.mark.anyio
async def test_non_transient_error_fails_immediately_no_retry(queue_test_env, monkeypatch):
    sessions, service, dl_dir = queue_test_env
    attempts = 0

    class PermanentFailingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(404, content=b"Media Not Found")

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client",
        lambda timeout: httpx.AsyncClient(transport=PermanentFailingTransport(), timeout=timeout),
    )

    with sessions() as db:
        v = Video(
            id="vid-fail-1",
            platform="xiaohongshu",
            platform_video_id="fail_1",
            url="https://www.xiaohongshu.com/explore/fail1",
            canonical_url="https://www.xiaohongshu.com/explore/fail1",
            fingerprint="fp-fail",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    job = service.request_download("vid-fail-1")
    await service.start()

    for _ in range(80):
        await asyncio.sleep(0.05)
        out = service.get_job_by_id(job.id)
        if out and out.status in ("completed", "failed", "cancelled"):
            break

    await service.shutdown()

    out = service.get_job_by_id(job.id)
    assert out.status == "failed"
    assert out.error_code == "media_http_404"
    assert attempts == 1  # No retry attempted!


@pytest.mark.anyio
async def test_resumable_download_with_range_206(queue_test_env, monkeypatch):
    sessions, service, dl_dir = queue_test_env
    full_mp4 = Path("tests/fixtures/tiny_valid.mp4").read_bytes()
    total_len = len(full_mp4)
    first_chunk_len = len(full_mp4) // 2

    with sessions() as db:
        v = Video(
            id="vid-range-1",
            platform="xiaohongshu",
            platform_video_id="range_1",
            url="https://www.xiaohongshu.com/explore/range1",
            canonical_url="https://www.xiaohongshu.com/explore/range1",
            fingerprint="fp-range",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.commit()

    job = service.request_download("vid-range-1")

    # Simulate existing .part file from previous interrupted run
    part_file = service.temp_dir / f"{job.id}.part"
    part_file.write_bytes(full_mp4[:first_chunk_len])

    class RangeAwareTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            range_hdr = request.headers.get("Range")
            assert range_hdr == f"bytes={first_chunk_len}-"
            remaining = full_mp4[first_chunk_len:]
            return httpx.Response(
                206,
                headers={
                    "Content-Range": f"bytes {first_chunk_len}-{total_len - 1}/{total_len}",
                    "Content-Length": str(len(remaining)),
                    "Content-Type": "video/mp4",
                },
                content=remaining,
            )

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client",
        lambda timeout: httpx.AsyncClient(transport=RangeAwareTransport(), timeout=timeout),
    )

    await service.start()

    for _ in range(120):
        await asyncio.sleep(0.05)
        out = service.get_job_by_id(job.id)
        if out and out.status in ("completed", "failed", "cancelled"):
            break

    await service.shutdown()

    out = service.get_job_by_id(job.id)
    assert out.status == "completed"
    assert out.file_size == total_len

    with sessions() as db:
        db_job = db.get(DownloadJob, job.id)
        assert db_job is not None and db_job.relative_path is not None
        final_file = dl_dir / db_job.relative_path
        assert final_file.exists()
        assert final_file.read_bytes() == full_mp4


@pytest.mark.anyio
async def test_graceful_shutdown_marks_unfinished_jobs(queue_test_env):
    sessions, service, dl_dir = queue_test_env

    with sessions() as db:
        v = Video(
            id="vid-shut-test",
            platform="xiaohongshu",
            platform_video_id="shut_1",
            url="https://www.xiaohongshu.com/explore/shut1",
            canonical_url="https://www.xiaohongshu.com/explore/shut1",
            fingerprint="fp-shut2",
            search_query="test",
            search_job_id="job1",
            is_mock=False,
        )
        db.add(v)
        db.flush()

        j1 = DownloadJob(id="job-val-shut", video_id=v.id, platform="xiaohongshu", status="validating")
        j2 = DownloadJob(id="job-retry-shut", video_id=v.id, platform="xiaohongshu", status="retry_wait")
        db.add_all([j1, j2])
        db.commit()

    await service.shutdown()

    with sessions() as db:
        rec1 = db.get(DownloadJob, "job-val-shut")
        rec2 = db.get(DownloadJob, "job-retry-shut")
        assert rec1.status == "interrupted"
        assert rec1.error_code == "server_shutdown"
        assert rec2.status == "interrupted"
        assert rec2.error_code == "server_shutdown"
