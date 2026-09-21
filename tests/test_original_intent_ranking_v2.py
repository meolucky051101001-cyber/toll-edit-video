from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.database import Base
from backend.core.events import EventBus
from backend.models.search_job import JobVideo, SearchJob
from backend.models.video import Video
from backend.schemas.contracts import VideoResult
from backend.services.metric_service import MetricService
from backend.services.ranking_service import rank_detailed
from backend.services.virality_service import ViralityService


@pytest.fixture
def in_memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield session_factory
    engine.dispose()


@pytest.fixture
def clean_event_bus():
    bus = EventBus()
    yield bus
    bus.clear()


def make_result(
    title: str,
    caption: str = "",
    hashtags: list[str] | None = None,
    like_count: int = 100,
    published_at: datetime | None = None,
) -> VideoResult:
    return VideoResult(
        platform="xiaohongshu",
        url="https://www.xiaohongshu.com/explore/sample_test",
        title=title,
        caption=caption,
        hashtags=hashtags or [],
        like_count=like_count,
        published_at=published_at or datetime.now(timezone.utc) - timedelta(days=2),
    )


def test_negative_keyword_penalty():
    query = "unbox áo khoác mỏng"
    negatives = ["mùa đông", "dày", "lông vũ"]

    normal_vid = make_result(
        title="Unbox áo khoác mỏng dáng rộng",
        caption="Chiếc áo khoác mỏng phong cách mùa thu nhẹ nhàng",
    )
    neg_vid = make_result(
        title="Unbox áo khoác dày mùa đông",
        caption="Mẫu áo khoác mỏng nhẹ nhưng giữ ấm mùa đông cực dày",
    )

    detail_normal = rank_detailed(normal_vid, query, negative_keywords=negatives)
    detail_neg = rank_detailed(neg_vid, query, negative_keywords=negatives)

    assert detail_normal.relevance > 60
    assert not detail_normal.matched_negative_tokens

    # Negative keyword penalty strictly reduces relevance and records violated token
    assert "mùa đông" in detail_neg.matched_negative_tokens or "dày" in detail_neg.matched_negative_tokens
    assert detail_neg.relevance <= 15.0
    assert detail_normal.final_score > detail_neg.final_score


def test_field_specific_provenance():
    res = make_result(
        title="Review bình giữ nhiệt mini",
        caption="Chiếc cốc giữ nhiệt thép không gỉ tiện lợi mang theo",
        hashtags=["cocgiunhiet", "binhgiunhiet"],
    )
    detail = rank_detailed(res, "bình giữ nhiệt mini", alternative_queries=["bình giữ nhiệt"])

    assert "title" in detail.provenance
    assert "caption" in detail.provenance
    assert "hashtags" in detail.provenance

    # Title provenance contains title tokens
    title_matches = detail.provenance["title"]
    assert any("binh" in t or "nhiet" in t for t in title_matches)


def test_title_priority_over_caption_spam():
    query = "son dưỡng môi hồng"

    title_match_vid = make_result(
        title="Son dưỡng môi hồng tự nhiên mềm mịn",
        caption="Đánh giá chi tiết son dưỡng",
        hashtags=["sonduong"],
    )

    caption_spam_vid = make_result(
        title="Vlog một ngày ăn uống đi chơi cuối tuần",
        caption=(
            "Hôm nay mình đi cà phê shopping rất nhiều món đồ đẹp: son dưỡng môi hồng tự nhiên, "
            "váy hoa, kẹp tóc, phụ kiện xinh xắn, trà sữa trân châu và nhiều thứ hay ho khác nữa..."
        ),
        hashtags=["vlog", "daily"],
    )

    detail_title = rank_detailed(title_match_vid, query)
    detail_spam = rank_detailed(caption_spam_vid, query)

    assert detail_title.relevance > 70
    assert detail_spam.relevance <= 35.0
    assert detail_title.relevance > detail_spam.relevance * 2


