import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from backend.api.videos import video_output
from backend.models import JobVideo, SearchJob, Video
from backend.providers.base import ProviderUnavailableError
from backend.providers.mock.provider import MockProvider
from backend.schemas.contracts import SearchFilters, SearchRequest, VideoResult
from backend.services.dedup_service import canonical_url, find_existing, fingerprint
from backend.services.ranking_service import rank
from backend.services.search_service import SearchService, merge_share_url
from backend.services.translator import is_cjk, translate_query


def make_job(sessions, limit=50):
    with sessions() as db:
        job = SearchJob(
            original_query="unbox đồ cute",
            platforms=["douyin", "xiaohongshu"],
            requested_limit=limit,
        )
        db.add(job)
        db.commit()
        return job.id


def test_request_validation():
    assert SearchRequest(query="  đồ   cute ").query == "đồ cute"
    for payload in [
        {"query": "  "},
        {"query": "x", "limit": 201},
        {"query": "x", "platforms": []},
        {"query": "x", "platforms": ["bad"]},
    ]:
        with pytest.raises(ValidationError):
            SearchRequest(**payload)


def test_normalizer_nulls_and_validation():
    result = VideoResult(platform="douyin", url="https://example.invalid/video/1")
    assert result.like_count is None and result.caption is None and result.hashtags == []
    with pytest.raises(ValidationError):
        VideoResult(platform="douyin", url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        VideoResult(platform="douyin", url="https://example.invalid/1", like_count=-1)


def test_canonical_url_keeps_identity():
    assert (
        canonical_url("https://EXAMPLE.com/video/1/?utm_source=test#x")
        == "https://example.com/video/1"
    )
    assert canonical_url("https://example.com/watch?v=1&from=share") != canonical_url(
        "https://example.com/watch?v=2"
    )


def test_fallback_fingerprint():
    first = VideoResult(
        platform="douyin", url="https://example.invalid/a", caption="Hello  world", author_name="A"
    )
    second = first.update_validated(url="https://example.invalid/b", caption="hello world")
    assert fingerprint(first) == fingerprint(second)
    assert fingerprint(first) != fingerprint(first.update_validated(platform="xiaohongshu"))


def test_ranking_separates_engagement_from_relevance():
    niche = VideoResult(
        platform="douyin", url="https://example.invalid/a", title="unbox đồ cute", like_count=1
    )
    viral = niche.update_validated(title="Football", like_count=10000000)
    assert rank(niche, "unbox đồ cute")[0] == 100
    assert rank(viral, "unbox đồ cute")[0] == 0
    assert rank(niche, "unbox đồ cute")[2] > rank(viral, "unbox đồ cute")[2]
    same = niche.update_validated(like_count=10000000)
    assert rank(niche, "unbox đồ cute")[0] == rank(same, "unbox đồ cute")[0]
    assert all(0 <= score <= 100 for score in rank(viral, "unbox đồ cute"))


def test_ranking_with_translated_alternative_queries():
    chinese_video = VideoResult(
        platform="xiaohongshu",
        url="https://example.invalid/xhs1",
        title="沉浸式开箱 少女心可爱文具与萌物",
        caption="今天开箱可爱的文具好物",
        hashtags=["开箱", "可爱好物"],
        like_count=5000,
    )
    assert rank(chinese_video, "ubox đồ cute")[0] == 0
    rel, qual, final = rank(
        chinese_video,
        "ubox đồ cute",
        alternative_queries=["可爱好物开箱", "少女心开箱", "萌物开箱"],
    )
    assert rel >= 50
    assert final >= 40


def test_mock_contract():
    results = asyncio.run(
        MockProvider("douyin", delay=0).search("unbox đồ cute", 20, SearchFilters())
    )
    assert len(results) == 23
    assert len({item.platform_video_id for item in results}) == 20
    assert all(item.is_mock and item.url.host == "example.invalid" for item in results)


def test_search_dedup_crud_and_history(sessions):
    service = SearchService(sessions)
    first = make_job(sessions)
    asyncio.run(service.run(first))
    with sessions() as db:
        job = db.get(SearchJob, first)
        assert job.status == "completed"
        assert job.processed_count == 50
        assert job.duplicate_count == 6
        video = db.scalar(select(Video))
        video.status = "saved"
        video_id = video.id
        db.commit()
    second = make_job(sessions)
    asyncio.run(service.run(second))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Video)) == 50
        assert db.scalar(select(func.count()).select_from(JobVideo)) == 100
        assert db.get(Video, video_id).status == "saved"
        assert db.get(JobVideo, (first, video_id))
        assert db.get(JobVideo, (second, video_id))


