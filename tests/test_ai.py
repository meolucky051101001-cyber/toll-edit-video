import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from backend.core.config import Settings
from backend.models import SearchJob, SearchPlan, SearchQuery
from backend.providers.ai.base import AIError, AIProvider
from backend.providers.ai.gemini import GeminiAIProvider
from backend.schemas.contracts import SearchRequest
from backend.schemas.expansion import QueryExpansion
from backend.services.ai_service import AIService
from backend.services.search_service import SearchService
from tests.test_pipeline import make_job


def config(**values):
    return Settings(
        _env_file=None,
        **dict(
            ai_provider="gemini",
            ai_api_key=SecretStr("test-secret"),
            ai_model="gemini-3.1-flash-lite",
            ai_free_tier_confirmed=True,
            ai_min_interval=0,
            **values,
        ),
    )


def payload(query="unbox đồ cute"):
    return dict(
        original_query=query,
        translated_query="可爱开箱",
        primary_keywords=["文具开箱", "可爱开箱"],
        related_keywords=["萌物"],
        hashtags=["#开箱", "开箱"],
        negative_keywords=[],
        topics=["文具"],
    )


class FakeAI(AIProvider):
    name = "test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def expand(self, query):
        self.calls += 1
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_expansion_bounds_and_normalization():
    expansion = QueryExpansion(**payload())
    assert expansion.hashtags == ["开箱"]
    assert expansion.queries(2) == ["可爱开箱", "文具开箱"]
    for patch in [
        {"translated_query": "English only"},
        {"primary_keywords": ["x"] * 6},
        {"negative_keywords": ["#"]},
        {"unexpected": "x"},
    ]:
        with pytest.raises(ValidationError):
            QueryExpansion(**(payload() | patch))
    assert SearchRequest(query="x", selected_queries=[" a ", "a"]).selected_queries == ["a"]
    for terms in [[""], ["x"] * 11, ["x" * 301]]:
        with pytest.raises(ValidationError):
            SearchRequest(query="x", selected_queries=terms)


def test_retry_validation_cache_and_copy():
    async def scenario():
        fake = FakeAI(["not JSON", json.dumps(payload("changed")), json.dumps(payload())])
        service = AIService(config(), lambda _: fake)
        result = await service.expand("unbox đồ cute")
        assert result.source == "gemini" and fake.calls == 3
        result.queries.clear()
        cached = await service.expand("  unbox   đồ cute  ")
        assert cached.cached and cached.queries and fake.calls == 3

    asyncio.run(scenario())


def test_exhausted_retries_and_quota_cooldown():
    async def scenario():
        fake = FakeAI(["{}"])  # Invalid JSON structure, never accepted as real AI.
        service = AIService(config(), lambda _: fake)
        fake.responses = ["{}"] * 3
        result = await service.expand("x")
        assert result.source == "fallback" and result.queries == ["x"] and fake.calls == 3
        assert result.error_code == "invalid_json"
        quota = FakeAI([AIError("quota", "Quota reached")])
        service = AIService(config(), lambda _: quota)
        assert (await service.expand("x")).error_code == "quota"
        assert (await service.expand("y")).error_code == "quota"
        assert quota.calls == 1

    asyncio.run(scenario())


def test_adapter_contract_and_secret_not_in_url():
    def handler(request):
        assert request.headers["x-goog-api-key"] == "test-secret"
        assert "test-secret" not in str(request.url) and not request.url.query
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        assert body["generationConfig"]["responseJsonSchema"]["additionalProperties"] is False
        assert "test-secret" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"thought": True, "text": "private reasoning"},
                                {"text": json.dumps(payload())},
                            ]
                        },
                    }
                ]
            },
        )

    raw = asyncio.run(
        GeminiAIProvider(config(), httpx.MockTransport(handler)).expand("unbox đồ cute")
    )
    assert QueryExpansion.model_validate_json(raw).translated_query == "可爱开箱"


@pytest.mark.parametrize(
    "status,code,retryable",
    [
        (400, "request_rejected", False),
        (401, "authentication", False),
        (403, "authentication", False),
        (404, "model_unavailable", False),
        (429, "quota", False),
        (503, "unavailable", True),
    ],
)
def test_provider_errors_do_not_expose_response(status, code, retryable):
    provider = GeminiAIProvider(
        config(), httpx.MockTransport(lambda _: httpx.Response(status, text="test-secret"))
    )
    with pytest.raises(AIError) as caught:
        asyncio.run(provider.expand("x"))
    assert caught.value.code == code and caught.value.retryable == retryable
    assert "test-secret" not in str(caught.value)


def test_provider_requires_free_confirmation_before_network():
    cfg = config()
    cfg.ai_free_tier_confirmed = False

    def unexpected(request):
        raise AssertionError("Must not send an unconfirmed request")

    with pytest.raises(AIError) as caught:
        asyncio.run(GeminiAIProvider(cfg, httpx.MockTransport(unexpected)).expand("x"))
    assert caught.value.code == "billing_unconfirmed"


def test_plan_persistence_and_query_budget(sessions):
    fake = FakeAI([json.dumps(payload())])
    service = SearchService(sessions, ai=AIService(config(), lambda _: fake))
    job_id = make_job(sessions, 20)
    with sessions() as db:
        db.add(SearchPlan(job_id=job_id, use_ai=True))
        db.commit()
    asyncio.run(service.run(job_id))
    with sessions() as db:
        plan = db.get(SearchPlan, job_id)
        job = db.get(SearchJob, job_id)
        assert plan.source == "gemini" and plan.expansion["translated_query"] == "可爱开箱"
        assert job.status == "completed" and 0 < job.processed_count <= 20
        assert all(n <= 10 for n in job.provider_counts.values())
        terms = list(db.scalars(select(SearchQuery).where(SearchQuery.job_id == job_id)))
        assert len(terms) <= 20 and all(t.query in plan.queries for t in terms)
    assert fake.calls == 1


def test_manual_queries_skip_ai_and_fallback_does_not_fail_search(sessions):
    async def scenario():
        fake = FakeAI([AIError("quota", "Quota reached")])
        service = SearchService(sessions, ai=AIService(config(), lambda _: fake))
        manual = make_job(sessions, 4)
        fallback = make_job(sessions, 4)
        with sessions() as db:
            db.add(SearchPlan(job_id=manual, use_ai=True, queries=["文具"], source="manual"))
            db.add(SearchPlan(job_id=fallback, use_ai=True))
            db.commit()
        await service.run(manual)
        assert fake.calls == 0
        await service.run(fallback)
        with sessions() as db:
            assert db.get(SearchJob, fallback).status == "completed"
            plan = db.get(SearchPlan, fallback)
            assert plan.source == "fallback" and plan.queries == ["unbox đồ cute"] and plan.warning

    asyncio.run(scenario())


def test_cancel_during_ai_releases_limiter_and_job(sessions):
    async def scenario():
        started = asyncio.Event()

        class WaitingAI(AIProvider):
            name = "waiting"

            async def expand(self, query):
                started.set()
                await asyncio.Event().wait()
                return ""

        ai = AIService(config(), lambda _: WaitingAI())
        service = SearchService(sessions, ai=ai)
        job_id = make_job(sessions)
        with sessions() as db:
            db.add(SearchPlan(job_id=job_id, use_ai=True))
            db.commit()
        service.submit(job_id)
        await asyncio.wait_for(started.wait(), 2)
        await service.cancel(job_id)
        assert ai.pending == 0 and not ai.lock.locked()
        with sessions() as db:
            assert db.get(SearchJob, job_id).status == "cancelled"

    asyncio.run(scenario())
