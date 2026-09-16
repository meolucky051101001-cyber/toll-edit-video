import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import async_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.api.search import resume_job
from backend.core.database import Base
from backend.models import SearchJob, SearchPlan
from backend.providers.base import SearchFilters
from backend.providers.douyin.parser import parse_count_text, parse_detail
from backend.providers.douyin.provider import DETAIL_SCRIPT, DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import VideoResult
from backend.services import translator
from backend.services.search_service import SearchService


# ---------------------------------------------------------------------------
# Test 1: Douyin Gate Mapping & Resume HTTP 200 (Priority 1)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_douyin_gate_mapping_and_resume_200():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    for state in ["login_required", "verification_required", "restricted"]:
        class MockBrowser:
            async def inspect(self, page):
                return state

        real_provider = DouyinProvider(MockBrowser())

        class GateAdapter:
            async def search(self, query, limit, filters, on_batch=None):
                await real_provider.gate(None)

            async def close(self):
                pass

        service = SearchService(sessions, factory=lambda platform, mock: GateAdapter())

        with sessions() as db:
            job = SearchJob(
                original_query="文具",
                platforms=["douyin"],
                requested_limit=1,
                is_mock=False,
            )
            db.add(job)
            db.flush()
            db.add(SearchPlan(job_id=job.id, queries=["文具"]))
            db.commit()
            job_id = job.id

        service.submit(job_id)

        # Wait for job to enter waiting state
        for _ in range(100):
            if job_id in service.waiters:
                break
            await asyncio.sleep(0.01)

        with sessions() as db:
            job = db.get(SearchJob, job_id)
            assert job is not None
            if state == "login_required":
                assert job.status == "waiting_for_login"
            else:
                assert job.status == "waiting_for_user"

            assert job_id in service.waiters

            # Attempt resume - MUST return HTTP 200, never 409
            request = SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(search=service))
            )
            resp = await resume_job(job_id, request, db)
            assert resp.id == job_id

        await service.cancel(job_id)

    engine.dispose()


# ---------------------------------------------------------------------------
# Test 2: Douyin Detail Script and Parser Contract (Priority 2)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Test 2: Douyin Detail Parser Contract (Unit Test)
# ---------------------------------------------------------------------------
def test_douyin_detail_parser_contract():
    raw = {
        "has_video": True,
        "title": "Stationery #unbox",
        "caption": "Stationery #unbox",
        "description": "Stationery #unbox",
        "like_count": "1.2万",
        "like_text": "1.2万",
        "comment_count": "123",
        "comment_text": "123",
        "favorite_count": "456",
        "collect_text": "456",
        "share_count": "78",
        "share_text": "78",
        "duration_seconds": 20,
        "duration": 20,
        "thumbnail_url": "https://p1.douyinpic.com/test.jpg",
        "poster": "https://p1.douyinpic.com/test.jpg",
        "hashtags": [],
    }
    result = parse_detail(raw, "https://www.douyin.com/video/1234567890123456789")
    assert result is not None
    assert result.platform == "douyin"
    assert result.like_count == 12000
    assert result.comment_count == 123
    assert result.favorite_count == 456
    assert result.share_count == 78
    assert result.duration_seconds == 20.0
    assert result.caption == "Stationery #unbox"
    assert str(result.thumbnail_url) == "https://p1.douyinpic.com/test.jpg"
    assert "unbox" in result.hashtags


