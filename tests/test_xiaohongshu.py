import asyncio

import pytest
from playwright.async_api import async_playwright
from pydantic import ValidationError

from backend.core.config import settings
from backend.models import SearchJob
from backend.providers.base import SearchProvider, UserActionRequired
from backend.providers.xiaohongshu.parser import (
    classify_page,
    note_url,
    parse_detail,
    resolve_xhs_urls,
)
from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters, VideoResult
from backend.services.browser_service import HOME, BrowserService
from backend.services.search_service import SearchService
from tests.test_pipeline import make_job


@pytest.mark.parametrize(
    "body,state",
    [
        ("IP存在风险 300012", "restricted"),
        ("安全验证 拖动滑块", "verification_required"),
        ("登录后推荐更懂你的笔记", "login_required"),
        ("扫码登录", "login_required"),
        ("公开内容", "page_available"),
    ],
)
def test_gate_classification(body, state):
    assert classify_page(body) == state


def test_dom_detail_requires_video_and_preserves_unknowns():
    url = HOME + "/" + "a" * 24 + "?xsec_token=private-token"
    raw = {
        "has_video": True,
        "title": "Test video",
        "duration": float("nan"),
        "poster": "http://127.0.0.1/private",
    }
    video = parse_detail(raw, url)
    assert (
        video
        and video.like_count is None
        and video.published_at is None
        and video.duration_seconds is None
    )
    assert (
        video.thumbnail_url is None and not video.is_mock and "private-token" not in str(video.url)
    )
    assert parse_detail(raw | {"has_video": False}, url) is None
    assert note_url("https://evil.invalid/explore/" + "a" * 24) is None
    assert note_url("https://www.xiaohongshu.com/user/profile/" + "a" * 24) is None


def test_pause_resume_and_partial_persistence(sessions):
    async def scenario():
        class GateProvider(SearchProvider):
            calls = 0

            async def search(self, query, limit, filters):
                self.calls += 1
                video = VideoResult(
                    platform="xiaohongshu",
                    platform_video_id="a" * 24,
                    url=HOME + "/" + "a" * 24,
                    title="Test",
                    is_mock=False,
                )
                if self.calls == 1:
                    raise UserActionRequired("waiting_for_login", "Manual login required", [video])
                return [video]

        provider = GateProvider()
        service = SearchService(sessions, lambda platform, mock: provider)
        job_id = make_job(sessions, 4)
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            job.platforms = ["xiaohongshu"]
            job.is_mock = False
            db.commit()
        service.submit(job_id)
        for _ in range(100):
            if job_id in service.waiters:
                break
            await asyncio.sleep(0.01)
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            assert job.status == "waiting_for_login" and job.processed_count == 1
        assert service.deadlines[job_id].when() is None
        assert service.resume(job_id)
        task = service.tasks[job_id]
        await task
        with sessions() as db:
            job = db.get(SearchJob, job_id)
            assert (
                job.status == "completed" and job.processed_count == 1 and job.error_message is None
            )
        assert not service.resume(job_id)

    asyncio.run(scenario())


def test_cancel_while_waiting_releases_worker(sessions):
    async def scenario():
        class GateProvider(SearchProvider):
            async def search(self, query, limit, filters):
                raise UserActionRequired("waiting_for_user", "Restriction")

        service = SearchService(sessions, lambda platform, mock: GateProvider())
        job_id = make_job(sessions)
        service.submit(job_id)
        for _ in range(100):
            if job_id in service.waiters:
                break
            await asyncio.sleep(0.01)
        await service.cancel(job_id)
        assert job_id not in service.waiters and not service.lock.locked()
        with sessions() as db:
            assert db.get(SearchJob, job_id).status == "cancelled"

    asyncio.run(scenario())


def test_real_mode_limits_and_resume_api(client):
    assert client.post("/api/search", json={"query": "x", "mode": "xiaohongshu"}).status_code == 422
    assert (
        client.post(
            "/api/search",
            json={"query": "x", "mode": "xiaohongshu", "platforms": ["xiaohongshu"], "limit": 150},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/search",
            json={
                "query": "x",
                "mode": "xiaohongshu",
                "platforms": ["xiaohongshu"],
                "selected_queries": ["a", "b", "c", "d"],
            },
        ).status_code
        == 422
    )
    assert client.post("/api/search/jobs/not-found/resume").status_code == 404


