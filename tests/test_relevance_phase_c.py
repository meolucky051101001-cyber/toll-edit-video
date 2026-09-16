import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.schemas.contracts import VideoResult
from backend.services import translator
from backend.services.ranking_service import rank, rank_detailed
from backend.services.translator import (
    fast_online_translate,
    fast_online_translate_async,
    generate_tiered_queries,
    plan_query,
)


def test_query_plan_decomposition_winter():
    plan = plan_query("phối đồ mùa đông")
    assert "mua dong" in plan.mandatory_attributes
    assert plan.subject == "phoi do"
    assert len(plan.tiered_queries) <= 3
    for q in plan.tiered_queries:
        assert "冬" in q, f"Expected '冬' in tiered query '{q}'"


def test_query_plan_decomposition_stationery_cute():
    plan = plan_query("unbox van phong pham cute")
    assert plan.subject == "van phong pham"
    assert plan.action == "unbox"
    assert "cute" in plan.modifiers
    assert len(plan.tiered_queries) == 3
    # All 3 tiered queries must retain stationery and unbox
    for q in plan.tiered_queries:
        assert "文具" in q, f"Expected '文具' in '{q}'"
        assert "开箱" in q, f"Expected '开箱' in '{q}'"
    # Ensure all 3 tiered queries are unique
    assert len(set(plan.tiered_queries)) == 3


def test_tiered_queries_budget_cap():
    queries = generate_tiered_queries("ubox do cute")
    assert 1 <= len(queries) <= 3
    assert len(set(queries)) == len(queries)
    for q in queries:
        assert "开箱" in q


def test_weighted_ranking_penalizes_missing_core():
    # Video only has generic terms (cute, vlog), missing core subject (fashion / winter)
    v_generic = VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/111",
        title="可爱少女心日常vlog分享",
    )
    rel, quality, final = rank(v_generic, "phối đồ mùa đông", ["冬季穿搭", "冬日OOTD"])
    assert rel <= 20.0, f"Expected heavy penalty for missing core topic, got: {rel}"


def test_weighted_ranking_rewards_exact_core_bigrams():
    # Video matches core bigrams (冬季, 穿搭)
    v_relevant = VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/222",
        title="冬季穿搭保暖显瘦日常搭配",
    )
    detail = rank_detailed(v_relevant, "phối đồ mùa đông", ["冬季穿搭", "冬日OOTD"])
    assert detail.relevance >= 75.0, f"Expected high relevance for exact core match, got: {detail.relevance}"
    assert "冬" in " ".join(detail.matched_tokens)
    assert "穿搭" in " ".join(detail.matched_tokens)


@pytest.mark.anyio
async def test_async_online_translate_cache():
    translator._ONLINE_TRANSLATE_CACHE["test_keyword_cache"] = "测试缓存"
    res = await fast_online_translate_async("test_keyword_cache")
    assert res == "测试缓存"
    sync_res = fast_online_translate("test_keyword_cache")
    assert sync_res == "测试缓存"


def test_search_expand_endpoint_offline_dictionary():
    client = TestClient(app)
    response = client.post("/api/search/expand", json={"query": "phối đồ mùa đông", "use_ai": False})
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "dictionary"
    assert data["plan"] is not None
    assert "mua dong" in data["plan"]["mandatory_attributes"]
    assert all("冬" in q for q in data["queries"])
