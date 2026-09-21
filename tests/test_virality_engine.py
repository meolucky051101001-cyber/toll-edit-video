import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.database import Base
from backend.core.events import EventBus
from backend.models.metric_snapshot import VideoMetricSnapshot
from backend.models.search_job import JobVideo, SearchJob
from backend.models.video import Video
from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.virality_service import (
    ViralityService,
    compute_virality,
)


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    return sessions


@pytest.fixture
def clean_event_bus():
    bus = EventBus()
    yield bus
    bus.clear()


def test_lifetime_velocity_and_age_buckets():
    now = datetime.now(timezone.utc)

    # 1. Fresh video (3 hours old, 1500 likes)
    v_fresh = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/fresh1",
        title="Breaking tip",
        published_at=now - timedelta(hours=3),
        like_count=1500,
        comment_count=100,
        share_count=50,
        is_mock=False,
    )
    m_fresh = compute_virality(v_fresh)
    assert m_fresh.age_bucket == "fresh"
    assert m_fresh.hourly_velocity == pytest.approx(500.0, rel=0.05)
    assert m_fresh.virality_score > 40.0

    # 2. Trending video (3 days old)
    v_trend = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/trend1",
        title="Weekly trend",
        published_at=now - timedelta(days=3),
        like_count=7200,
        is_mock=False,
    )
    m_trend = compute_virality(v_trend)
    assert m_trend.age_bucket == "trending"
    assert m_trend.hourly_velocity == pytest.approx(100.0, rel=0.05)

    # 3. Established video (14 days old)
    v_est = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/est1",
        title="Monthly guide",
        published_at=now - timedelta(days=14),
        like_count=5000,
        is_mock=False,
    )
    m_est = compute_virality(v_est)
    assert m_est.age_bucket == "established"

    # 4. Evergreen video (60 days old)
    v_ever = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/ever1",
        title="Old staple",
        published_at=now - timedelta(days=60),
        like_count=10000,
        is_mock=False,
    )
    m_ever = compute_virality(v_ever)
    assert m_ever.age_bucket == "evergreen"


def test_snapshot_hourly_velocity():
    now = datetime.now(timezone.utc)
    t1 = now - timedelta(hours=2)
    t2 = now

    s1 = VideoMetricSnapshot(
        video_id="v-test",
        like_count=1000,
        captured_at=t1,
    )
    s2 = VideoMetricSnapshot(
        video_id="v-test",
        like_count=2200,
        captured_at=t2,
    )

    metrics = compute_virality(s2, snapshots=[s1, s2])
    # 1200 likes gained in 2 hours = 600 likes/hour
    assert metrics.hourly_velocity == pytest.approx(600.0, rel=0.05)


def test_acceleration_calculation():
    now = datetime.now(timezone.utc)
    s1 = VideoMetricSnapshot(
        video_id="v-accel",
        like_count=100,
        captured_at=now - timedelta(hours=2),
    )
    s2 = VideoMetricSnapshot(
        video_id="v-accel",
        like_count=300,  # 200 likes in 1 hr
        captured_at=now - timedelta(hours=1),
    )
    s3 = VideoMetricSnapshot(
        video_id="v-accel",
        like_count=1100,  # 800 likes in 1 hr
        captured_at=now,
    )

    metrics = compute_virality(s3, snapshots=[s1, s2, s3])
    assert metrics.acceleration > 0.0  # Positive acceleration!
    assert metrics.hourly_velocity == pytest.approx(800.0, rel=0.05)


def test_spread_ratio_impact():
    v_passive = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/p1",
        title="Passive views",
        like_count=1000,
        comment_count=0,
        share_count=0,
        favorite_count=0,
        is_mock=False,
    )
    v_viral = VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/v1",
        title="Viral discussion",
        like_count=1000,
        comment_count=200,
        share_count=150,
        favorite_count=300,
        is_mock=False,
    )

    m_passive = compute_virality(v_passive)
    m_viral = compute_virality(v_viral)

    assert m_viral.spread_ratio > m_passive.spread_ratio
    assert m_viral.virality_score > m_passive.virality_score


def test_ranking_preserves_original_intent_with_virality():
    # 1. Relevant but modest engagement
    relevant = VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/1111111111111111111",
        title="unbox văn phòng phẩm cute",
        like_count=500,
        is_mock=False,
    )
    # 2. Distractor with massive viral engagement
    distractor = VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/2222222222222222222",
        title="nấu ăn ngon gia đình",
        like_count=2000000,
        comment_count=50000,
        share_count=20000,
        is_mock=False,
    )

    r_rel = rank_detailed(relevant, "unbox văn phòng phẩm cute")
    r_dist = rank_detailed(distractor, "unbox văn phòng phẩm cute")

    # Relevance constraint: distractor must have 0 or <= 20 relevance
    assert r_rel.relevance > 70.0
    assert r_dist.relevance == 0.0
    assert r_rel.final_score > r_dist.final_score
    assert r_dist.final_score == 0.0