def test_dedup_url_and_fallback(sessions):
    result = VideoResult(
        platform="douyin", url="https://example.invalid/a", caption="Caption", author_name="A"
    )
    with sessions() as db:
        video = Video(
            **result.model_dump(exclude={"url"}),
            url=str(result.url),
            canonical_url=canonical_url(str(result.url)),
            fingerprint=fingerprint(result),
            search_query="x",
            search_job_id="test",
        )
        db.add(video)
        db.commit()
        assert (
            find_existing(
                db, result.update_validated(url="https://example.invalid/a?utm_source=x")
            ).id
            == video.id
        )
        assert (
            find_existing(db, result.update_validated(url="https://example.invalid/b")).id
            == video.id
        )


def test_cancel_active_and_queued(sessions):
    async def scenario():
        service = SearchService(sessions, lambda platform, _: MockProvider(platform, delay=1))
        first, second = make_job(sessions), make_job(sessions)
        service.submit(first)
        service.submit(second)
        await asyncio.sleep(0.03)
        await service.cancel(second)
        await service.cancel(first)
        with sessions() as db:
            assert db.get(SearchJob, first).status == "cancelled"
            assert db.get(SearchJob, second).status == "cancelled"
        await service.shutdown()

    asyncio.run(scenario())


def test_provider_failure_is_visible(sessions):
    def unavailable(platform, mock):
        raise ProviderUnavailableError("Real provider chưa có.")

    job_id = make_job(sessions)
    asyncio.run(SearchService(sessions, unavailable).run(job_id))
    with sessions() as db:
        job = db.get(SearchJob, job_id)
        assert job.status == "failed" and job.error_message == "Real provider chưa có."


def test_restart_recovery(sessions):
    job_id = make_job(sessions)
    SearchService(sessions).recover_interrupted()
    with sessions() as db:
        assert db.get(SearchJob, job_id).status == "failed"
        assert "chạy lại" in db.get(SearchJob, job_id).error_message


def test_translator_detects_cjk_and_translates_vietnamese():
    assert is_cjk("可爱好物开箱") is True
    assert is_cjk("ubox đồ cute") is False
    translated = translate_query("ubox đồ cute")
    assert any("开箱" in t for t in translated)
    assert any("可爱" in t or "萌物" in t for t in translated)