# ---------------------------------------------------------------------------
# Test 2b: Douyin Detail Script Browser Integration (Real JS Execution)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_douyin_detail_script_browser_integration():
    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
    except Exception as e:
        await playwright.stop()
        pytest.skip(f"Chrome browser not available in test environment: {e}")

    try:
        page = await browser.new_page()
        await page.route("**/*", lambda r: r.abort())
        await page.set_content(
            """
            <meta property="og:image" content="https://p1.douyinpic.com/test.jpg">
            <h1 data-e2e="video-desc">Stationery #unbox</h1>
            <video></video>
            <span data-e2e="like-count">1.2万</span>
            <span data-e2e="comment-count">123</span>
            <span data-e2e="favorite-count">456</span>
            <span data-e2e="share-count">78</span>
            """
        )
        await page.locator("video").evaluate(
            "v => Object.defineProperty(v, 'duration', {value: 20})"
        )
        # Evaluate JS extractor directly — NO try/except fallback!
        raw = await page.evaluate(DETAIL_SCRIPT)
    finally:
        await browser.close()
        await playwright.stop()

    # Assert raw extractor contract keys
    assert raw["has_video"] is True
    assert raw["caption"] == "Stationery #unbox"
    assert raw["description"] == "Stationery #unbox"
    assert raw["like_count"] == "1.2万"
    assert raw["comment_count"] == "123"
    assert raw["favorite_count"] == "456"
    assert raw["share_count"] == "78"
    assert raw["duration_seconds"] == 20
    assert raw["thumbnail_url"] == "https://p1.douyinpic.com/test.jpg"

    result = parse_detail(raw, "https://www.douyin.com/video/1234567890123456789")
    assert result is not None
    assert result.platform == "douyin"
    assert result.like_count == 12000
    assert result.comment_count == 123
    assert result.favorite_count == 456
    assert result.share_count == 78
    assert result.duration_seconds == 20.0
    assert result.caption == "Stationery #unbox"
    assert str(result.thumbnail_url) == "https://p1.douyinpic.com/test.jpg"
    assert "unbox" in result.hashtags


def test_douyin_count_parsing():
    assert parse_count_text("1.2万") == 12000
    assert parse_count_text("3.5w") == 35000
    assert parse_count_text("10k") == 10000
    assert parse_count_text("123") == 123
    assert parse_count_text("1,234") == 1234
    assert parse_count_text("赞") is None
    assert parse_count_text(None) is None


# ---------------------------------------------------------------------------
# Test 3: Subject Retention in Translation (Priority 3)
# ---------------------------------------------------------------------------
def test_subject_retention_in_translation():
    with patch.object(translator, "fast_online_translate", return_value=None):
        queries = {
            "nuôi mèo": translator.plan_query("nuôi mèo").tiered_queries,
            "sửa xe máy": translator.plan_query("sửa xe máy").tiered_queries,
            "unbox đồng hồ": translator.plan_query("unbox đồng hồ").tiered_queries,
            "phối đồ mùa đông giá rẻ": translator.plan_query("phối đồ mùa đông giá rẻ").tiered_queries,
            "review máy ảnh": translator.plan_query("review máy ảnh").tiered_queries,
            "unbox máy khoan": translator.plan_query("unbox máy khoan").tiered_queries,
        }

        # 1. nuôi mèo must retain cat/pet care
        assert any("养猫" in q for q in queries["nuôi mèo"])
        assert not any(q in ("测评", "沉浸式测评", "少女心", "开箱") for q in queries["nuôi mèo"])

        # 2. sửa xe máy must retain motorcycle/repair
        assert any(any(k in q for k in ("修摩托车", "摩托车维修", "机车维修", "修车")) for q in queries["sửa xe máy"])
        assert not any(q in ("测评", "沉浸式测评", "少女心", "开箱") for q in queries["sửa xe máy"])

        # 3. unbox đồng hồ must retain watch, no winter
        assert any(any(k in q for k in ("手表", "腕表")) for q in queries["unbox đồng hồ"])
        assert not any("冬" in q for q in queries["unbox đồng hồ"])

        # 4. phối đồ mùa đông giá rẻ must have winter AND budget
        for q in queries["phối đồ mùa đông giá rẻ"][:3]:
            assert any(w in q for w in ("冬", "冬季", "冬日"))
            assert any(b in q for b in ("平价", "百元", "性价比"))

        # 5. review máy ảnh must retain camera (相机), NOT generic action words
        assert any("相机" in q for q in queries["review máy ảnh"])
        assert not any(q in ("测评", "沉浸式测评", "少女心") for q in queries["review máy ảnh"])

        # 6. unbox máy khoan must retain drill (电钻), NOT generic action words
        assert any("电钻" in q for q in queries["unbox máy khoan"])
        assert not any(q in ("开箱", "沉浸式", "学生") for q in queries["unbox máy khoan"])

        # 7. Unmapped query without subject MUST fall back to raw query, never orphan generic terms
        unknown_plan = translator.plan_query("review máy bay phản lực")
        assert unknown_plan.tiered_queries == ["review máy bay phản lực"]


