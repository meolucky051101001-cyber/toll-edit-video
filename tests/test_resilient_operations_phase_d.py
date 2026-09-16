import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.models import JobVideo, SearchJob, SearchPlan
from backend.providers.base import SearchProvider
from backend.schemas.contracts import VideoResult
from backend.services.search_service import SearchService


def make_test_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.mark.anyio
async def test_platform_locks_concurrency():
    sessions = make_test_db()
    xhs_waiting = asyncio.Event()
    xhs_release = asyncio.Event()

    class MockXHSProvider(SearchProvider):
        async def search(self, query, limit, filters):
            xhs_waiting.set()
            await xhs_release.wait()
            return []

    class MockDouyinProvider(SearchProvider):
        async def search(self, query, limit, filters):
            return [
                VideoResult(
                    platform="douyin",
                    url="https://www.douyin.com/video/dy123",
                    title="Douyin fast video",
                    is_mock=True,
                )
            ]

    def factory(platform, is_mock):
        if platform == "xiaohongshu":
            return MockXHSProvider()
        return MockDouyinProvider()

    service = SearchService(sessions, factory)

    # 1. Start XHS job that will block waiting
    with sessions() as db:
        j_xhs = SearchJob(original_query="xhs query", platforms=["xiaohongshu"], requested_limit=10, is_mock=False)
        db.add(j_xhs)
        db.flush()
        db.add(SearchPlan(job_id=j_xhs.id, queries=["xhs query"]))
        db.commit()
        xhs_job_id = j_xhs.id

    t_xhs = asyncio.create_task(service.run(xhs_job_id))
    await xhs_waiting.wait()

    # XHS is now holding the xhs platform lock!
    assert service.get_platform_lock("xiaohongshu").locked()
    assert not service.get_platform_lock("douyin").locked()

    # 2. Now start Douyin job concurrently
    with sessions() as db:
        j_dy = SearchJob(original_query="douyin query", platforms=["douyin"], requested_limit=10, is_mock=False)
        db.add(j_dy)
        db.flush()
        db.add(SearchPlan(job_id=j_dy.id, queries=["douyin query"]))
        db.commit()
        dy_job_id = j_dy.id

    # Douyin job MUST complete without waiting for XHS!
    await asyncio.wait_for(service.run(dy_job_id), timeout=2.0)

    with sessions() as db:
        dy_job = db.get(SearchJob, dy_job_id)
        assert dy_job.status == "completed"
        assert dy_job.processed_count == 1

    # Cleanup XHS job
    xhs_release.set()
    await t_xhs


@pytest.mark.anyio
async def test_cancel_retains_committed_batches():
    sessions = make_test_db()

    with sessions() as db:
        job = SearchJob(original_query="test cancel", platforms=["mock"], requested_limit=10, is_mock=True)
        db.add(job)
        db.commit()
        job_id = job.id

    service = SearchService(sessions)

    # Persist 2 videos
    results = [
        VideoResult(platform="douyin", url="https://douyin.com/v1", title="Video 1", is_mock=True),
        VideoResult(platform="douyin", url="https://douyin.com/v2", title="Video 2", is_mock=True),
    ]
    await service.persist_results(job_id, "douyin", "test cancel", results, 10)

    # Cancel the job
    await service.cancel(job_id)

    with sessions() as db:
        saved_job = db.get(SearchJob, job_id)
        assert saved_job.status == "cancelled"
        assert saved_job.processed_count == 2
        assert "Đã lưu an toàn 2 video" in saved_job.error_message

        # Verify the 2 videos exist in database
        links = db.query(JobVideo).filter_by(job_id=job_id).all()
        assert len(links) == 2


def test_recover_interrupted_with_partial_results():
    sessions = make_test_db()

    with sessions() as db:
        job_with_videos = SearchJob(
            original_query="interrupted partial",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="searching",
            processed_count=5,
        )
        job_no_videos = SearchJob(
            original_query="interrupted zero",
            platforms=["douyin"],
            requested_limit=10,
            status="searching",
            processed_count=0,
        )
        db.add_all([job_with_videos, job_no_videos])
        db.commit()
        id1, id2 = job_with_videos.id, job_no_videos.id

    service = SearchService(sessions)
    service.recover_interrupted()

    with sessions() as db:
        j1 = db.get(SearchJob, id1)
        assert j1.status == "interrupted"
        assert "Đã lưu an toàn 5/10 video" in j1.error_message

        j2 = db.get(SearchJob, id2)
        assert j2.status == "failed"
        assert "chạy lại từ Lịch sử" in j2.error_message
