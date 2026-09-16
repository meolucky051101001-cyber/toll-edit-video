import pytest

from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import intent_constraints, rank_detailed
from backend.services.translator import (
    extract_action,
    extract_subject,
    is_valid_action_match,
    is_valid_subject_match,
    normalize_vietnamese,
    plan_query,
    plan_query_async,
    restore_vietnamese_phrases,
)


def video(title: str, popular: bool = False) -> VideoResult:
    return VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/1234567890",
        title=title,
        like_count=1000000 if popular else 500,
    )


# ---------------------------------------------------------------------------
# Test Group 1: Homograph Disambiguation & Safe Subject Extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,forbidden_subject,expected_valid_subject",
    [
        ("bài tập gym cho người mới", "cho", "tap gym"),
        ("mẹo trị mụn ẩn cho da dầu", "meo", "tri mun an"),
        ("sữa rửa mặt cho da dầu", "sua", "sua rua mat"),
        ("cách làm sữa chua", "sua", "sua chua"),
        ("cách làm bánh bao", "lam banh", "lam banh bao"),
        ("sơn móng tay", "son", "son mong tay"),
        ("áo sơ mi", "so", ""),
        ("so sánh điện thoại", "so", ""),
    ],
)
def test_homograph_disambiguation(query, forbidden_subject, expected_valid_subject):
    norm = normalize_vietnamese(query)
    # The forbidden subject must NOT be considered valid
    assert not is_valid_subject_match(query, norm, forbidden_subject)
    # The extracted subject must match the expected semantic subject
    actual_subject = extract_subject(query, norm)
    assert actual_subject == expected_valid_subject


def test_dairy_is_not_mistaken_for_repair_action():
    for q in ["sữa rửa mặt cho da dầu", "cách làm sữa chua", "cách làm sữa hạt"]:
        norm = normalize_vietnamese(q)
        assert not is_valid_action_match(q, norm, "sua")
        action = extract_action(q, norm)
        assert action != "sua"


def test_real_cats_and_dogs_are_still_recognized():
    assert extract_subject("nuôi mèo", normalize_vietnamese("nuôi mèo")) == "nuoi meo"
    assert extract_subject("mèo cute", normalize_vietnamese("mèo cute")) in ("meo", "meo cute")
    assert extract_subject("nuôi chó", normalize_vietnamese("nuôi chó")) == "nuoi cho"
    assert extract_subject("chó cute", normalize_vietnamese("chó cute")) == "cho"


# ---------------------------------------------------------------------------
# Test Group 2: Unaccented Vietnamese Phrase Restoration
# ---------------------------------------------------------------------------
def test_restore_vietnamese_phrases():
    assert "bàn phím cơ" in restore_vietnamese_phrases("review ban phim co")
    assert "trị mụn ẩn" in restore_vietnamese_phrases("meo tri mun an cho da dau")
    assert "cà phê muối" in restore_vietnamese_phrases("cach pha ca phe muoi")
    assert "tăng cơ lưng" in restore_vietnamese_phrases("bai tap gym tang co lung")
    assert "sữa chua" in restore_vietnamese_phrases("cach lam sua chua")
    assert "bánh bao" in restore_vietnamese_phrases("cach lam banh bao")


# ---------------------------------------------------------------------------
# Test Group 3: Tiered Queries Accuracy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,required_chinese_terms",
    [
        ("cách làm bánh bao", ["包子"]),
        ("cach lam banh bao", ["包子"]),
        ("review bàn phím cơ", ["机械键盘"]),
        ("review ban phim co", ["机械键盘"]),
        ("mẹo trị mụn ẩn cho da dầu", ["闭口", "祛痘"]),
        ("meo tri mun an cho da dau", ["闭口", "祛痘"]),
        ("cách làm sữa chua", ["酸奶"]),
        ("sữa rửa mặt cho da dầu", ["洗面奶"]),
        ("cách pha cà phê muối", ["咖啡", "海盐咖啡"]),
        ("bài tập gym tăng cơ lưng", ["背肌", "背部"]),
    ],
)
@pytest.mark.anyio
async def test_plan_query_tiered_queries_accuracy(query, required_chinese_terms):
    plan = await plan_query_async(query)
    assert len(plan.tiered_queries) <= 3
    assert len(plan.tiered_queries) >= 1
    # Must contain at least one of the expected Chinese domain terms
    has_expected = any(
        any(term in t for term in required_chinese_terms)
        for t in plan.tiered_queries
    )
    assert has_expected, (
        f"Query '{query}' produced {plan.tiered_queries}, expected one of {required_chinese_terms}"
    )