def test_unknown_subject_with_modifiers_preserves_raw_query():
    # When subject is unknown, modifiers (cute, đẹp) or actions (review, unbox)
    # must NOT hijack the query into generic terms (可爱可爱测评, 沉浸式好看, etc.)
    with patch.object(
        translator,
        "fast_online_translate",
        side_effect=AssertionError("Unexpected sync call"),
    ):
        test_queries = [
            "review máy in",
            "review máy in cute",
            "review máy in đẹp",
            "unbox kính thiên văn",
            "unbox kính thiên văn đẹp",
            "unbox kính thiên văn cute",
            "thiết kế cầu treo",
            "thiết kế cầu treo đẹp",
        ]
        for q in test_queries:
            plan = translator.plan_query(q)
            assert plan.tiered_queries == [q], (
                f"Query '{q}' degraded to generic: {plan.tiered_queries}"
            )
            assert not any(
                term
                in (
                    "测评",
                    "沉浸式测评",
                    "少女心",
                    "开箱",
                    "沉浸式",
                    "学生",
                    "可爱可爱测评",
                    "沉浸式好看",
                )
                for term in plan.tiered_queries
            )


# ---------------------------------------------------------------------------
# Test 4: Async Plan Zero Sync Fallback (Priority 3)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_async_plan_zero_sync_fallback():
    sync_calls = []
    async_calls = []

    def fake_sync(text):
        sync_calls.append(text)
        return None

    async def fake_async(text):
        async_calls.append(text)
        return "斜拉桥设计"

    with patch.object(translator, "fast_online_translate", side_effect=fake_sync),          patch.object(translator, "fast_online_translate_async", side_effect=fake_async):
        plan = await translator.plan_query_async("thiết kế cầu treo")

    # Sync translation must NEVER be called
    assert len(sync_calls) == 0, f"Expected 0 sync calls, got {len(sync_calls)}: {sync_calls}"
    # Async translation should have been invoked
    assert len(async_calls) == 1
    assert "斜拉桥设计" in plan.tiered_queries


# ---------------------------------------------------------------------------
# Test 5: Blocked-Tab Lifecycle and Capability Check (Priority 4)
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_blocked_tab_lifecycle_and_close():
    # Test Douyin provider close releases blocked page
    douyin_browser = MagicMock()
    douyin_browser.release_blocked_page = AsyncMock()
    douyin = DouyinProvider(douyin_browser)
    await douyin.close()
    douyin_browser.release_blocked_page.assert_awaited_once()

    # Test Xiaohongshu provider close releases blocked page
    xhs_browser = MagicMock()
    xhs_browser.release_blocked_page = AsyncMock()
    xhs = XiaohongshuProvider(xhs_browser)
    await xhs.close()
    xhs_browser.release_blocked_page.assert_awaited_once()


@pytest.mark.anyio
async def test_search_service_inspect_capability():
    class LegacyProvider:
        async def search(self, query: str, limit: int, filters: SearchFilters):
            return [
                VideoResult(
                    platform="douyin",
                    platform_video_id="123",
                    url="https://www.douyin.com/video/123",
                    title="Legacy Test",
                )
            ]

        async def close(self):
            pass

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    service = SearchService(sessions, factory=lambda p, m: LegacyProvider())

    with sessions() as db:
        job = SearchJob(
            original_query="test",
            platforms=["douyin"],
            requested_limit=1,
            is_mock=True,
        )
        db.add(job)
        db.flush()
        db.add(SearchPlan(job_id=job.id, queries=["test"]))
        db.commit()
        job_id = job.id

    service.submit(job_id)

    # Wait for completion
    for _ in range(50):
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            if job and job.status in ("completed", "failed"):
                break
        await asyncio.sleep(0.05)

    with sessions() as db:
        job = db.get(SearchJob, job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.processed_count == 1

    engine.dispose()
