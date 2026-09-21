import pytest

from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.translator import compound_intent, plan_query


@pytest.mark.parametrize("query", ["nhíp gắp sticker", "nhip gap sticker", "nhíp sticker cute", "sticker tweezers", "贴纸镊子"])
def test_tool_and_purpose_required(query):
    terms = plan_query(query).tiered_queries
    assert all("镊子" in q and "贴纸" in q for q in terms)
    def score(title):
        return rank_detailed(VideoResult(platform="douyin",url="https://www.douyin.com/video/1234567890123456789", title=title),query,terms)
    good = score("贴纸专用镊子 精密弯头")
    for title in ["可爱贴纸开箱 手账贴纸", "嫁接睫毛镊子", "好物推荐"]:
        bad = score(title)
        assert bad.relevance <= 20
        assert good.final_score > bad.final_score
        assert bad.missing_core_tokens


def test_compound_keeps_action_and_price():
    terms=plan_query("review nhíp gắp sticker giá rẻ").tiered_queries
    assert all("测评" in q and "平价" in q and "贴纸" in q and "镊子" in q for q in terms)


def test_substrings_do_not_trigger_compound():
    assert compound_intent("tweezers stickership") is None
    assert compound_intent("membership sticker") is None


def test_eyelash_is_not_sticker():
    assert all("睫毛" in q and "贴纸" not in q for q in plan_query("nhíp gắp mi").tiered_queries)
