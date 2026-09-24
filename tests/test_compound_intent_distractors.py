"""Tests for scoped compound intent validation on 'nhíp gắp sticker' with detailed distractors."""

import pytest

from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import (
    RankBreakdown,
    validate_compound_intent_relevance,
    validate_sticker_tweezer_compound_intent,
)


def _make_breakdown(score: float = 75.0, missing_mandatory: list[str] | None = None) -> RankBreakdown:
    return RankBreakdown(
        relevance=score,
        quality=70.0,
        final_score=score,
        missing_mandatory_intents=missing_mandatory or [],
    )


def test_compound_intent_accepts_valid_sticker_tweezer():
    """Accepts videos satisfying both tool (tweezer) and purpose (sticker/guka) intents."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000001",
        url="https://www.douyin.com/video/7350000000000000001",
        title="手账咕卡贴纸专用镊子开箱测评 弯头直头对比好用不伤纸",
        author_name="手账研究所",
        media_type="video",
    )
    bd = _make_breakdown(75.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is True
    assert "Satisfies both tool" in reason


def test_compound_intent_rejects_eyelash_tweezer_distractor():
    """Rejects eyelash tweezers lacking sticker purpose, even if containing tweezer terms."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000002",
        url="https://www.douyin.com/video/7350000000000000002",
        title="新手嫁接睫毛超轻开花镊子实测 手感轻巧好夹",
        author_name="美睫师小王",
        media_type="video",
    )
    bd = _make_breakdown(50.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "incompatible purpose" in reason.lower() or "missing mandatory purpose" in reason.lower()


def test_compound_intent_rejects_phone_repair_tweezer_distractor():
    """Rejects industrial/electronics repair tweezers lacking sticker craft purpose."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000003",
        url="https://www.douyin.com/video/7350000000000000003",
        title="修手机精密防静电尖头镊子实测 夹跳线稳准不打滑",
        author_name="数码维修老陈",
        media_type="video",
    )
    bd = _make_breakdown(45.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "incompatible purpose" in reason.lower() or "missing mandatory purpose" in reason.lower()


def test_compound_intent_rejects_sticker_without_tweezer_distractor():
    """Rejects sticker collection videos that completely lack tweezers."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000004",
        url="https://www.douyin.com/video/7350000000000000004",
        title="1000张可爱手账贴纸大合集 少女心爆棚每一张都想贴",
        author_name="贴纸杂货铺",
        media_type="video",
    )
    bd = _make_breakdown(40.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "tool" in reason.lower()


def test_compound_intent_rejects_planner_without_sticker_distractor():
    """Rejects journal/planner only (手账) without stickers or tweezers."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000005",
        url="https://www.douyin.com/video/7350000000000000005",
        title="复古牛皮纸手账本内页排版灵感分享 极简手绘记录日常",
        author_name="手账日常",
        media_type="video",
    )
    bd = _make_breakdown(30.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "no match" in reason.lower() or "missing" in reason.lower()


def test_compound_intent_rejects_caption_hashtag_only_spam_distractor():
    """Rejects videos where keywords appear only in caption/hashtags while title is completely unrelated."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000006",
        url="https://www.douyin.com/video/7350000000000000006",
        title="周末一人食沉浸式做减脂晚餐 牛排沙拉减脂餐制作",  # Title completely unrelated
        caption="今日做饭日常 #贴纸 #镊子 #咕卡",  # Keywords spammed in caption
        hashtags=["贴纸", "镊子", "咕卡"],
        author_name="美食日记",
        media_type="video",
    )
    bd = _make_breakdown(55.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "title" in reason.lower()


def test_compound_intent_rejects_image_text_notes():
    """Rejects non-video image/text notes even if keywords match."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000007",
        url="https://www.douyin.com/note/7350000000000000007",
        title="新手必看！5款手账贴纸镊子图解选购指南",
        author_name="好物课代表",
        media_type="image_text",
    )
    bd = _make_breakdown(65.0)
    is_rel, reason = validate_sticker_tweezer_compound_intent(vr, bd)
    assert is_rel is False
    assert "media_type" in reason.lower()


def test_compound_intent_unsupported_query_raises_value_error():
    """validate_compound_intent_relevance raises ValueError when invoked with queries other than 'nhíp gắp sticker'."""
    vr = VideoResult(
        platform="douyin",
        platform_video_id="7350000000000000008",
        url="https://www.douyin.com/video/7350000000000000008",
        title="百元内卷王机械键盘推荐",
        author_name="外设控",
        media_type="video",
    )
    bd = _make_breakdown(60.0)
    with pytest.raises(ValueError) as exc_info:
        validate_compound_intent_relevance(vr, bd, query="bàn phím cơ giá rẻ")
    assert "strictly scoped to 'nhíp gắp sticker'" in str(exc_info.value)
