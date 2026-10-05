import asyncio

import pytest
from sqlalchemy import select

from backend.models import JobVideo, SearchJob, Video
from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.search_service import SearchService
from backend.services.translator import plan_query


@pytest.mark.parametrize(
    "platform,url,video_id",
    [
        ("douyin", "https://www.douyin.com/video/7000000000000000003", "7000000000000000003"),
        (
            "xiaohongshu",
            "https://www.xiaohongshu.com/explore/64f6d1a2b3c4d5e6f7a8b9c0",
            "64f6d1a2b3c4d5e6f7a8b9c0",
        ),
    ],
)
def test_sticker_removal_query_accepts_tweezer_tool_results_on_both_platforms(
    platform, url, video_id
):
    result = VideoResult(
        platform=platform,
        platform_video_id=video_id,
        url=url,
        title="手账尖头镊子，夹取贴纸更方便",
        hashtags=["贴纸工具", "咕卡"],
        media_type="video",
    )

    detail = rank_detailed(
        result,
        "dụng cụ bóc sticker",
        alternative_queries=["贴纸镊子", "咕卡工具", "手账贴纸工具"],
    )

    assert "dung cu" not in detail.missing_mandatory_intents
    assert "sticker" not in detail.missing_mandatory_intents
    assert detail.relevance >= 40.0


def test_sticker_tool_intent_still_rejects_a_tweezer_video_without_sticker_context():
    result = VideoResult(
        platform="xiaohongshu",
        platform_video_id="64f6d1a2b3c4d5e6f7a8b9c0",
        url="https://www.xiaohongshu.com/explore/64f6d1a2b3c4d5e6f7a8b9c0",
        title="手账镊子日常分享",
        media_type="video",
    )

    detail = rank_detailed(result, "dụng cụ bóc sticker")

    assert "sticker" in detail.missing_mandatory_intents


def test_dung_cu_boc_sticker_expands_to_precise_chinese_terms():
    plan = plan_query("dụng cụ bóc sticker")

    assert plan.tiered_queries == ["贴纸镊子", "夹取贴纸", "贴纸剥离器"]


def test_query_phrase_ranks_explicit_sticker_pickup_video_above_real_threshold():
    result = VideoResult(
        platform="xiaohongshu",
        platform_video_id="64f6d1a2b3c4d5e6f7a8b9c0",
        url="https://www.xiaohongshu.com/explore/64f6d1a2b3c4d5e6f7a8b9c0",
        title="当手帐儿尝试用细胞夹取贴纸时：",
        media_type="video",
    )
    plan = plan_query("dụng cụ bóc sticker")

    detail = rank_detailed(result, "dụng cụ bóc sticker", alternative_queries=plan.tiered_queries)

    assert "dung cu" not in detail.missing_mandatory_intents
    assert "sticker" not in detail.missing_mandatory_intents
    assert detail.relevance >= 40.0


def test_sticker_peeling_tool_is_relevant_but_sticker_only_is_not():
    tool = VideoResult(
        platform="xiaohongshu",
        platform_video_id="64f6d1a2b3c4d5e6f7a8b9c0",
        url="https://www.xiaohongshu.com/explore/64f6d1a2b3c4d5e6f7a8b9c0",
        title="自制贴纸剥离器！",
        media_type="video",
    )
    sticker_only = tool.update_validated(title="贴纸书开箱与收纳")
    assert not rank_detailed(tool, "dụng cụ bóc sticker").missing_mandatory_intents
    assert "dung cu" in rank_detailed(
        sticker_only, "dụng cụ bóc sticker"
    ).missing_mandatory_intents


def test_real_search_persistence_enforces_strict_sticker_tweezer_title(sessions):
    with sessions() as db:
        job = SearchJob(
            original_query="nhíp gắp sticker",
            platforms=["xiaohongshu"],
            requested_limit=2,
            min_relevance=40.0,
            is_mock=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    tool_only = VideoResult(
        platform="xiaohongshu",
        platform_video_id="a" * 24,
        url="https://www.xiaohongshu.com/explore/" + "a" * 24,
        title="我的人生镊子",
        caption="整理手账工具 #贴纸 #造景",
        media_type="video",
    )
    both_in_title = VideoResult(
        platform="xiaohongshu",
        platform_video_id="b" * 24,
        url="https://www.xiaohongshu.com/explore/" + "b" * 24,
        title="造景贴纸镊子分享",
        media_type="video",
    )
    loose_rank = rank_detailed(
        tool_only, "nhíp gắp sticker", alternative_queries=["贴纸镊子"]
    )
    assert loose_rank.relevance >= 40 and not loose_rank.missing_mandatory_intents

    service = SearchService(sessions)
    asyncio.run(
        service.persist_results(
            job_id,
            "xiaohongshu",
            "贴纸镊子",
            [tool_only, both_in_title],
            2,
            alternative_queries=["贴纸镊子"],
            original_query="nhíp gắp sticker",
        )
    )
    with sessions() as db:
        saved_ids = {
            video.platform_video_id
            for video in db.scalars(select(Video).join(JobVideo).where(JobVideo.job_id == job_id))
        }
        persisted_job = db.get(SearchJob, job_id)
        assert saved_ids == {"b" * 24}
        assert persisted_job is not None and persisted_job.rejected_count == 1
        assert "Tiêu đề chưa xác minh" in persisted_job.diagnostics["rejected_candidates"][0]["reason"]


def test_sticker_tweezer_queries_keep_both_intents_and_prioritize_live_winner():
    plan = plan_query("nhíp gắp sticker")
    assert plan.tiered_queries == [
        "造景贴纸镊子", "贴纸镊子", "咕卡贴纸镊子"
    ]


def test_sticker_tweezer_photo_does_not_outrank_video():
    candidate = VideoResult(
        platform="xiaohongshu",
        platform_video_id="c" * 24,
        url="https://www.xiaohongshu.com/explore/" + "c" * 24,
        title="手账贴纸镊子选购指南",
        media_type="image_text",
    )
    score = rank_detailed(candidate, "nhíp gắp sticker")
    assert score.relevance == 0.0
    assert "video" in score.missing_mandatory_intents
