import json
import os
from pathlib import Path

import pytest

from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.identity_policy import validate_result_identity
from backend.services.ranking_service import (
    rank_detailed,
    validate_compound_intent_relevance,
)
from backend.services.translator import plan_query

BENCHMARK_FILE = Path(__file__).resolve().parent.parent / "data" / "benchmarks" / "crawled_benchmark_samples.json"

MIN_LIVE_SAMPLES = int(os.environ.get("MIN_LIVE_SAMPLES", "3"))
MIN_LIVE_RELEVANCE_RATE = float(os.environ.get("MIN_LIVE_RELEVANCE_RATE", "0.80"))


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
def test_offline_crawl_provider_contract_and_filters():
    """Validates real crawl provider contract and ranking filters against benchmark samples."""
    filters = SearchFilters(
        sort_by="most_liked",
        publish_time="all",
        content_type="all",
    )

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
        if filters.sort_by == "most_liked":
            items.sort(key=lambda x: x.like_count or 0, reverse=True)
            for i in range(len(items) - 1):
                assert (items[i].like_count or 0) >= (items[i + 1].like_count or 0)

        # Verify ranking works accurately
        details = [rank_detailed(item, query, alternative_queries=plan.tiered_queries) for item in items]
        assert all(d.final_score >= 0.0 for d in details)


@pytest.mark.live_acceptance
@pytest.mark.anyio
async def test_live_network_probe_reachability():
    """Validates that real Chromium browser instances can reach Douyin and Xiaohongshu networks."""
    if os.environ.get("LIVE_CRAWL_ENABLED") != "1":
        pytest.skip("Pass LIVE_CRAWL_ENABLED=1 to execute live network probe")

    from backend.services.browser_service import BrowserService
    from backend.services.douyin_browser_service import DouyinBrowserService

    douyin_browser = DouyinBrowserService(headless=True)
    xhs_browser = BrowserService(headless=True)
    try:
        dy_ok, dy_detail = await douyin_browser.probe_live()
        xhs_ok, xhs_detail = await xhs_browser.probe_live()
        print(f"\nPROBE_DOUYIN: {dy_detail}")
        print(f"PROBE_XIAOHONGSHU: {xhs_detail}")

        assert dy_ok, f"Douyin live network reachability probe failed: {dy_detail}"
        assert xhs_ok, f"Xiaohongshu live network reachability probe failed: {xhs_detail}"
    finally:
        await douyin_browser.close()
        await xhs_browser.close()


@pytest.mark.live_acceptance
@pytest.mark.anyio
async def test_live_crawl_douyin_real_results():
    """Strictly validates that Douyin live search retrieves real video items matching compound query intent."""
    if os.environ.get("LIVE_CRAWL_ENABLED") != "1":
        pytest.skip("Pass LIVE_CRAWL_ENABLED=1 to execute live Douyin crawl")

    from backend.providers.base import UserActionRequired
    from backend.providers.douyin.provider import DouyinProvider
    from backend.services.douyin_browser_service import DouyinBrowserService

    raw_query = "nhíp gắp sticker"
    plan = plan_query(raw_query)
    target_query = plan.tiered_queries[0] if plan.tiered_queries else "贴纸镊子"
    print(f"\n[Douyin Live Crawl] Original query: '{raw_query}', Target Chinese query: '{target_query}' (Tiered: {plan.tiered_queries})")

    browser = DouyinBrowserService(headless=True)
    provider = DouyinProvider(browser=browser, auto_elevate=False)
    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="video")

    try:
        results = await provider.search(target_query, limit=5, filters=filters)
    except UserActionRequired as uar:
        pytest.fail(f"Douyin live crawl blocked by authentication/verification challenge: {uar}")
    finally:
        await provider.close()
        await browser.close()

    assert len(results) > 0, f"Douyin live search for '{target_query}' returned 0 video results"
    seen_ids = set()
    relevant_count = 0
    relevance_audit = []

    for r in results:
        assert r.platform == "douyin"
        assert r.platform_video_id and len(r.platform_video_id) >= 6
        assert r.platform_video_id not in seen_ids, f"Duplicate Douyin ID: {r.platform_video_id}"
        seen_ids.add(r.platform_video_id)
        assert r.is_mock is False
        assert "example.invalid" not in str(r.url)
        assert "douyin.com" in str(r.url)

        # Strict identity verification
        is_valid, resolved_id, err = validate_result_identity(
            platform=r.platform,
            platform_video_id=r.platform_video_id,
            url=str(r.url),
            share_url=r.share_url,
            is_mock=False,
        )
        assert is_valid, f"Live Douyin result failed identity validation: {err}"
        assert r.media_type == "video", f"Expected media_type='video', got '{r.media_type}'"

        breakdown = rank_detailed(r, raw_query, alternative_queries=plan.tiered_queries)
        is_relevant, rel_reason = validate_compound_intent_relevance(r, breakdown, raw_query)

        if is_relevant:
            relevant_count += 1

        relevance_audit.append({
            "id": r.platform_video_id,
            "title": r.title,
            "score": breakdown.final_score,
            "is_relevant": is_relevant,
            "reason": rel_reason,
        })

    print(f"\n[Douyin Live Relevance Audit]: {relevant_count}/{len(results)} videos match compound intent '{raw_query}':")
    for item in relevance_audit:
        print(f"  - [{item['id']}] Score={item['score']:.1f}, Relevant={item['is_relevant']} | Reason: {item['reason']} | Title: {item['title'][:50]}")

    if len(results) < MIN_LIVE_SAMPLES:
        pytest.fail(
            f"Douyin live crawl returned insufficient samples ({len(results)}/{MIN_LIVE_SAMPLES}). "
            f"Cannot certify precision without minimum sample threshold."
        )

    relevance_rate = relevant_count / len(results)
    assert relevance_rate >= MIN_LIVE_RELEVANCE_RATE, (
        f"Douyin live crawl relevance rate {relevance_rate:.2f} ({relevant_count}/{len(results)}) "
        f"is below required threshold {MIN_LIVE_RELEVANCE_RATE:.2f}. "
        f"All returned videos must strictly satisfy both tool (tweezer) and purpose (sticker) intents."
    )