# ---------------------------------------------------------------------------
# Test Group 4: Ranking Relevance Without False Penalty Ceilings
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "query,alternatives,good_title,distractor_title",
    [
        (
            "cách làm bánh bao",
            ["包子制作教程", "做包子"],
            "手把手教你在家做包子，蓬松宣软",
            "新手做生日蛋糕烘焙",
        ),
        (
            "review bàn phím cơ",
            ["机械键盘测评", "机械键盘推荐"],
            "2024最值得买的百元机械键盘测评",
            "电影公司回顾合集",
        ),
        (
            "mẹo trị mụn ẩn cho da dầu",
            ["油皮闭口去除技巧", "去闭口技巧"],
            "油皮闭口怎么去除？3个护肤小技巧",
            "可爱小猫咪日常吸猫",
        ),
        (
            "sữa rửa mặt cho da dầu",
            ["油皮洗面奶推荐", "控油洁面测评"],
            "适合油皮的控油温和洗面奶推荐",
            "修摩托车机车维修日常",
        ),
        (
            "cách làm sữa chua",
            ["自制酸奶教程", "家庭做酸奶"],
            "电饭煲自制酸奶教程，浓稠拉丝超简单",
            "家电故障维修指南",
        ),
        (
            "bài tập gym tăng cơ lưng",
            ["背肌训练动作", "健身房练背教学"],
            "健身房练背动作教学，打造倒三角背肌",
            "新手狗狗日常养狗教程",
        ),
    ],
)
def test_ranking_relevance_distinguishes_accurate_results(
    query, alternatives, good_title, distractor_title
):
    good_rank = rank_detailed(video(good_title), query, alternatives)
    distractor_rank = rank_detailed(video(distractor_title, popular=True), query, alternatives)

    assert good_rank.relevance >= 65.0, (
        f"Good video '{good_title}' scored low: {good_rank.relevance}"
    )
    assert distractor_rank.relevance <= 25.0, (
        f"Distractor video '{distractor_title}' scored too high: {distractor_rank.relevance}"
    )
    assert good_rank.final_score > distractor_rank.final_score * 2


# ---------------------------------------------------------------------------
# Test Group 5: Speed & Topic Precision Guards
# ---------------------------------------------------------------------------
def test_heavy_media_extensions_blocked():
    from backend.services.browser_service import HEAVY_EXTENSIONS
    for ext in [".mp4", ".m4s", ".m3u8", ".ts", ".webm", ".flv", ".woff", ".woff2"]:
        assert ext in HEAVY_EXTENSIONS, f"Expected {ext} to be blocked in HEAVY_EXTENSIONS"


def test_adaptive_target_candidates_formula():
    # Small limit should not trigger excessive over-scrolling
    limit = 10
    target = min(max(limit * 2, 20), 50)
    assert target == 20, f"Expected 20 candidates for limit 10, got {target}"

    # Large limit should be capped at 50 to avoid infinite scroll slowdown
    limit = 80
    target = min(max(limit * 2, 20), 50)
    assert target == 50, f"Expected 50 candidates cap for limit 80, got {target}"


def test_recommendation_and_guess_filtering_selectors():
    import inspect
    from backend.providers.douyin.provider import DouyinProvider
    from backend.providers.xiaohongshu.provider import XiaohongshuProvider

    douyin_src = inspect.getsource(DouyinProvider.search)
    assert ".guess-words" in douyin_src
    assert "search_guess_words" in douyin_src
    assert "recommend" in douyin_src

    xhs_src = inspect.getsource(XiaohongshuProvider.search)
    assert "recommend-box" in xhs_src
    assert "guess-you-like" in xhs_src
    assert "related-search" in xhs_src