def test_provider_browser_dom_and_restriction_without_network(tmp_path):
    async def scenario():
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                str(tmp_path / "profile"),
                headless=True,
                channel=settings.xhs_browser_channel,
            )
            requests = []
            restricted = False

            async def route_handler(route):
                requests.append(route.request.url)
                if restricted:
                    html = "<title>安全限制</title><body>IP存在风险 300012</body>"
                elif "/explore/" in route.request.url:
                    html = '<meta property="og:title" content="Synthetic DOM video"><video style="width:400px;height:250px"></video>'
                elif "keyword=" in route.request.url:
                    html = (
                        '<a href="https://www.xiaohongshu.com/explore/'
                        + "a" * 24
                        + '">Synthetic result</a>'
                    )
                else:
                    html = """<div class="search-box"><input id="search-input">
                    <button class="search-icon"
                    onclick="location.href='/search_result?keyword='+encodeURIComponent(document.querySelector('#search-input').value)">
                    Search</button></div>"""
                await route.fulfill(status=200, body=html, content_type="text/html")

            await context.route("**/*", route_handler)
            page = await context.new_page()
            await page.goto(HOME)
            browser = BrowserService(tmp_path / "unused", headless=True)
            browser.context = context
            browser.page = page
            results = await XiaohongshuProvider(browser).search("文具", 1, SearchFilters())
            assert (
                len(results) == 1
                and results[0].title == "Synthetic DOM video"
                and not results[0].is_mock
            )
            assert all("xiaohongshu.com" in url for url in requests)
            submitted = sum("keyword=" in url for url in requests)
            repeated = await XiaohongshuProvider(browser).search("文具", 1, SearchFilters())
            assert len(repeated) == 1
            assert sum("keyword=" in url for url in requests) == submitted
            restricted = True
            await page.goto(HOME)
            count = len(requests)
            with pytest.raises(UserActionRequired) as caught:
                await XiaohongshuProvider(browser).search("文具", 1, SearchFilters())
            assert caught.value.state == "waiting_for_user" and len(requests) == count
            await context.close()

    asyncio.run(scenario())


def test_browser_status_does_not_report_its_own_lock_as_busy(tmp_path):
    async def scenario():
        browser = BrowserService(tmp_path / "profile", headless=True)
        assert (await browser.refresh_status())["busy"] is False
        async with browser.lock:
            assert (await browser.refresh_status())["busy"] is True
        assert (await browser.refresh_status())["open"] is False

    asyncio.run(scenario())


def test_note_url_supports_rednote_and_all_routes():
    test_id = "69d3cb2b00000000200399e3"
    canonical = f"https://www.xiaohongshu.com/explore/{test_id}"
    
    # Rednote routes
    assert note_url(f"https://www.rednote.com/discovery/item/{test_id}?xsec_token=xyz") == canonical
    assert note_url(f"https://www.rednote.com/search_result/{test_id}") == canonical
    assert note_url(f"https://www.rednote.com/explore/{test_id}") == canonical
    assert note_url(f"https://rednote.com/explore/{test_id}") == canonical

    # Xiaohongshu routes
    assert note_url(f"https://www.xiaohongshu.com/discovery/item/{test_id}?xsec_token=xyz") == canonical
    assert note_url(f"https://www.xiaohongshu.com/search_result/{test_id}") == canonical
    assert note_url(f"https://www.xiaohongshu.com/explore/{test_id}") == canonical

    # Invalid / untrusted hosts
    assert note_url(f"https://evil.com/explore/{test_id}") is None
    assert note_url(f"https://www.rednote.com/invalid_route/{test_id}") is None


