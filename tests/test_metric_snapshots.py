import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.database import Base
from backend.core.events import EventBus
from backend.models.metric_snapshot import VideoMetricSnapshot
from backend.models.search_job import SearchJob
from backend.models.video import Video
from backend.schemas.contracts import VideoResult
from backend.services.metric_service import MetricService
from backend.services.search_service import SearchService


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


def create_sample_video(sessions, video_id="vid-test-1", **kwargs) -> Video:
    with sessions() as db:
        video = Video(
            id=video_id,
            platform="xiaohongshu",
            platform_video_id=kwargs.get("platform_video_id", "xhs_123"),
            url=kwargs.get("url", "https://www.xiaohongshu.com/explore/xhs_123"),
            canonical_url=kwargs.get("canonical_url", "https://www.xiaohongshu.com/explore/xhs_123"),
            fingerprint=kwargs.get("fingerprint", "fp_123"),
            title=kwargs.get("title", "Video thử nghiệm"),
            search_query="test",
            search_job_id="job-1",
            like_count=kwargs.get("like_count", 100),
            comment_count=kwargs.get("comment_count", 20),
            share_count=kwargs.get("share_count", 5),
            favorite_count=kwargs.get("favorite_count", 15),
            view_count=kwargs.get("view_count", 1000),
        )
        db.add(video)
        db.commit()
        return video


def test_metric_snapshot_model_and_foreign_key_cascade(in_memory_db):
    create_sample_video(in_memory_db, "vid-cascade-test")

    with in_memory_db() as db:
        snap = VideoMetricSnapshot(
            video_id="vid-cascade-test",
            like_count=500,
            comment_count=50,
            source="test",
        )
        db.add(snap)
        db.commit()

        # Check snapshot exists
        found = db.scalars(
            select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == "vid-cascade-test")
        ).all()
        assert len(found) == 1
        assert found[0].like_count == 500

        # Delete parent video -> snapshot deleted cascade
        v = db.get(Video, "vid-cascade-test")
        db.delete(v)
        db.commit()

        found_after = db.scalars(
            select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == "vid-cascade-test")
        ).all()
        assert len(found_after) == 0


def test_record_snapshot_initial_and_deduplication(in_memory_db, clean_event_bus):
    video = create_sample_video(in_memory_db, "vid-dedup-test")
    svc = MetricService(sessions=in_memory_db, event_bus=clean_event_bus, throttle_seconds=300)

    # 1. Initial snapshot
    snap1 = svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=20,
        share_count=5,
        favorite_count=15,
        view_count=1000,
        source="search",
    )
    assert snap1 is not None
    assert snap1.like_count == 100

    # 2. Record identical metrics immediately -> throttled (same snapshot returned)
    snap2 = svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=20,
        share_count=5,
        favorite_count=15,
        view_count=1000,
        source="search",
    )
    assert snap2.id == snap1.id

    with in_memory_db() as db:
        count = len(
            db.scalars(
                select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == video.id)
            ).all()
        )
        assert count == 1


def test_record_snapshot_changed_metrics_records_new(in_memory_db, clean_event_bus):
    video = create_sample_video(in_memory_db, "vid-changed-metrics")
    svc = MetricService(sessions=in_memory_db, event_bus=clean_event_bus, throttle_seconds=300)

    snap1 = svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=20,
    )
    assert snap1 is not None

    # Change like_count from 100 to 150
    snap2 = svc.record_snapshot(
        video_id=video.id,
        like_count=150,
        comment_count=20,
    )
    assert snap2 is not None
    assert snap2.id != snap1.id
    assert snap2.like_count == 150

    with in_memory_db() as db:
        count = len(
            db.scalars(
                select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == video.id)
            ).all()
        )
        assert count == 2


def test_record_snapshot_force_override(in_memory_db, clean_event_bus):
    video = create_sample_video(in_memory_db, "vid-force-test")
    svc = MetricService(sessions=in_memory_db, event_bus=clean_event_bus, throttle_seconds=300)

    snap1 = svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=20,
    )

    # Force recording even with identical metrics
    snap2 = svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=20,
        force=True,
    )
    assert snap2.id != snap1.id

    with in_memory_db() as db:
        count = len(
            db.scalars(
                select(VideoMetricSnapshot).where(VideoMetricSnapshot.video_id == video.id)
            ).all()
        )
        assert count == 2


def test_get_history_computes_running_deltas(in_memory_db):
    video = create_sample_video(in_memory_db, "vid-history-deltas")
    svc = MetricService(sessions=in_memory_db, throttle_seconds=0)

    svc.record_snapshot(
        video_id=video.id,
        like_count=100,
        comment_count=10,
        share_count=2,
        favorite_count=5,
        view_count=1000,
        force=True,
    )
    svc.record_snapshot(
        video_id=video.id,
        like_count=180,
        comment_count=15,
        share_count=4,
        favorite_count=8,
        view_count=1500,
        force=True,
    )
    svc.record_snapshot(
        video_id=video.id,
        like_count=250,
        comment_count=25,
        share_count=10,
        favorite_count=12,
        view_count=2200,
        force=True,
    )

    history = svc.get_history(video_id=video.id)
    assert history.video_id == video.id
    assert history.total_snapshots == 3
    snaps = history.snapshots

    # Snapshot 0: baseline (deltas 0)
    assert snaps[0].delta_likes == 0
    assert snaps[0].delta_comments == 0
    assert snaps[0].elapsed_seconds == 0.0

    # Snapshot 1: relative to snapshot 0
    assert snaps[1].delta_likes == 80
    assert snaps[1].delta_comments == 5
    assert snaps[1].delta_shares == 2
    assert snaps[1].delta_views == 500

    # Snapshot 2: relative to snapshot 1
    assert snaps[2].delta_likes == 70
    assert snaps[2].delta_comments == 10
    assert snaps[2].delta_shares == 6
    assert snaps[2].delta_views == 700