def test_update_video_virality_and_db(in_memory_db, clean_event_bus):
    svc = ViralityService(event_bus=clean_event_bus)

    with in_memory_db() as db:
        job = SearchJob(
            id="job1",
            original_query="test",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="completed",
        )
        db.add(job)
        v = Video(
            id="v-db-test",
            platform="xiaohongshu",
            platform_video_id="pvid1",
            url="https://www.xiaohongshu.com/explore/pvid1",
            canonical_url="https://www.xiaohongshu.com/explore/pvid1",
            fingerprint="fp1",
            title="Virality DB Test",
            search_query="test",
            search_job_id="job1",
            like_count=2000,
            comment_count=150,
            share_count=80,
            favorite_count=300,
            published_at=datetime.now(timezone.utc) - timedelta(hours=4),
        )
        db.add(v)
        db.flush()

        jv = JobVideo(
            job_id="job1",
            video_id="v-db-test",
            relevance_score=80.0,
            quality_score=60.0,
            final_score=75.0,
        )
        db.add(jv)
        db.commit()

        metrics = svc.update_video_virality("v-db-test", db=db)
        assert metrics is not None
        assert metrics.virality_score > 0.0

        refreshed = db.get(Video, "v-db-test")
        assert refreshed.virality_score == metrics.virality_score
        assert refreshed.velocity_score == metrics.velocity_score

        refreshed_jv = db.get(JobVideo, ("job1", "v-db-test"))
        assert refreshed_jv.virality_score == metrics.virality_score
        assert refreshed_jv.velocity_score == metrics.velocity_score


@pytest.mark.anyio
async def test_sse_virality_update_event(in_memory_db, clean_event_bus):
    svc = ViralityService(event_bus=clean_event_bus)
    sub = clean_event_bus.subscribe()

    with in_memory_db() as db:
        v = Video(
            id="v-sse-virality",
            platform="xiaohongshu",
            platform_video_id="pvid_sse",
            url="https://www.xiaohongshu.com/explore/pvid_sse",
            canonical_url="https://www.xiaohongshu.com/explore/pvid_sse",
            fingerprint="fp_sse",
            title="SSE Virality Test",
            search_query="test",
            search_job_id="job_sse",
            like_count=5000,
        )
        db.add(v)
        db.commit()

        svc.update_video_virality("v-sse-virality", db=db)

    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev.event == "video:virality_update"
    assert ev.data["video_id"] == "v-sse-virality"
    assert "virality_score" in ev.data
    assert "hourly_velocity" in ev.data

    await sub.aclose()


def test_api_virality_endpoints_and_sort():
    with TestClient(app) as client:
        from backend.core.database import SessionLocal

        with SessionLocal() as db:
            v1 = db.get(Video, "v-api-virality-1")
            if not v1:
                v1 = Video(
                    id="v-api-virality-1",
                    platform="xiaohongshu",
                    platform_video_id="v_api_1",
                    url="https://www.xiaohongshu.com/explore/v_api_1",
                    canonical_url="https://www.xiaohongshu.com/explore/v_api_1",
                    fingerprint="fp_api_1",
                    title="High Virality Video",
                    search_query="test",
                    search_job_id="job_api",
                    like_count=10000,
                    virality_score=85.0,
                    velocity_score=90.0,
                )
                db.add(v1)
            v2 = db.get(Video, "v-api-virality-2")
            if not v2:
                v2 = Video(
                    id="v-api-virality-2",
                    platform="xiaohongshu",
                    platform_video_id="v_api_2",
                    url="https://www.xiaohongshu.com/explore/v_api_2",
                    canonical_url="https://www.xiaohongshu.com/explore/v_api_2",
                    fingerprint="fp_api_2",
                    title="Low Virality Video",
                    search_query="test",
                    search_job_id="job_api",
                    like_count=50,
                    virality_score=10.0,
                    velocity_score=5.0,
                )
                db.add(v2)
            db.commit()

        # 1. GET /api/videos/{video_id}/virality
        res = client.get("/api/videos/v-api-virality-1/virality")
        assert res.status_code == 200
        data = res.json()
        assert data["video_id"] == "v-api-virality-1"
        assert "virality_score" in data
        assert "age_bucket" in data

        # 2. POST /api/videos/{video_id}/virality/recalculate
        recalc_res = client.post("/api/videos/v-api-virality-1/virality/recalculate")
        assert recalc_res.status_code == 200
        assert recalc_res.json()["video_id"] == "v-api-virality-1"

        # 3. GET /api/videos?sort=virality
        sort_res = client.get("/api/videos?sort=virality")
        assert sort_res.status_code == 200
        items = sort_res.json()["items"]
        assert len(items) >= 2
        # Check ordering: highest virality first
        virality_scores = [item["virality_score"] for item in items if item["id"].startswith("v-api-virality")]
        assert virality_scores == sorted(virality_scores, reverse=True)

        # 4. GET /api/videos?sort=velocity
        vel_res = client.get("/api/videos?sort=velocity")
        assert vel_res.status_code == 200