def test_parse_detail_extracts_author_engagement_and_hashtags():
    test_id = "69d3cb2b00000000200399e3"
    playback_url = f"https://www.rednote.com/discovery/item/{test_id}?xsec_token=secret_token"
    raw = {
        "has_video": True,
        "title": "Emoji Pencil Unboxing",
        "description": "Lovely pencil #cute #unboxing #stationery",
        "duration": 15.5,
        "poster": "//fe-s10.rednotecdn.com/poster.png",
        "author_name": "WangWang",
        "author_url": "https://www.rednote.com/user/profile/5a7ac3df4eacab67c885316e",
        "like_text": "34.5K",
        "collect_text": "7952",
        "comment_text": "182",
    }
    video = parse_detail(raw, playback_url)
    assert video is not None
    assert video.platform == "xiaohongshu"
    assert video.platform_video_id == test_id
    assert str(video.url) == f"https://www.xiaohongshu.com/explore/{test_id}"
    assert video.share_url == playback_url
    assert video.title == "Emoji Pencil Unboxing"
    assert video.caption == "Lovely pencil #cute #unboxing #stationery"
    assert video.thumbnail_url == "https://fe-s10.rednotecdn.com/poster.png"
    assert video.author_name == "WangWang"
    assert video.author_id == "5a7ac3df4eacab67c885316e"
    assert video.author_url == "https://www.rednote.com/user/profile/5a7ac3df4eacab67c885316e"
    assert video.duration_seconds == 15.5
    assert video.like_count == 34500
    assert video.favorite_count == 7952
    assert video.comment_count == 182
    assert video.hashtags == ["cute", "unboxing", "stationery"]
    assert not video.is_mock


def test_classify_page_url_error_codes():
    # URL with error_code=300012
    assert classify_page("some text", url="https://www.xiaohongshu.com/website-login/error?error_code=300012") == "restricted"
    # URL with website-login path
    assert classify_page("some text", url="https://www.xiaohongshu.com/website-login/error?error_code=123") == "login_required"
    # English terms
    assert classify_page("IP at risk. Switch network.", url="https://www.rednote.com/") == "restricted"
    assert classify_page("Scan QR code to log in", url="https://www.rednote.com/") == "login_required"