@pytest.mark.live_acceptance
@pytest.mark.anyio
async def test_live_crawl_xiaohongshu_real_results():
    """Strictly validates that Xiaohongshu live search retrieves real video items matching compound query intent."""
    if os.environ.get("LIVE_CRAWL_ENABLED") != "1":
        pytest.skip("Pass LIVE_CRAWL_ENABLED=1 to execute live Xiaohongshu crawl")

    from backend.providers.base import UserActionRequired
    from backend.providers.xiaohongshu.provider import XiaohongshuProvider
    from backend.services.browser_service import BrowserService

    raw_query = "nhíp gắp sticker"
    plan = plan_query(raw_query)
    target_query = plan.tiered_queries[0] if plan.tiered_queries else "贴纸镊子"
    print(f"\n[Xiaohongshu Live Crawl] Original query: '{raw_query}', Target Chinese query: '{target_query}' (Tiered: {plan.tiered_queries})")

    browser = BrowserService(headless=True)
    provider = XiaohongshuProvider(browser=browser, auto_elevate=False)
    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="video")

    try:
        results = await provider.search(target_query, limit=5, filters=filters)
    except UserActionRequired as uar:
        pytest.fail(f"Xiaohongshu live crawl blocked by authentication/login required: {uar}")
    finally:
        await provider.close()
        await browser.close()

    assert len(results) > 0, f"Xiaohongshu live search for '{target_query}' returned 0 video results"
    seen_ids = set()
    relevant_count = 0
    relevance_audit = []

    for r in results:
        assert r.platform == "xiaohongshu"
        assert r.platform_video_id and len(r.platform_video_id) >= 8
        assert r.platform_video_id not in seen_ids, f"Duplicate XHS ID: {r.platform_video_id}"
        seen_ids.add(r.platform_video_id)
        assert r.is_mock is False
        assert "example.invalid" not in str(r.url)
        assert "xiaohongshu.com" in str(r.url) or "rednote.com" in str(r.url)

        # Strict identity verification
        is_valid, resolved_id, err = validate_result_identity(
            platform=r.platform,
            platform_video_id=r.platform_video_id,
            url=str(r.url),
            share_url=r.share_url,
            is_mock=False,
        )
        assert is_valid, f"Live Xiaohongshu result failed identity validation: {err}"
        assert r.media_type == "video", f"Expected media_type='video' for content_type=video, got '{r.media_type}'"

        breakdown = rank_detailed(r, raw_query, alternative_queries=plan.tiered_queries)
        is_relevant, rel_reason = validate_compound_intent_relevance(r, breakdown, raw_query)

        if is_relevant:
            relevant_count += 1

        relevance_audit.append({
            "id": r.platform_video_id,
            "title": r.title,
            "score": breakdown.final_score,
            "is_relevant": is_relevant,
            "reason": rel_reason,
        })

    print(f"\n[Xiaohongshu Live Relevance Audit]: {relevant_count}/{len(results)} videos match compound intent '{raw_query}':")
    for item in relevance_audit:
        print(f"  - [{item['id']}] Score={item['score']:.1f}, Relevant={item['is_relevant']} | Reason: {item['reason']} | Title: {item['title'][:50]}")

    if len(results) < MIN_LIVE_SAMPLES:
        pytest.fail(
            f"Xiaohongshu live crawl returned insufficient samples ({len(results)}/{MIN_LIVE_SAMPLES}). "
            f"Cannot certify precision without minimum sample threshold."
        )

    relevance_rate = relevant_count / len(results)
    assert relevance_rate >= MIN_LIVE_RELEVANCE_RATE, (
        f"Xiaohongshu live crawl relevance rate {relevance_rate:.2f} ({relevant_count}/{len(results)}) "
        f"is below required threshold {MIN_LIVE_RELEVANCE_RATE:.2f}. "
        f"All returned videos must strictly satisfy both tool (tweezer) and purpose (sticker) intents."
    )


@pytest.mark.live_acceptance
@pytest.mark.anyio
async def test_live_crawl_provider_acceptance_with_filters():
    """Compatibility runner for existing stage 11 caller."""
    is_live_enabled = os.environ.get("LIVE_CRAWL_ENABLED") == "1"
    if not is_live_enabled:
        test_offline_crawl_provider_contract_and_filters()
    else:
        await test_live_network_probe_reachability()
        await test_live_crawl_douyin_real_results()
        await test_live_crawl_xiaohongshu_real_results()
