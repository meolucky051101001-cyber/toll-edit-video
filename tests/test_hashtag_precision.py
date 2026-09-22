from datetime import datetime, timedelta, timezone

import pytest

from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.translator import (
    extract_hashtag_intent,
    plan_query,
    plan_query_async,
)


def make_test_video(
    title: str,
    caption: str = "",
    hashtags: list[str] | None = None,
    like_count: int = 500,
) -> VideoResult:
    return VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/1122334455",
        title=title,
        caption=caption,
        hashtags=hashtags or [],
        like_count=like_count,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    )


# ---------------------------------------------------------------------------
# Test 1: Hashtag Intent Extraction & Unspaced Segmentation
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw_input,expected_clean,expected_tags",
    [
        ("#decorbanhoc", "decor bàn học", ["decorbanhoc"]),
        ("#banphimco", "bàn phím cơ", ["banphimco"]),
        ("phối đồ #ootd", "phối đồ ootd", ["ootd"]),
        ("#decor_ban_hoc", "decor bàn học", ["decor_ban_hoc"]),
        ("#duongda cho người mới", "dưỡng da cho người mới", ["duongda"]),
        ("#kemchongnang", "kem chống nắng", ["kemchongnang"]),
        ("#suaxe máy", "sửa xe máy", ["suaxe"]),
        ("#vintage", "vintage", ["vintage"]),
    ],
)
def test_extract_hashtag_intent(raw_input, expected_clean, expected_tags):
    clean, tags = extract_hashtag_intent(raw_input)
    assert clean == expected_clean
    assert tags == expected_tags


# ---------------------------------------------------------------------------
# Test 2: Query Planning with Hashtags
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_plan_query_with_hashtag_decorbanhoc():
    plan = await plan_query_async("#decorbanhoc")
    assert any("书桌布置" in q or "桌搭" in q for q in plan.tiered_queries)
    # Tiered queries for hashtag search must format with # for topic search
    assert any(q.startswith("#") for q in plan.tiered_queries)


@pytest.mark.anyio
async def test_plan_query_with_hashtag_banphimco():
    plan = await plan_query_async("#banphimco")
    assert any("机械键盘" in q for q in plan.tiered_queries)
    assert any(q.startswith("#") for q in plan.tiered_queries)


@pytest.mark.anyio
async def test_plan_query_with_hashtag_ootd():
    plan = await plan_query_async("#ootd")
    assert any("OOTD" in q or "穿搭" in q for q in plan.tiered_queries)


def test_plan_query_sync_with_hashtag():
    plan = plan_query("#decorbanhoc")
    assert any("书桌布置" in q or "桌搭" in q for q in plan.tiered_queries)


# ---------------------------------------------------------------------------
# Test 3: Ranking Boost for Video with Exact Matching Hashtag
# ---------------------------------------------------------------------------
def test_ranking_hashtag_boost_over_plain_caption():
    query = "decor bàn học"

    vid_with_hashtag = make_test_video(
        title="Góc bàn học nhỏ phong cách tối giản",
        caption="Chia sẻ góc học tập gọn gàng",
        hashtags=["decorbanhoc", "gocbanhoc"],
    )

    vid_without_hashtag = make_test_video(
        title="Góc bàn học nhỏ phong cách tối giản",
        caption="Chia sẻ góc học tập decor bàn học gọn gàng",
        hashtags=[],
    )

    detail_tag = rank_detailed(vid_with_hashtag, query)
    detail_notag = rank_detailed(vid_without_hashtag, query)

    # Video with matched hashtag receives hashtag boost and ranks higher
    assert detail_tag.relevance > detail_notag.relevance
    assert "hashtags" in detail_tag.provenance
    assert any("decorbanhoc" in h or "gocbanhoc" in h for h in detail_tag.provenance["hashtags"])


# ---------------------------------------------------------------------------
# Test 4: Strict Hashtag Search (#hashtag) Penalizes Video Without Hashtag
# ---------------------------------------------------------------------------
def test_strict_hashtag_search_penalizes_missing_hashtag():
    hashtag_query = "#decorbanhoc"

    matching_vid = make_test_video(
        title="Setup bàn học xinh xắn",
        caption="Góc làm việc decor bàn học",
        hashtags=["decorbanhoc"],
    )

    non_matching_vid = make_test_video(
        title="Setup bàn học xinh xắn",
        caption="Góc làm việc decor bàn học của mình hôm nay",
        hashtags=["vlog", "daily"],  # Missing #decorbanhoc
    )

    detail_match = rank_detailed(matching_vid, hashtag_query)
    detail_non_match = rank_detailed(non_matching_vid, hashtag_query)

    # Matching hashtag video gets strong score
    assert detail_match.relevance >= 70.0
    assert not detail_match.missing_mandatory_intents

    # Non-matching video is heavily penalized and flagged as missing mandatory hashtag
    assert detail_non_match.relevance <= 20.0
    assert any("#decorbanhoc" in m for m in detail_non_match.missing_mandatory_intents)


# ---------------------------------------------------------------------------
# Test 5: Exact Hashtag Filter Flag Enforcement
# ---------------------------------------------------------------------------
def test_exact_hashtag_filter_flag():
    filters = SearchFilters(exact_hashtag=True, target_hashtag="ootd")
    assert filters.exact_hashtag is True
    assert filters.target_hashtag == "ootd"

    matching_vid = make_test_video(
        title="Phối đồ phong cách Hàn Quốc",
        caption="Outfit hôm nay cực xinh",
        hashtags=["OOTD", "dailylook"],
    )

    lacking_vid = make_test_video(
        title="Phối đồ phong cách Hàn Quốc ootd",
        caption="Outfit hôm nay cực xinh ootd",
        hashtags=["koreanstyle"],
    )

    detail_match = rank_detailed(
        matching_vid,
        "phối đồ",
        exact_hashtag=filters.exact_hashtag,
        target_hashtag=filters.target_hashtag,
    )
    detail_lack = rank_detailed(
        lacking_vid,
        "phối đồ",
        exact_hashtag=filters.exact_hashtag,
        target_hashtag=filters.target_hashtag,
    )

    assert detail_match.relevance > 70.0
    assert detail_lack.relevance <= 20.0
    assert any("#ootd" in m for m in detail_lack.missing_mandatory_intents)