def test_resolve_xhs_urls_branches():
    nid = "69d3cb2b00000000200399e3"
    token_detail = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=detail_token"
    token_cand = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=cand_token"
    canonical = f"https://www.xiaohongshu.com/explore/{nid}"

    # Branch 1: detail_url has token -> canonical is explore, share_url preserves detail token
    c1, s1, id1 = resolve_xhs_urls(token_detail, canonical)
    assert c1 == canonical
    assert s1 == token_detail
    assert id1 == nid

    # Branch 2: detail_url has no token, candidate_link has token -> share_url preserves candidate token
    c2, s2, id2 = resolve_xhs_urls(canonical, token_cand)
    assert c2 == canonical
    assert s2 == token_cand
    assert id2 == nid

    # Branch 3: neither has token -> share_url falls back to canonical
    c3, s3, id3 = resolve_xhs_urls(canonical, canonical)
    assert c3 == canonical
    assert s3 == canonical
    assert id3 == nid

    # Branch 4: untrusted or insecure URL with token is ignored; fallback to trusted canonical
    untrusted = f"https://evil.example/discovery/item/{nid}?xsec_token=evil_token"
    c4, s4, id4 = resolve_xhs_urls(untrusted, canonical)
    assert c4 == canonical
    assert s4 == canonical
    assert id4 == nid

    # Branch 5: both completely invalid -> ValueError
    with pytest.raises(ValueError, match="Cannot resolve canonical Xiaohongshu URL"):
        resolve_xhs_urls("https://evil.example/invalid", "ftp://evil.example/invalid")

    # Branch 6: token belongs to a different note ID -> token discarded, returns canonical of detail note
    nid_b = "69d3cb2b00000000200399f9"
    detail_note_b = f"https://www.xiaohongshu.com/explore/{nid_b}"
    cand_token_a = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=token_for_a"
    c6, s6, id6 = resolve_xhs_urls(detail_note_b, cand_token_a)
    assert c6 == detail_note_b
    assert s6 == detail_note_b  # token for note A is NOT used on note B
    assert id6 == nid_b

    # Branch 7: token URL has unrelated non-note path -> token discarded
    unrelated_path_token = "https://www.xiaohongshu.com/unrelated?xsec_token=some_token"
    c7, s7, id7 = resolve_xhs_urls(unrelated_path_token, canonical)
    assert c7 == canonical
    assert s7 == canonical
    assert id7 == nid

    # Branch 8: token URL has credentials -> rejected, fallback to canonical
    creds_token = f"https://user:pass@www.xiaohongshu.com/explore/{nid}?xsec_token=token123"
    c8, s8, id8 = resolve_xhs_urls(creds_token, canonical)
    assert c8 == canonical
    assert s8 == canonical
    assert id8 == nid

    # Branch 9: token URL has custom port -> rejected, fallback to canonical
    port_token = f"https://www.xiaohongshu.com:8443/explore/{nid}?xsec_token=token123"
    c9, s9, id9 = resolve_xhs_urls(port_token, canonical)
    assert c9 == canonical
    assert s9 == canonical
    assert id9 == nid

    # Branch 10: token URL has empty xsec_token -> rejected, fallback to canonical
    empty_token_url = f"https://www.rednote.com/discovery/item/{nid}?xsec_token="
    c10, s10, id10 = resolve_xhs_urls(empty_token_url, canonical)
    assert c10 == canonical
    assert s10 == canonical
    assert id10 == nid

    # Branch 11: token in fragment/hash rather than query -> rejected, fallback to canonical
    frag_token_url = f"https://www.rednote.com/discovery/item/{nid}#xsec_token=FAKE_HASH"
    c11, s11, id11 = resolve_xhs_urls(frag_token_url, canonical)
    assert c11 == canonical
    assert s11 == canonical
    assert id11 == nid

    # Branch 12: unrelated parameter name containing substring -> rejected, fallback to canonical
    similar_param_url = f"https://www.rednote.com/discovery/item/{nid}?not_xsec_token=FAKE"
    c12, s12, id12 = resolve_xhs_urls(similar_param_url, canonical)
    assert c12 == canonical
    assert s12 == canonical
    assert id12 == nid


def test_extract_and_has_xhs_token():
    from backend.providers.xiaohongshu.parser import extract_xhs_token, has_xhs_token

    # Valid token
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=VALID123") == "VALID123"
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=VALID123") is True

    # Empty token value
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=") is None
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=") is False

    # Whitespace token value
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=%20%20") is None
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=%20%20") is False

    # Fragment / hash token
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123#xsec_token=FAKE") is None
    assert has_xhs_token("https://www.rednote.com/discovery/item/123#xsec_token=FAKE") is False

    # Substring in other parameter names
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?not_xsec_token=FAKE") is None
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?not_xsec_token=FAKE") is False
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token_suffix=FAKE") is None
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token_suffix=FAKE") is False

    # Duplicate parameter: last non-empty wins
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=FIRST&xsec_token=SECOND") == "SECOND"
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=FIRST&xsec_token=") == "FIRST"

    # URL encoded values
    assert extract_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=hello%2Bworld%3D") == "hello+world="
    assert has_xhs_token("https://www.rednote.com/discovery/item/123?xsec_token=hello%2Bworld%3D") is True

    # None and invalid inputs
    assert extract_xhs_token(None) is None
    assert has_xhs_token(None) is False
    assert extract_xhs_token("") is None
    assert has_xhs_token("") is False


