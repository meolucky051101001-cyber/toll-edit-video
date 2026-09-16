import asyncio
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from playwright.async_api import async_playwright
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.api.browser import status as xhs_status
from backend.api.videos import video_output
from backend.core.database import Base
from backend.models import JobVideo, SearchJob, SearchPlan, Video
from backend.providers.base import SelectorChangedError
from backend.providers.douyin.provider import DouyinProvider
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.schemas.expansion import ExpansionOutcome
from backend.services import translator
from backend.services.dedup_service import find_existing
from backend.services.douyin_browser_service import DouyinBrowserService
from backend.services.identity_policy import (
    merge_share_url,
    resolve_xhs_short_link_sync,
)
from backend.services.search_service import SearchService


def test_ai_disabled_zero_ai_calls():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    with sessions() as db:
        job = SearchJob(
            original_query="文具", platforms=["xiaohongshu"], requested_limit=10, is_mock=False
        )
        db.add(job)
        db.flush()
        db.add(SearchPlan(job_id=job.id, use_ai=False, queries=[]))
        db.commit()
        job_id = job.id

    class AIStub:
        def __init__(self):
            self.calls = 0

        async def expand(self, query):
            self.calls += 1
            return ExpansionOutcome(original_query=query, source="gemini", queries=["文具"])

    ai = AIStub()
    service = SearchService(sessions, ai=ai)
    queries = asyncio.run(service.prepare_queries(job_id, "文具"))

    assert ai.calls == 0
    assert queries == ["文具"]
    engine.dispose()


def test_winter_query_translation_preserves_winter():
    with patch.object(translator, "fast_online_translate", return_value=None):
        terms = translator.translate_query("phối đồ mùa đông")[:3]
        assert any("冬" in t for t in terms), f"Expected winter character in top 3 terms, got: {terms}"


def test_expansion_outcome_accepts_dictionary_source():
    outcome = ExpansionOutcome(
        original_query="phối đồ mùa đông",
        source="dictionary",
        queries=["冬季穿搭", "保暖显瘦穿搭"],
    )
    assert outcome.source == "dictionary"


def test_duplicate_refresh_updates_share_url_and_missing_metadata():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    service = SearchService(sessions)

    with sessions() as db:
        j1 = SearchJob(
            original_query="文具", platforms=["xiaohongshu"], requested_limit=10, is_mock=False
        )
        j2 = SearchJob(
            original_query="文具", platforms=["xiaohongshu"], requested_limit=10, is_mock=False
        )
        db.add_all([j1, j2])
        db.commit()
        id1, id2 = j1.id, j2.id

    vid = VideoResult(
        platform="xiaohongshu",
        platform_video_id="a" * 24,
        url="https://www.xiaohongshu.com/explore/" + "a" * 24,
        share_url="https://www.xiaohongshu.com/explore/" + "a" * 24 + "?xsec_token=SYNTHETIC_OLD",
        title="Original",
        caption=None,
    )
    asyncio.run(service.persist_results(id1, "xiaohongshu", "文具", [vid], 10))

    assert vid.share_url is not None
    refreshed = vid.update_validated(
        share_url=vid.share_url.replace("OLD", "NEW"),
        caption="Enriched caption",
        duration_seconds=25.5,
    )
    asyncio.run(service.persist_results(id2, "xiaohongshu", "文具", [refreshed], 10))

    with sessions() as db:
        saved = db.scalar(select(Video).where(Video.platform_video_id == "a" * 24))
        assert saved is not None
        assert "NEW" in str(saved.share_url)
        assert saved.caption == "Enriched caption"
        assert saved.duration_seconds == 25.5
    engine.dispose()


def test_browser_status_get_is_read_only_and_does_not_resume():
    async def scenario():
        class StatusStub:
            async def refresh_status(self):
                return {"state": "verification_required", "diagnostics": {"is_authenticated": True}}

        service = SimpleNamespace(waiters={"douyin_waiting_captcha": asyncio.Event()})
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(browser=StatusStub(), search=service))
        )
        res = await xhs_status(request)
        assert res["state"] == "verification_required"
        assert not service.waiters["douyin_waiting_captcha"].is_set()

    asyncio.run(scenario())


def test_douyin_inspect_guest_and_captcha_not_authenticated():
    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            page = await browser.new_page()
            with tempfile.TemporaryDirectory() as tmp:
                inspector = DouyinBrowserService(Path(tmp), headless=True)
                await page.set_content("<body>Public guest page</body>")
                await inspector.inspect(page)
                assert inspector.diagnostics.get("is_authenticated") is False
                assert inspector.state == "page_available"

                await page.set_content("<body>安全验证 拖动滑块</body>")
                await inspector.inspect(page)
                assert inspector.state == "verification_required"
                assert inspector.diagnostics.get("is_authenticated") is False
            await browser.close()

    asyncio.run(scenario())


