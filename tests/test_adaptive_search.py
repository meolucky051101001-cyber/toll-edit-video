import asyncio
from unittest.mock import patch

import pytest
from backend.models import SearchJob
from backend.providers.base import SearchFilters, SearchProvider
from backend.schemas.contracts import VideoResult
from backend.services.ranking_service import rank_detailed
from backend.services.search_service import SearchService


class ControllableProvider(SearchProvider):
    def __init__(self, platform: str, yield_counts: list[int]):
        self.platform = platform
        self.yield_counts = list(yield_counts)
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int, filters: SearchFilters, on_batch=None) -> list[VideoResult]:
        self.calls.append((query, limit))
        count = self.yield_counts.pop(0) if self.yield_counts else limit
        actual = min(count, limit)
        results = []
        for i in range(actual):
            vr = VideoResult(
                platform=self.platform,
                platform_video_id=f"pvid_{query}_{i}",
                url=f"https://www.rednote.com/explore/{query}_{i}",
                title=f"Video for {query} #{i}",
                is_mock=False,
            )
            results.append(vr)
        if on_batch:
            await on_batch(results)
        return results

    async def close(self) -> None:
        pass


def test_consensus_bonus_boosts_videos_matching_multiple_queries():
    vr = VideoResult(
        platform="xiaohongshu",
        url="https://www.rednote.com/explore/123",
        title="机械键盘评测 沉浸式打字音",
        caption="超好用的客制化机械键盘",
        is_mock=False,
    )
    single_res = rank_detailed(vr, "机械键盘")
    multi_res = rank_detailed(vr, "机械键盘", alternative_queries=["键盘评测", "客制化键盘"])

    assert multi_res.relevance >= single_res.relevance + 4.5
    assert multi_res.final_score > single_res.final_score


def test_consensus_bonus_does_not_break_intent_ceiling():
    distractor = VideoResult(
        platform="douyin",
        url="https://www.douyin.com/video/999",
        title="手表维修保养指南",
        like_count=50000,
        is_mock=False,
    )
    res = rank_detailed(distractor, "unbox đồng hồ", alternative_queries=["手表开箱", "腕表"])
    assert res.relevance <= 45.0


def test_adaptive_harvest_stops_early_when_primary_query_satisfies_quota(sessions):
    provider = ControllableProvider("xiaohongshu", yield_counts=[10, 10, 10])

    def factory(plat, is_mock):
        return provider

    service = SearchService(sessions, factory=factory)

    with sessions() as db:
        job = SearchJob(
            original_query="bàn phím cơ",
            platforms=["xiaohongshu"],
            requested_limit=10,
            is_mock=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with patch.object(service, "prepare_queries", return_value=["机械键盘", "客制化键盘", "打字音"]):
        asyncio.run(service.run(job_id))

    with sessions() as db:
        completed_job = db.get(SearchJob, job_id)
        assert completed_job.status == "completed"
        assert completed_job.processed_count == 10

    assert len(provider.calls) == 1
    assert provider.calls[0] == ("机械键盘", 10)


def test_adaptive_harvest_cascades_to_fill_deficit(sessions):
    provider = ControllableProvider("xiaohongshu", yield_counts=[4, 6, 10])

    def factory(plat, is_mock):
        return provider

    service = SearchService(sessions, factory=factory)

    with sessions() as db:
        job = SearchJob(
            original_query="bàn phím cơ",
            platforms=["xiaohongshu"],
            requested_limit=10,
            is_mock=False,
        )
        db.add(job)
        db.commit()
        job_id = job.id

    with patch.object(service, "prepare_queries", return_value=["机械键盘", "客制化键盘", "打字音"]):
        asyncio.run(service.run(job_id))

    with sessions() as db:
        completed_job = db.get(SearchJob, job_id)
        assert completed_job.status == "completed"
        assert completed_job.processed_count == 10

    assert len(provider.calls) == 2
    assert provider.calls[0] == ("机械键盘", 10)
    assert provider.calls[1] == ("客制化键盘", 6)