@pytest.mark.anyio
async def test_search_service_records_snapshots(in_memory_db, clean_event_bus):
    metric_svc = MetricService(sessions=in_memory_db, event_bus=clean_event_bus)
    search_svc = SearchService(
        sessions=in_memory_db,
        event_bus=clean_event_bus,
        metric_service=metric_svc,
    )

    with in_memory_db() as db:
        job = SearchJob(
            id="job-snap-test",
            original_query="tai nghe",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="searching",
        )
        db.add(job)
        db.commit()

    # Emulate provider result
    item = VideoResult(
        platform="xiaohongshu",
        platform_video_id="snap_v1",
        url="https://www.xiaohongshu.com/explore/snap_v1",
        title="Tai nghe bluetooth xịn",
        like_count=350,
        comment_count=45,
        share_count=10,
        favorite_count=30,
        view_count=5000,
        is_mock=False,
    )

    # In SearchService run loop, insertion triggers record_snapshot
    with in_memory_db() as db:
        v = Video(
            id="vid-search-snap",
            platform=item.platform,
            platform_video_id=item.platform_video_id,
            url=str(item.url),
            canonical_url=str(item.url),
            fingerprint="fp-snap-v1",
            title=item.title,
            search_query="tai nghe",
            search_job_id="job-snap-test",
            like_count=item.like_count,
            comment_count=item.comment_count,
            share_count=item.share_count,
            favorite_count=item.favorite_count,
            view_count=item.view_count,
        )
        db.add(v)
        db.flush()
        search_svc.metric_service.record_snapshot(
            video_id=v.id,
            like_count=v.like_count,
            comment_count=v.comment_count,
            share_count=v.share_count,
            favorite_count=v.favorite_count,
            view_count=v.view_count,
            source="search",
            db=db,
        )
        db.commit()

    # Verify snapshot persisted
    history = metric_svc.get_history("vid-search-snap")
    assert history.total_snapshots == 1
    assert history.snapshots[0].like_count == 350
    assert history.snapshots[0].source == "search"


@pytest.mark.anyio
async def test_metric_snapshot_dispatches_sse_event(in_memory_db, clean_event_bus):
    video = create_sample_video(in_memory_db, "vid-sse-dispatch")
    svc = MetricService(sessions=in_memory_db, event_bus=clean_event_bus)

    sub = clean_event_bus.subscribe()

    svc.record_snapshot(
        video_id=video.id,
        like_count=999,
        comment_count=88,
        source="search",
        force=True,
    )

    ev = await asyncio.wait_for(sub.get(), timeout=1.0)
    assert ev.event == "video:metrics_update"
    assert ev.data["video_id"] == video.id
    assert ev.data["like_count"] == 999
    assert ev.data["comment_count"] == 88

    await sub.aclose()


def test_api_metric_history_and_refresh():
    with TestClient(app) as client:
        # Create a video directly in the test client's db
        from backend.core.database import SessionLocal

        with SessionLocal() as db:
            video = db.get(Video, "vid-api-metric-test")
            if not video:
                video = Video(
                    id="vid-api-metric-test",
                    platform="xiaohongshu",
                    platform_video_id="api_snap_1",
                    url="https://www.xiaohongshu.com/explore/api_snap_1",
                    canonical_url="https://www.xiaohongshu.com/explore/api_snap_1",
                    fingerprint="fp-api-snap",
                    title="API Metric Test",
                    search_query="test",
                    search_job_id="job-1",
                    like_count=123,
                    comment_count=45,
                    share_count=6,
                    favorite_count=12,
                    view_count=1500,
                )
                db.add(video)
                db.commit()

        # 1. GET /api/videos/{video_id}/metrics/history
        res = client.get("/api/videos/vid-api-metric-test/metrics/history")
        assert res.status_code == 200
        data = res.json()
        assert data["video_id"] == "vid-api-metric-test"
        assert isinstance(data["snapshots"], list)

        # 2. POST /api/videos/{video_id}/metrics/refresh
        refresh_res = client.post("/api/videos/vid-api-metric-test/metrics/refresh")
        assert refresh_res.status_code == 200
        snap = refresh_res.json()
        assert snap["video_id"] == "vid-api-metric-test"
        assert snap["source"] == "refresh"
        assert snap["like_count"] == 123

        # 3. 404 for non-existent video
        err_res = client.get("/api/videos/non-existent-vid-999/metrics/history")
        assert err_res.status_code == 404