def test_xiaohongshu_photo_cards_rejected():
    async def scenario():
        class BrowserStub:
            def __init__(self, page, context, base):
                self.page, self.context, self.base_url = page, context, base
                self.lock = asyncio.Lock()

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                return "page_available"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            async def handler(route):
                body = "<title>文具</title><button>Videos</button>"
                for ident in ["a", "b"]:
                    body += (
                        '<section class="note-item"><a href="/explore/'
                        + ident * 24
                        + '"></a><span class="title">Photo stationery</span><svg class="heart"></svg></section>'
                    )
                await route.fulfill(status=200, body=body, content_type="text/html")

            await context.route("**/*", handler)
            page = await context.new_page()
            await page.goto("https://www.xiaohongshu.com/search_result?keyword=%E6%96%87%E5%85%B7")

            provider = XiaohongshuProvider(
                BrowserStub(page, context, "https://www.xiaohongshu.com")
            )
            with pytest.raises(SelectorChangedError):
                await provider.search("文具", 1, SearchFilters())

            await browser.close()

    asyncio.run(scenario())


def test_xiaohongshu_video_tab_selector():
    async def scenario():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            page = await browser.new_page()
            await page.set_content('<button>Videos</button>')
            video_tab = page.locator(
                "button, div[role='tab'], [role='tab'], .tab, a"
            ).filter(has_text=re.compile(r"^(Videos?|视频)$", re.IGNORECASE)).first
            assert await video_tab.count() == 1
            await browser.close()

    asyncio.run(scenario())


def test_douyin_search_redirect_rejected():
    async def scenario():
        class BrowserStub:
            def __init__(self, page, context, base):
                self.page, self.context, self.base_url = page, context, base
                self.lock = asyncio.Lock()

            async def ensure_open(self):
                return self.page

            async def inspect(self, page):
                return "page_available"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel="chrome")
            context = await browser.new_context()

            async def handler(route):
                if "/search/" in route.request.url:
                    await route.fulfill(status=302, headers={"location": "https://www.douyin.com/"})
                    return
                body = "<title>Home feed</title>"
                for ident in ["1234567890123456789", "2234567890123456789"]:
                    body += (
                        '<li class="card"><a href="/video/'
                        + ident
                        + '"></a><p>Unrelated home recommendation</p></li>'
                    )
                await route.fulfill(status=200, body=body, content_type="text/html")

            await context.route("**/*", handler)
            page = await context.new_page()
            await page.goto("https://www.douyin.com/")

            provider = DouyinProvider(BrowserStub(page, context, "https://www.douyin.com"))
            with pytest.raises(SelectorChangedError) as exc:
                await provider.search("文具", 1, SearchFilters())
            assert "chuyển hướng" in str(exc.value)

            await browser.close()

    asyncio.run(scenario())


def test_f1_unresolved_short_link_cannot_overwrite_verified_token():
    nid_a = "69d3cb2b00000000200399e3"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    verified_token = f"https://www.xiaohongshu.com/explore/{nid_a}?xsec_token=VALID_TOKEN_A"
    incoming_short = "https://xhslink.com/unknown?xsec_token=UNVERIFIED_FAKE"

    # Merge must prefer existing verified token over unresolved short link
    merged = merge_share_url("xiaohongshu", nid_a, canon_a, verified_token, incoming_short)
    assert merged == verified_token
    assert "xhslink.com" not in str(merged)


def test_f1_unresolved_short_link_cannot_overwrite_verified_canonical():
    nid_a = "69d3cb2b00000000200399e3"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    incoming_short = "https://xhslink.com/unknown?xsec_token=UNVERIFIED_FAKE"

    # Merge must prefer existing verified canonical over unresolved short link
    merged = merge_share_url("xiaohongshu", nid_a, canon_a, canon_a, incoming_short)
    assert merged == canon_a
    assert "xhslink.com" not in str(merged)


def test_f1_short_link_resolution_and_validation():
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"

    # Mock resolving short link to note A
    with patch("httpx.Client.head") as mock_head:
        mock_head.return_value = SimpleNamespace(
            url=f"https://www.xiaohongshu.com/explore/{nid_a}?xsec_token=RESOLVED_TOKEN_A"
        )
        resolved = resolve_xhs_short_link_sync("https://xhslink.com/targetA", expected_id=nid_a)
        assert resolved is not None
        assert nid_a in resolved
        assert "RESOLVED_TOKEN_A" in resolved

    # Short link resolving to different note B must be rejected
    with patch("httpx.Client.head") as mock_head:
        mock_head.return_value = SimpleNamespace(
            url=f"https://www.xiaohongshu.com/explore/{nid_b}?xsec_token=TOKEN_B"
        )
        rejected = resolve_xhs_short_link_sync("https://xhslink.com/targetB", expected_id=nid_a)
        assert rejected is None

    # Short link network failure/timeout returns None
    with patch("httpx.Client.head", side_effect=TimeoutError("Request timed out")):
        timeout_res = resolve_xhs_short_link_sync("https://xhslink.com/timeout", expected_id=nid_a)
        assert timeout_res is None