def test_virality_recalculates_quality_and_final_scores(in_memory_db, clean_event_bus):
    virality_svc = ViralityService(event_bus=clean_event_bus)

    with in_memory_db() as db:
        job = SearchJob(
            id="job-v2",
            original_query="test ranking",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="completed",
        )
        db.add(job)
        v = Video(
            id="v-ranking-test",
            platform="xiaohongshu",
            platform_video_id="p1",
            url="https://www.xiaohongshu.com/explore/p1",
            canonical_url="https://www.xiaohongshu.com/explore/p1",
            fingerprint="fp1",
            title="Sổ tay mini bullet journal",
            search_query="sổ tay mini",
            search_job_id="job-v2",
            like_count=100,
            relevance_score=80.0,
            quality_score=20.0,
            final_score=67.2,
            virality_score=0.0,
            velocity_score=0.0,
            published_at=datetime.now(timezone.utc) - timedelta(hours=5),
        )
        db.add(v)
        db.flush()

        jv = JobVideo(
            job_id="job-v2",
            video_id="v-ranking-test",
            relevance_score=80.0,
            quality_score=20.0,
            final_score=67.2,
        )
        db.add(jv)
        db.commit()

        initial_final = v.final_score

        # Simulate massive virality growth
        v.like_count = 15000
        v.comment_count = 1200
        v.share_count = 900
        v.favorite_count = 2500
        db.commit()

        metrics = virality_svc.update_video_virality("v-ranking-test", db=db)
        assert metrics is not None
        assert metrics.virality_score > 50.0

        refreshed_v = db.get(Video, "v-ranking-test")
        refreshed_jv = db.get(JobVideo, ("job-v2", "v-ranking-test"))

        # Both quality and final score must be recalculated and higher
        assert refreshed_v.quality_score > 20.0
        assert refreshed_v.final_score > initial_final
        assert refreshed_jv.quality_score == refreshed_v.quality_score
        assert refreshed_jv.final_score == refreshed_v.final_score


def test_metric_snapshot_auto_cascades_to_virality_and_ranking(in_memory_db, clean_event_bus):
    metric_svc = MetricService(event_bus=clean_event_bus, throttle_seconds=0)

    with in_memory_db() as db:
        job = SearchJob(
            id="job-cascade",
            original_query="cascade test",
            platforms=["xiaohongshu"],
            requested_limit=10,
            status="completed",
        )
        db.add(job)
        v = Video(
            id="v-cascade",
            platform="xiaohongshu",
            platform_video_id="p_casc",
            url="https://www.xiaohongshu.com/explore/p_casc",
            canonical_url="https://www.xiaohongshu.com/explore/p_casc",
            fingerprint="fp_casc",
            title="Hộp bút đa năng",
            search_query="hộp bút",
            search_job_id="job-cascade",
            like_count=50,
            relevance_score=85.0,
            quality_score=15.0,
            final_score=70.0,
            virality_score=0.0,
            published_at=datetime.now(timezone.utc) - timedelta(hours=3),
        )
        db.add(v)
        db.flush()
        jv = JobVideo(
            job_id="job-cascade",
            video_id="v-cascade",
            relevance_score=85.0,
            quality_score=15.0,
            final_score=70.0,
        )
        db.add(jv)
        db.commit()

        # Record a snapshot with high engagement
        v.like_count = 8000
        v.comment_count = 500
        v.share_count = 400
        v.favorite_count = 1500
        db.commit()

        snap = metric_svc.record_snapshot(
            video_id="v-cascade",
            like_count=8000,
            comment_count=500,
            share_count=400,
            favorite_count=1500,
            force=True,
            db=db,
        )
        assert snap is not None

        # Automatically cascaded to virality and final_score
        refreshed = db.get(Video, "v-cascade")
        assert refreshed.virality_score > 0.0
        assert refreshed.final_score > 70.0


def test_viral_distractor_with_virality_100_cannot_beat_relevant_video():
    query = "bút gel ngòi nhỏ viết êm"

    relevant_small = make_result(
        title="Bút gel ngòi nhỏ 0.38mm viết êm không tắc mực",
        caption="Đánh giá chi tiết bút gel đi học",
        hashtags=["butgel", "stationery"],
        like_count=300,
    )

    # 10M likes viral video on cooking / celebrity
    viral_distractor = make_result(
        title="Hướng dẫn làm lẩu hải sản khổng lồ siêu ngon tại nhà",
        caption="Hôm nay trời lạnh ăn lẩu ấm cúng",
        hashtags=["nauandaily", "haisan", "foodie"],
        like_count=10000000,
    )

    detail_relevant = rank_detailed(relevant_small, query)
    detail_distractor = rank_detailed(viral_distractor, query)

    assert detail_relevant.relevance >= 60.0
    assert detail_distractor.relevance == 0.0
    assert detail_distractor.final_score == 0.0
    assert detail_relevant.final_score > 0.0
    assert detail_relevant.final_score > detail_distractor.final_score
