"""Parallel multi-platform crawling and the query result cache."""

import asyncio
from datetime import timedelta

import pytest

from backend.core.config import settings
from backend.models import JobVideo, SearchJob, SearchPlan, Video
from backend.models.video import utcnow
from backend.schemas.contracts import VideoResult
from backend.services.search_service import SearchService

_FILTERS = {
    "minimum_likes": 0,
    "minimum_views": 0,
    "minimum_relevance": 40.0,
    "sort_by": "relevance",
    "publish_time": "all",
    "content_type": "all",
    "exact_hashtag": False,
    "target_hashtag": None,
}


def _dy(video_id: str) -> VideoResult:
    return VideoResult(
        platform="douyin",
        platform_video_id=video_id,
        title="书桌布置视频",
        url=f"https://www.douyin.com/video/{video_id}",
    )


def _xhs(video_id: str) -> VideoResult:
    return VideoResult(
        platform="xiaohongshu",
        platform_video_id=video_id,
        title="书桌布置视频",
        url=f"https://www.xiaohongshu.com/explore/{video_id}",
    )


def _add_video(db, video_id: str, job_id: str) -> None:
    db.add(
        Video(
            id=f"v_{video_id}",
            platform="douyin",
            platform_video_id=video_id,
            url=f"https://www.douyin.com/video/{video_id}",
            canonical_url=f"https://www.douyin.com/video/{video_id}",
            fingerprint=f"fp-{video_id}",
            title="书桌布置视频",
            search_query="decor bàn học",
            search_job_id=job_id,
        )
    )


# ---------------------------------------------------------------- parallel ---


@pytest.mark.anyio
async def test_dual_platform_crawls_concurrently(sessions):
    """Both providers must be inside search() at the same time, and both
    platforms' results must be persisted for the shared job."""
    job_id = "test-parallel-both"
    with sessions() as db:
        db.add(
            SearchJob(
                id=job_id,
                original_query="decor bàn học",
                status="pending",
                platforms=["douyin", "xiaohongshu"],
                requested_limit=4,
                is_mock=False,
                created_at=utcnow(),
            )
        )
        db.add(SearchPlan(job_id=job_id, queries=["书桌布置"], use_ai=False))
        db.commit()

    tracker = {"inside": 0, "max_inside": 0}

    class SlowProvider:
        capture_origin = "untrusted"

        def __init__(self, results):
            self.results = results

        async def search(self, term, limit, filters, on_batch=None):
            tracker["inside"] += 1
            tracker["max_inside"] = max(tracker["max_inside"], tracker["inside"])
            try:
                await asyncio.sleep(0.2)
            finally:
                tracker["inside"] -= 1
            return self.results

    dy = SlowProvider([_dy("dy_par_001"), _dy("dy_par_002")])
    xhs = SlowProvider([_xhs("xhs_par_001"), _xhs("xhs_par_002")])
    service = SearchService(
        sessions=sessions, factory=lambda platform, mock: dy if platform == "douyin" else xhs
    )

    await asyncio.wait_for(service.pipeline(job_id), timeout=30)

    assert tracker["max_inside"] == 2, "platforms must crawl concurrently"
    with sessions() as db:
        job = db.get(SearchJob, job_id)
        assert job.status == "completed"
        assert job.provider_counts.get("douyin") == 2
        assert job.provider_counts.get("xiaohongshu") == 2
        assert job.processed_count == 4


# -------------------------------------------------------------------- cache ---


