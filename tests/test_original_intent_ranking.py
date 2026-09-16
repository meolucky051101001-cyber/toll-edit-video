import pytest

from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed


def video(title, popular=False):
    return VideoResult(
        platform="douyin", url="https://www.douyin.com/video/1234567890123456789",
        title=title, like_count=1000000 if popular else 5,
    )


@pytest.mark.parametrize("query,alternatives,good,bad,ceiling", [
    ("unbox văn phòng phẩm cute", ["文具开箱", "开箱"], "可爱文具开箱", "手表开箱", 20),
    ("文具开箱", ["文具开箱", "开箱"], "文具开箱", "手表开箱", 20),
    ("phối đồ mùa đông", ["冬季穿搭", "穿搭"], "冬季穿搭", "夏季穿搭", 40),
    ("冬季穿搭", ["冬季穿搭", "穿搭"], "冬季穿搭", "夏季穿搭", 40),
    ("review đồng hồ giá rẻ", ["平价手表测评", "手表测评"], "平价手表测评", "手表测评", 40),
    ("unbox đồng hồ", ["手表开箱", "手表"], "手表开箱", "手表维修", 45),
])
def test_expansion_cannot_erase_original_intent(query, alternatives, good, bad, ceiling):
    relevant = rank_detailed(video(good), query, alternatives)
    distractor = rank_detailed(video(bad, popular=True), query, alternatives)
    assert distractor.relevance <= ceiling
    assert relevant.final_score > distractor.final_score
    assert distractor.missing_core_tokens


def test_vietnamese_accents_and_equivalent_action():
    detail = rank_detailed(video("Đập hộp đồng hồ"), "unbox đồng hồ", ["đập hộp đồng hồ"])
    assert detail.relevance > 75


def test_watch_is_not_mistaken_for_winter():
    detail = rank_detailed(video("腕表开箱"), "unbox đồng hồ", ["腕表开箱"])
    assert detail.relevance > 75


def test_unknown_topic_keeps_lexical_matching():
    detail = rank_detailed(video("quasar spectroscopy"), "quasar spectroscopy")
    assert detail.relevance == 100


def test_popularity_cannot_create_relevance():
    detail = rank_detailed(video("cooking pasta", popular=True), "quasar spectroscopy")
    assert detail.final_score == 0
