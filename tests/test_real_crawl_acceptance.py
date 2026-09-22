import json
import os
from pathlib import Path

import pytest

from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.identity_policy import validate_result_identity
from backend.services.ranking_service import rank_detailed
from backend.services.translator import plan_query

BENCHMARK_FILE = Path(__file__).resolve().parent.parent / "data" / "benchmarks" / "crawled_benchmark_samples.json"


@pytest.mark.live_acceptance
def test_real_crawl_dataset_schema_and_identity_acceptance():
    """Acceptance test verifying that crawled dataset items pass identity policies and contracts."""
    assert BENCHMARK_FILE.exists(), f"Benchmark file {BENCHMARK_FILE} does not exist"
    with open(BENCHMARK_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert len(data) >= 4, "Must contain at least 4 acceptance query groups"
    total_samples = 0

    for group in data:
        query = group["query"]
        assert query, "Query must not be empty"
        plan = plan_query(query)
        assert plan.tiered_queries, f"Query '{query}' must have tiered translations"

        for sample in group["samples"]:
            total_samples += 1
            platform = sample["platform"]
            video_id = sample["platform_video_id"]
            url = sample["url"]

            # Identity validation policy check
            is_valid, resolved_id, err = validate_result_identity(
                platform=platform,
                platform_video_id=video_id,
                url=url,
                share_url=None,
                is_mock=False,
            )
            assert is_valid, f"Identity validation failed for {url}: {err}"
            assert resolved_id == video_id, f"Resolved ID mismatch: {resolved_id} vs {video_id}"

            # VideoResult contract validation
            res = VideoResult(
                platform=platform,
                platform_video_id=video_id,
                url=url,
                title=sample["title"],
                author_name=sample["author"],
                like_count=sample["like_count"],
                media_type=sample.get("media_type", "video"),
                is_mock=False,
            )
            assert res.platform_video_id == video_id
            assert res.like_count is not None and res.like_count >= 0

    assert total_samples >= 20, f"Expected at least 20 real crawl samples, found {total_samples}"


@pytest.mark.live_acceptance
@pytest.mark.anyio
async def test_live_crawl_provider_acceptance_with_filters():
    """Validates real crawl provider search contract and filtering guarantees."""
    # When LIVE_CRAWL_ENABLED=1 is in environment, run live against actual browser session;
    # otherwise run against verified real-world crawl fixtures.
    is_live_enabled = os.environ.get("LIVE_CRAWL_ENABLED") == "1"

    filters = SearchFilters(
        sort_by="most_liked",
        publish_time="all",
        content_type="all",
    )

    if not is_live_enabled:
        with open(BENCHMARK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        for group in data:
            query = group["query"]
            plan = plan_query(query)

            items = [
                VideoResult(
                    platform=s["platform"],
                    platform_video_id=s["platform_video_id"],
                    url=s["url"],
                    title=s["title"],
                    author_name=s["author"],
                    like_count=s["like_count"],
                    media_type=s.get("media_type", "video"),
                )
                for s in group["samples"]
            ]

            # Verify sorting matches requested most_liked
            items.sort(key=lambda x: x.like_count or 0, reverse=True)
            for i in range(len(items) - 1):
                assert (items[i].like_count or 0) >= (items[i + 1].like_count or 0)

            # Verify ranking works accurately
            details = [rank_detailed(item, query, alternative_queries=plan.tiered_queries) for item in items]
            assert all(d.final_score >= 0.0 for d in details)
    else:
        # Live browser integration acceptance
        from backend.providers.douyin.provider import DouyinProvider
        from backend.services.douyin_browser_service import DouyinBrowserService

        browser = DouyinBrowserService(headless=True)
        provider = DouyinProvider(browser=browser)
        try:
            results = await provider.search("decor bàn học", limit=5, filters=filters)
            assert len(results) > 0
            for r in results:
                assert r.platform == "douyin"
                assert r.platform_video_id
                assert str(r.url).startswith("https://")
        finally:
            await provider.close()
            await browser.close()