def test_merge_share_url():
    nid = "69d3cb2b00000000200399e3"
    canonical = f"https://www.xiaohongshu.com/explore/{nid}"
    old_token = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=OLD111"
    new_token = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=NEW222"
    empty_token = f"https://www.rednote.com/discovery/item/{nid}?xsec_token="
    frag_token = f"https://www.rednote.com/discovery/item/{nid}#xsec_token=FAKE"
    diff_note = "https://www.rednote.com/discovery/item/69d3cb2b00000000200399f9?xsec_token=DIFF"
    diff_note_2 = "https://www.rednote.com/discovery/item/69d3cb2b00000000200399aa?xsec_token=DIFF2"
    short_link = "https://xhslink.com/a/sampleShort"

    # Case 1: token A -> canonical A retains existing token A
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, canonical) == old_token

    # Case 2: token A -> token A mới upgrades to new token
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, new_token) == new_token

    # Case 3: canonical A -> token A upgrades to token
    assert merge_share_url("xiaohongshu", nid, canonical, canonical, new_token) == new_token

    # Case 4: token A -> empty token retains existing token
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, empty_token) == old_token

    # Case 5: token A -> fragment fake token retains existing token
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, frag_token) == old_token

    # Case 6: incoming token for different note ID is rejected and retains existing token
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, diff_note) == old_token

    # Case 7: neither has token -> uses incoming canonical
    assert merge_share_url("xiaohongshu", nid, canonical, None, canonical) == canonical

    # Case 8: None + wrong ID -> rejects wrong incoming note ID and falls back to record canonical
    assert merge_share_url("xiaohongshu", nid, canonical, None, diff_note) == canonical
    assert merge_share_url("xiaohongshu", nid, None, None, diff_note) == canonical

    # Case 9: Cả hai URL sai (both URLs have wrong note ID) -> returns record canonical, never wrong URL
    assert merge_share_url("xiaohongshu", nid, canonical, diff_note, diff_note_2) == canonical

    # Case 10: Record thiếu ID nhưng có canonical A -> derives note ID from canonical and rejects wrong incoming
    assert merge_share_url("xiaohongshu", None, canonical, None, diff_note) == canonical
    assert merge_share_url("xiaohongshu", None, canonical, old_token, canonical) == old_token
    assert merge_share_url("xiaohongshu", None, canonical, old_token, new_token) == new_token

    # Case 11: Unresolved short link (xhslink.com) is preserved when no full token URL exists
    assert merge_share_url("xiaohongshu", nid, canonical, None, short_link) == short_link
    assert merge_share_url("xiaohongshu", nid, canonical, short_link, new_token) == new_token
    assert merge_share_url("xiaohongshu", nid, canonical, old_token, short_link) == old_token

    # Case 12: Neither URL provided at all -> None
    assert merge_share_url("xiaohongshu", nid, canonical, None, None) is None

    # Case 13: other platform (Douyin)
    dy_url1 = "https://www.douyin.com/video/7123456789012345678"
    dy_url2 = "https://www.douyin.com/video/7123456789012345679"
    assert merge_share_url("douyin", "7123456789012345678", None, dy_url1, dy_url2) == dy_url2


def test_persist_results_preserves_token_on_duplicate_search(sessions):
    service = SearchService(sessions)
    nid = "69d3cb2b00000000200399e3"
    canonical = f"https://www.xiaohongshu.com/explore/{nid}"
    token_url = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=PERSISTENT_TOKEN"

    job1 = make_job(sessions)
    vid1 = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid,
        url=canonical,
        share_url=token_url,
        title="Initial XHS with token",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job1, "xiaohongshu", "query1", [vid1], 10))

    # User interacts with the video, setting status to 'favorite'
    with sessions() as db:
        saved = db.scalar(select(Video).where(Video.platform_video_id == nid))
        assert saved is not None
        assert saved.share_url == token_url
        saved.status = "favorite"
        db.commit()

    # Second search discovers the exact same video, but detail crawl only returned canonical (no token)
    job2 = make_job(sessions)
    vid2 = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid,
        url=canonical,
        share_url=canonical,  # canonical fallback, missing token!
        title="Second search title",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job2, "xiaohongshu", "query2", [vid2], 10))

    # Verify that the existing record maintained the token URL, status='favorite', and created JobVideo for both
    with sessions() as db:
        updated = db.scalar(select(Video).where(Video.platform_video_id == nid))
        assert updated is not None
        assert updated.share_url == token_url  # Token was NOT lost!
        assert updated.status == "favorite"  # User status was NOT reset!

        # Check JobVideo links for both jobs
        link1 = db.get(JobVideo, (job1, updated.id))
        link2 = db.get(JobVideo, (job2, updated.id))
        assert link1 is not None
        assert link2 is not None