@pytest.mark.anyio
async def test_result_cache_replays_completed_job(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    with sessions() as db:
        _add_video(db, "dy_cache_001", "src_job")
        db.flush()  # videos must exist before job_videos (no ORM relationship)
        db.add(
            SearchJob(
                id="src_job",
                original_query="decor bàn học",
                status="completed",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                processed_count=3,
                found_count=5,
                created_at=utcnow(),
            )
        )
        db.add(
            SearchPlan(
                job_id="src_job", queries=["书桌布置"], use_ai=False, filters=dict(_FILTERS)
            )
        )
        db.add(
            SearchJob(
                id="new_job",
                original_query="decor bàn học",
                status="pending",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                created_at=utcnow(),
            )
        )
        db.add(
            SearchPlan(job_id="new_job", queries=[], use_ai=False, filters=dict(_FILTERS))
        )
        # Flush parents first: without ORM relationships the unit of work does
        # not guarantee cross-table insert order for job_videos' FKs.
        db.flush()
        db.add(
            JobVideo(
                job_id="src_job",
                video_id="v_dy_cache_001",
                relevance_score=80.0,
                quality_score=70.0,
                final_score=75.0,
            )
        )
        db.commit()

    def factory_must_not_run(platform, mock):
        raise AssertionError("cache hit must not create providers")

    service = SearchService(sessions=sessions, factory=factory_must_not_run)
    await asyncio.wait_for(service.pipeline("new_job"), timeout=10)

    with sessions() as db:
        job = db.get(SearchJob, "new_job")
        assert job.status == "completed"
        assert job.processed_count == 1
        assert job.diagnostics["cache_hit"]["source_job_id"] == "src_job"
        links = db.query(JobVideo).filter_by(job_id="new_job").all()
        assert len(links) == 1 and links[0].video_id == "v_dy_cache_001"
        assert links[0].final_score == 75.0


@pytest.mark.anyio
async def test_result_cache_miss_on_filter_mismatch(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    with sessions() as db:
        _add_video(db, "dy_cache_002", "src_job2")
        db.add(
            SearchJob(
                id="src_job2",
                original_query="decor bàn học",
                status="completed",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                processed_count=3,
                found_count=5,
                created_at=utcnow(),
            )
        )
        db.add(
            SearchPlan(
                job_id="src_job2", queries=["书桌布置"], use_ai=False, filters=dict(_FILTERS)
            )
        )
        db.add(
            SearchJob(
                id="new_job2",
                original_query="decor bàn học",
                status="pending",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                created_at=utcnow(),
            )
        )
        stricter = dict(_FILTERS, minimum_likes=1000)
        db.add(SearchPlan(job_id="new_job2", queries=[], use_ai=False, filters=stricter))
        db.commit()

    calls: list[str] = []

    class EmptyProvider:
        capture_origin = "untrusted"

        async def search(self, term, limit, filters, on_batch=None):
            return []

    def factory(platform, mock):
        calls.append(platform)
        return EmptyProvider()

    service = SearchService(sessions=sessions, factory=factory)
    await asyncio.wait_for(service.pipeline("new_job2"), timeout=30)

    assert calls, "filter mismatch must fall through to a real crawl"
    with sessions() as db:
        job = db.get(SearchJob, "new_job2")
        assert job.status == "completed"
        assert "cache_hit" not in (job.diagnostics or {})


@pytest.mark.anyio
async def test_result_cache_miss_after_ttl(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    with sessions() as db:
        _add_video(db, "dy_cache_003", "src_job3")
        db.add(
            SearchJob(
                id="src_job3",
                original_query="decor bàn học",
                status="completed",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                processed_count=3,
                found_count=5,
                created_at=utcnow() - timedelta(hours=2),
            )
        )
        db.add(
            SearchPlan(
                job_id="src_job3", queries=["书桌布置"], use_ai=False, filters=dict(_FILTERS)
            )
        )
        db.add(
            SearchJob(
                id="new_job3",
                original_query="decor bàn học",
                status="pending",
                platforms=["douyin"],
                requested_limit=3,
                is_mock=False,
                created_at=utcnow(),
            )
        )
        db.add(SearchPlan(job_id="new_job3", queries=[], use_ai=False, filters=dict(_FILTERS)))
        db.commit()

    calls: list[str] = []

    class EmptyProvider:
        capture_origin = "untrusted"

        async def search(self, term, limit, filters, on_batch=None):
            return []

    def factory(platform, mock):
        calls.append(platform)
        return EmptyProvider()

    service = SearchService(sessions=sessions, factory=factory)
    await asyncio.wait_for(service.pipeline("new_job3"), timeout=30)

    assert calls, "expired cache entry must fall through to a real crawl"


def _seed_cache_pair(
    db,
    suffix: str,
    *,
    source_min_relevance: float | None,
    new_min_relevance: float | None,
    source_plan_extra: dict | None = None,
) -> None:
    src, new = f"src_job{suffix}", f"new_job{suffix}"
    _add_video(db, f"dy_cache_{suffix}", src)
    db.add(
        SearchJob(
            id=src,
            original_query="decor bàn học",
            status="completed",
            platforms=["douyin"],
            requested_limit=3,
            is_mock=False,
            processed_count=3,
            found_count=5,
            min_relevance=source_min_relevance,
            created_at=utcnow(),
        )
    )
    plan_kwargs: dict = {"queries": ["书桌布置"], "use_ai": False, "filters": dict(_FILTERS)}
    plan_kwargs.update(source_plan_extra or {})
    db.add(SearchPlan(job_id=src, **plan_kwargs))
    db.add(
        SearchJob(
            id=new,
            original_query="decor bàn học",
            status="pending",
            platforms=["douyin"],
            requested_limit=3,
            is_mock=False,
            min_relevance=new_min_relevance,
            created_at=utcnow(),
        )
    )
    db.add(SearchPlan(job_id=new, queries=[], use_ai=False, filters=dict(_FILTERS)))
    db.flush()
    db.add(
        JobVideo(
            job_id=src,
            video_id=f"v_dy_cache_{suffix}",
            relevance_score=80.0,
            quality_score=70.0,
            final_score=75.0,
        )
    )
    db.commit()


@pytest.mark.anyio
async def test_result_cache_miss_on_relevance_threshold(sessions, monkeypatch):
    """M1: the job-level precision gate (min_relevance) is part of the key.

    The API defaults it to 40 for real searches independently of
    filters.minimum_relevance, so two jobs with identical filters can still
    have different thresholds — a strict job must not replay a loose cache.
    """
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    with sessions() as db:
        _seed_cache_pair(db, "4", source_min_relevance=40.0, new_min_relevance=80.0)

    calls: list[str] = []

    class EmptyProvider:
        capture_origin = "untrusted"

        async def search(self, term, limit, filters, on_batch=None):
            return []

    def factory(platform, mock):
        calls.append(platform)
        return EmptyProvider()

    service = SearchService(sessions=sessions, factory=factory)
    await asyncio.wait_for(service.pipeline("new_job4"), timeout=30)

    assert calls, "threshold mismatch must fall through to a real crawl"
    with sessions() as db:
        job = db.get(SearchJob, "new_job4")
        assert job.status == "completed"
        assert "cache_hit" not in (job.diagnostics or {})


@pytest.mark.anyio
async def test_result_cache_hit_on_same_relevance_threshold(sessions, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    with sessions() as db:
        _seed_cache_pair(db, "5", source_min_relevance=80.0, new_min_relevance=80.0)

    def factory_must_not_run(platform, mock):
        raise AssertionError("cache hit must not create providers")

    service = SearchService(sessions=sessions, factory=factory_must_not_run)
    await asyncio.wait_for(service.pipeline("new_job5"), timeout=10)

    with sessions() as db:
        job = db.get(SearchJob, "new_job5")
        assert job.status == "completed"
        assert job.diagnostics["cache_hit"]["source_job_id"] == "src_job5"


@pytest.mark.anyio
async def test_result_cache_hit_persists_plan_fields(sessions, monkeypatch):
    """M2: the copied plan fields must survive in a fresh session.

    The pipeline loads the plan in a session that closes before the cache
    replay, so mutating that detached object silently lost the write.
    """
    monkeypatch.setattr(settings, "search_cache_ttl_seconds", 3600)
    extra = {
        "source": "ai",
        "warning": "AI mở rộng truy vấn có thể lệch chủ đề",
        "expansion": {"seed": "decor bàn học", "variants": 2},
    }
    with sessions() as db:
        _seed_cache_pair(
            db,
            "6",
            source_min_relevance=None,
            new_min_relevance=None,
            source_plan_extra={"queries": ["书桌布置", "bàn học tối giản"], **extra},
        )

    def factory_must_not_run(platform, mock):
        raise AssertionError("cache hit must not create providers")

    service = SearchService(sessions=sessions, factory=factory_must_not_run)
    await asyncio.wait_for(service.pipeline("new_job6"), timeout=10)

    with sessions() as db:
        job = db.get(SearchJob, "new_job6")
        assert job.diagnostics["cache_hit"]["source_job_id"] == "src_job6"
        plan = db.get(SearchPlan, "new_job6")
        assert plan.queries == ["书桌布置", "bàn học tối giản"]
        assert plan.source == "ai"
        assert plan.warning == extra["warning"]
        assert plan.expansion == extra["expansion"]