def test_f2_schema_rejects_identity_contradiction():
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"

    # VideoResult with platform_video_id=B and url=canonical A must raise ValidationError
    with pytest.raises(ValidationError) as exc:
        VideoResult(
            platform="xiaohongshu",
            platform_video_id=nid_b,
            url=canon_a,
            is_mock=False,
        )
    assert "Mâu thuẫn danh tính" in str(exc.value)


def test_f2_merge_share_url_returns_none_on_identity_contradiction():
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"

    # Contradiction: platform_video_id=B vs canonical A
    # Must return None and NEVER silently fabricate canonical B
    merged = merge_share_url("xiaohongshu", nid_b, canon_a, None, canon_a)
    assert merged is None


def test_f2_persist_results_quarantines_mismatch_without_row_or_jobvideo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = SearchService(sessions)

    with sessions() as db:
        job = SearchJob(
            original_query="test", platforms=["xiaohongshu"], requested_limit=5, is_mock=False
        )
        db.add(job)
        db.commit()
        job_id = job.id

    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"

    # Construct invalid candidate using model_construct to simulate bypassed validation
    mismatched = VideoResult.model_construct(
        platform="xiaohongshu",
        platform_video_id=nid_b,
        url=canon_a,
        share_url=canon_a,
        title="Contradictory candidate",
        is_mock=False,
    )

    asyncio.run(service.persist_results(job_id, "xiaohongshu", "test", [mismatched], 5))

    with sessions() as db:
        # Neither video A nor video B should exist
        assert db.scalar(select(Video).where(Video.platform_video_id == nid_b)) is None
        assert db.scalar(select(Video).where(Video.canonical_url == canon_a)) is None
        # No JobVideo records created
        assert db.scalar(select(JobVideo).where(JobVideo.job_id == job_id)) is None


def test_f2_missing_platform_video_id_inferred_from_canonical():
    nid_a = "69d3cb2b00000000200399e3"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"

    res = VideoResult(
        platform="xiaohongshu",
        platform_video_id=None,
        url=canon_a,
        title="Missing ID item",
        is_mock=False,
    )
    assert res.platform_video_id == nid_a


def test_f2_dedup_prevents_cross_record_overwrite_with_conflicting_id():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"

    with sessions() as db:
        v_a = Video(
            id="rec-a",
            platform="xiaohongshu",
            platform_video_id=nid_a,
            url=canon_a,
            canonical_url=canon_a,
            title="Video A",
            fingerprint="fp-a",
            search_query="test",
            search_job_id="job-1",
            status="saved",
            final_score=0.9,
        )
        db.add(v_a)
        db.commit()

        # Candidate with ID B should NOT match existing record with ID A
        candidate_b = VideoResult(
            platform="xiaohongshu",
            platform_video_id=nid_b,
            url=f"https://www.xiaohongshu.com/explore/{nid_b}",
            title="Video B",
            is_mock=False,
        )
        matched = find_existing(db, candidate_b)
        assert matched is None


def test_f2_api_readback_and_library_state_preservation():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    service = SearchService(sessions)

    nid_a = "69d3cb2b00000000200399e3"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    token_url_a = f"{canon_a}?xsec_token=TOKEN_FIRST"

    with sessions() as db:
        j1 = SearchJob(
            original_query="q1", platforms=["xiaohongshu"], requested_limit=5, is_mock=False
        )
        db.add(j1)
        db.commit()
        job1_id = j1.id

    vid_initial = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid_a,
        url=canon_a,
        share_url=token_url_a,
        title="Note A Saved",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job1_id, "xiaohongshu", "q1", [vid_initial], 5))

    # User marks video as saved in library
    with sessions() as db:
        rec = db.scalar(select(Video).where(Video.platform_video_id == nid_a))
        assert rec is not None
        rec.status = "saved"
        db.commit()

    # Second crawl with canonical only (no token)
    with sessions() as db:
        j2 = SearchJob(
            original_query="q2", platforms=["xiaohongshu"], requested_limit=5, is_mock=False
        )
        db.add(j2)
        db.commit()
        job2_id = j2.id

    vid_second = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid_a,
        url=canon_a,
        share_url=None,
        title="Note A Second Search",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job2_id, "xiaohongshu", "q2", [vid_second], 5))

    # Verify state, links, and API readback
    with sessions() as db:
        updated = db.scalar(select(Video).where(Video.platform_video_id == nid_a))
        assert updated is not None
        assert updated.status == "saved"  # Status preserved
        assert updated.share_url == token_url_a  # Verified token preserved

        out = video_output(updated, db)
        assert str(out.url) == canon_a
        assert out.share_url == token_url_a
        assert out.status == "saved"