def test_persist_results_rejects_wrong_note_share_url_and_preserves_status(sessions):
    """Integration test: duplicate search with wrong note ID share_url does not corrupt record."""
    service = SearchService(sessions)
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    token_b = f"https://www.rednote.com/discovery/item/{nid_b}?xsec_token=SYNTHETIC_B"

    # Initial job saves record A with share_url = None
    job1 = make_job(sessions)
    vid_a = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid_a,
        url=canon_a,
        share_url=None,
        title="Note A Title",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job1, "xiaohongshu", "query1", [vid_a], 10))

    # User marks Note A as saved
    with sessions() as db:
        rec_a = db.scalar(select(Video).where(Video.platform_video_id == nid_a))
        assert rec_a is not None
        assert rec_a.share_url is None
        rec_a.status = "saved"
        db.commit()

    # Second search matches Note A's canonical URL, but carries share_url belonging to Note B
    job2 = make_job(sessions)
    vid_corrupted = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid_a,
        url=canon_a,
        share_url=token_b,  # wrong note ID B!
        title="Note A Second Crawl",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job2, "xiaohongshu", "query2", [vid_corrupted], 10))

    with sessions() as db:
        updated = db.scalar(select(Video).where(Video.platform_video_id == nid_a))
        assert updated is not None
        # Must NEVER be assigned Note B's URL!
        assert updated.share_url != token_b
        assert nid_b not in str(updated.share_url)
        assert updated.share_url == canon_a  # Safely fell back to Note A's canonical
        assert updated.status == "saved"  # Status preserved

        # Both JobVideo links exist
        assert db.get(JobVideo, (job1, updated.id)) is not None
        assert db.get(JobVideo, (job2, updated.id)) is not None

        # Verify API response serialization
        out = video_output(updated, db)
        assert str(out.url) == canon_a
        assert out.share_url == canon_a
        assert nid_b not in str(out.share_url)


def test_persist_results_rejects_wrong_note_when_platform_video_id_is_none(sessions):
    """When record's platform_video_id is None, expected ID is derived from canonical_url."""
    service = SearchService(sessions)
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    token_b = f"https://www.rednote.com/discovery/item/{nid_b}?xsec_token=SYNTHETIC_B"

    job1 = make_job(sessions)
    vid_legacy = VideoResult(
        platform="xiaohongshu",
        platform_video_id=None,  # No platform_video_id
        url=canon_a,
        share_url=None,
        title="Legacy Note A without platform_video_id",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job1, "xiaohongshu", "query1", [vid_legacy], 10))

    # Second crawl brings incoming with Note B's share_url
    job2 = make_job(sessions)
    vid_incoming = VideoResult(
        platform="xiaohongshu",
        platform_video_id=None,
        url=canon_a,
        share_url=token_b,
        title="Second crawl incoming",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job2, "xiaohongshu", "query2", [vid_incoming], 10))

    with sessions() as db:
        saved = db.scalar(select(Video).where(Video.canonical_url == canon_a))
        assert saved is not None
        assert saved.share_url != token_b
        assert nid_b not in str(saved.share_url)
        assert saved.share_url == canon_a


def test_persist_results_boundary_rejects_wrong_note_on_initial_insert(sessions):
    """Boundary check: Initial insert with mismatched share_url is sanitized."""
    service = SearchService(sessions)
    nid_a = "69d3cb2b00000000200399e3"
    nid_b = "69d3cb2b00000000200399f9"
    canon_a = f"https://www.xiaohongshu.com/explore/{nid_a}"
    token_b = f"https://www.rednote.com/discovery/item/{nid_b}?xsec_token=SYNTHETIC_B"

    job1 = make_job(sessions)
    vid_initial_mismatch = VideoResult(
        platform="xiaohongshu",
        platform_video_id=nid_a,
        url=canon_a,
        share_url=token_b,
        title="Mismatched initial insert",
        is_mock=False,
    )
    asyncio.run(service.persist_results(job1, "xiaohongshu", "query1", [vid_initial_mismatch], 10))

    with sessions() as db:
        saved = db.scalar(select(Video).where(Video.platform_video_id == nid_a))
        assert saved is not None
        assert saved.share_url != token_b
        assert nid_b not in str(saved.share_url)
        assert saved.share_url == canon_a