def test_video_result_frozen_and_platform_url_policy():
    from backend.schemas.contracts import update_video_result

    nid = "69d3cb2b00000000200399e3"
    canonical_xhs = f"https://www.xiaohongshu.com/explore/{nid}"
    token_xhs = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=CB123"

    vr = VideoResult(
        platform="xiaohongshu",
        url=canonical_xhs,
        share_url=token_xhs,
        title="Test XHS",
    )
    assert vr.share_url == token_xhs
    assert str(vr.url) == canonical_xhs
    initial_dump = vr.model_dump(mode="json")

    # Immutability test: direct assignment to any field must fail with frozen_instance error
    with pytest.raises(ValidationError) as exc_info:
        vr.share_url = f"ftp://www.xiaohongshu.com/explore/{nid}"  # type: ignore[misc]
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        vr.url = "https://evil.example/video"  # type: ignore[misc]
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())

    with pytest.raises(ValidationError) as exc_info:
        vr.platform = "douyin"  # type: ignore[misc]
    assert any(err["type"] == "frozen_instance" for err in exc_info.value.errors())

    # Critical requirement: model state and model_dump remain completely intact and uncorrupted
    assert vr.share_url == token_xhs
    assert str(vr.url) == canonical_xhs
    assert vr.platform == "xiaohongshu"
    assert vr.model_dump(mode="json") == initial_dump

    # Update helper test: update_validated with valid changes produces a new immutable instance
    new_token = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=NEW999"
    vr_updated = vr.update_validated(share_url=new_token)
    assert vr_updated.share_url == new_token
    assert str(vr_updated.url) == canonical_xhs
    # Original remains untouched
    assert vr.share_url == token_xhs
    assert vr.model_dump(mode="json") == initial_dump

    # Top-level helper update_video_result works identically
    vr_helper = update_video_result(vr, share_url=new_token)
    assert vr_helper.share_url == new_token
    assert vr.share_url == token_xhs

    # Update helper test: update_validated with invalid URL must raise ValidationError and keep original intact
    with pytest.raises(ValidationError):
        vr.update_validated(share_url=f"ftp://www.xiaohongshu.com/explore/{nid}")
    assert vr.share_url == token_xhs

    with pytest.raises(ValidationError):
        vr.update_validated(url="https://evil.example/video")
    assert str(vr.url) == canonical_xhs

    with pytest.raises(ValidationError):
        vr.update_validated(share_url=f"https://user:pass@www.xiaohongshu.com/explore/{nid}")
    assert vr.share_url == token_xhs

    with pytest.raises(ValidationError):
        vr.update_validated(share_url=f"https://www.xiaohongshu.com:8443/explore/{nid}")
    assert vr.share_url == token_xhs

    with pytest.raises(ValidationError):
        vr.update_validated(share_url=f"https://example.invalid/xiaohongshu/{nid}")
    assert vr.share_url == token_xhs

    # model_copy(update=...) test: overridden to strictly validate updates
    vr_copied = vr.model_copy(update={"share_url": new_token})
    assert vr_copied.share_url == new_token
    assert vr.share_url == token_xhs

    with pytest.raises(ValidationError):
        vr.model_copy(update={"share_url": f"ftp://www.xiaohongshu.com/explore/{nid}"})
    assert vr.share_url == token_xhs

    with pytest.raises(ValidationError):
        vr.model_copy(update={"url": "https://evil.example/video"})
    assert str(vr.url) == canonical_xhs

    # Construction validation: primary url on untrusted host must fail
    with pytest.raises(ValidationError):
        VideoResult(
            platform="xiaohongshu",
            url="https://evil.example/video",
            title="Evil",
        )

    # Construction validation: cross-platform primary URL must fail
    with pytest.raises(ValidationError):
        VideoResult(
            platform="xiaohongshu",
            url="https://www.douyin.com/video/7123456789012345678",
            title="Cross",
        )

    # Construction validation: non-mock using example.invalid for url must fail
    with pytest.raises(ValidationError):
        VideoResult(
            platform="xiaohongshu",
            url=f"https://example.invalid/xiaohongshu/{nid}",
            title="Non-mock invalid",
            is_mock=False,
        )

    # Construction validation: mock must use example.invalid
    vr_mock = VideoResult(
        platform="xiaohongshu",
        url=f"https://example.invalid/xiaohongshu/{nid}",
        share_url=f"https://example.invalid/xiaohongshu/{nid}",
        title="Mock",
        is_mock=True,
    )
    assert vr_mock.is_mock is True
    assert "example.invalid" in str(vr_mock.url)

    # Mock with untrusted host must fail
    with pytest.raises(ValidationError):
        VideoResult(
            platform="xiaohongshu",
            url="https://evil.example/video",
            title="Mock with evil url",
            is_mock=True,
        )



