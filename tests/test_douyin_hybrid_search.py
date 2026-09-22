from datetime import datetime, timezone

import pytest

from backend.models.search_plan import SearchPlan
from backend.models.video import Video
from backend.providers.douyin.parser import (
    extract_douyin_carousel_images,
    parse_aweme_item,
    parse_search_response_json,
    video_url,
)
from backend.schemas.contracts import SearchFilters
from backend.services.identity_policy import extract_douyin_id, validate_result_identity
from backend.services.virality_service import compute_engagement_velocity


def test_douyin_note_url_canonicalization():
    """Verify that both /video/ and /note/ Douyin URLs are canonicalized correctly."""
    note_raw = "https://www.douyin.com/note/7412345678901234567?extra_param=abc"
    assert video_url(note_raw) == "https://www.douyin.com/note/7412345678901234567"
    assert extract_douyin_id(note_raw) == "7412345678901234567"
    is_valid, resolved_id, err = validate_result_identity("douyin", "7412345678901234567", note_raw)
    assert is_valid is True
    assert resolved_id == "7412345678901234567"

    vid_raw = "https://www.douyin.com/video/7412345678901234567?extra_param=abc"
    assert video_url(vid_raw) == "https://www.douyin.com/video/7412345678901234567"
    assert extract_douyin_id(vid_raw) == "7412345678901234567"
    is_valid, resolved_id, err = validate_result_identity("douyin", "7412345678901234567", vid_raw)
    assert is_valid is True
    assert resolved_id == "7412345678901234567"


def test_parse_aweme_item_video():
    """Verify parse_aweme_item extracts full metrics from a video item."""
    raw_item = {
        "aweme_id": "7412345678901234567",
        "desc": "测评最火的手帐胶带！超好看 #手帐 #文具测评",
        "create_time": 1726000000,
        "author": {
            "sec_uid": "MS4wLjABAAAA_sec_user",
            "nickname": "手帐测评家",
        },
        "statistics": {
            "digg_count": 85000,
            "comment_count": 1200,
            "share_count": 450,
            "collect_count": 9800,
            "play_count": 500000,
        },
        "video": {
            "duration": 58000,  # 58 seconds in ms
            "cover": {
                "url_list": ["https://p3-pc.douyinpic.com/cover1.jpg"],
            },
        },
    }

    parsed = parse_aweme_item(raw_item)
    assert parsed is not None
    assert parsed.platform == "douyin"
    assert parsed.platform_video_id == "7412345678901234567"
    assert str(parsed.url) == "https://www.douyin.com/video/7412345678901234567"
    assert parsed.title == "测评最火的手帐胶带！超好看 #手帐 #文具测评"
    assert parsed.author_name == "手帐测评家"
    assert parsed.author_id == "MS4wLjABAAAA_sec_user"
    assert parsed.like_count == 85000
    assert parsed.comment_count == 1200
    assert parsed.share_count == 450
    assert parsed.favorite_count == 9800
    assert parsed.view_count == 500000
    assert parsed.duration_seconds == 58.0
    assert parsed.thumbnail_url == "https://p3-pc.douyinpic.com/cover1.jpg"
    assert parsed.media_type == "video"
    assert parsed.images == []
    assert "手帐" in parsed.hashtags
    assert "文具测评" in parsed.hashtags


def test_parse_aweme_item_image_text_note():
    """Verify parse_aweme_item correctly identifies and extracts 图文 note carousels."""
    raw_item = {
        "aweme_id": "7423456789012345678",
        "desc": "这几款胶带太惊艳了，手帐党闭眼入！",
        "create_time": 1726000000,
        "media_type": 2,  # Douyin image-text indicator
        "author": {
            "sec_uid": "MS4wLjABAAAA_sec_note",
            "nickname": "手帐插画师",
        },
        "statistics": {
            "digg_count": 12000,
            "comment_count": 340,
            "share_count": 110,
            "collect_count": 4200,
        },
        "images": [
            {"url_list": ["https://p3-pc.douyinpic.com/img1.jpg"]},
            {"url_list": ["https://p3-pc.douyinpic.com/img2.jpg"]},
            {"url_list": ["https://p3-pc.douyinpic.com/img3.jpg"]},
        ],
    }

    parsed = parse_aweme_item(raw_item)
    assert parsed is not None
    assert parsed.platform == "douyin"
    assert parsed.platform_video_id == "7423456789012345678"
    assert str(parsed.url) == "https://www.douyin.com/note/7423456789012345678"
    assert parsed.media_type == "image_text"
    assert len(parsed.images) == 3
    assert parsed.images[0] == "https://p3-pc.douyinpic.com/img1.jpg"
    assert parsed.thumbnail_url == "https://p3-pc.douyinpic.com/img1.jpg"


def test_parse_search_response_json_with_filters():
    """Verify parse_search_response_json parses multi-item search response and applies filters."""
    mock_response = {
        "status_code": 0,
        "data": [
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "7410000000000000001",
                    "desc": "视频 1 - 高赞",
                    "statistics": {"digg_count": 50000},
                    "video": {"duration": 30000},
                },
            },
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "7410000000000000002",
                    "desc": "视频 2 - 低赞",
                    "statistics": {"digg_count": 50},
                    "video": {"duration": 20000},
                },
            },
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "7410000000000000003",
                    "desc": "图文 3 - 高赞",
                    "statistics": {"digg_count": 30000},
                    "images": [{"url_list": ["https://p3-pc.douyinpic.com/note.jpg"]}],
                },
            },
        ],
    }

    # Filter: minimum likes 1000
    filters_min_likes = SearchFilters(minimum_likes=1000)
    results = parse_search_response_json(mock_response, limit=10, filters=filters_min_likes)
    assert len(results) == 2
    assert {r.platform_video_id for r in results} == {
        "7410000000000000001",
        "7410000000000000003",
    }

    # Filter: content_type = "video"
    filters_video_only = SearchFilters(content_type="video")
    results_vid = parse_search_response_json(mock_response, limit=10, filters=filters_video_only)
    assert len(results_vid) == 2
    assert all(r.media_type == "video" for r in results_vid)

    # Filter: content_type = "image_text"
    filters_note_only = SearchFilters(content_type="image_text")
    results_note = parse_search_response_json(mock_response, limit=10, filters=filters_note_only)
    assert len(results_note) == 1
    assert results_note[0].platform_video_id == "7410000000000000003"
    assert results_note[0].media_type == "image_text"


def test_compute_engagement_velocity():
    """Verify compute_virality computes weighted hourly velocity with time-decay."""
    from backend.services.virality_service import compute_virality

    video = Video(
        id="test-vid-1",
        platform="douyin",
        platform_video_id="7410000000000000001",
        url="https://www.douyin.com/video/7410000000000000001",
        canonical_url="https://www.douyin.com/video/7410000000000000001",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job-1",
        like_count=10000,
        comment_count=1000,
        share_count=200,
        favorite_count=500,
        published_at=datetime.now(timezone.utc),
    )

    metrics = compute_virality(video)
    # Likes(10000*1) + Comments(1000*1.5=1500) + Shares(200*2.5=500) + Favorites(500*1.2=600) = 12600
    assert metrics.hourly_velocity > 10000.0
    velocity_score = compute_engagement_velocity(video)
    assert 0.0 <= velocity_score <= 100.0


def test_douyin_search_api_with_filters(client, sessions):
    """Verify /api/search accepts sort_by, publish_time, and content_type filters."""
    payload = {
        "query": "手帐排版",
        "mode": "douyin",
        "platforms": ["douyin"],
        "limit": 10,
        "sort_by": "most_liked",
        "publish_time": "week",
        "content_type": "image_text",
    }
    resp = client.post("/api/search", json=payload)
    assert resp.status_code == 202
    data = resp.json()
    job_id = data["id"]

    # Verify search_plan stores filters
    with sessions() as db:
        plan = db.query(SearchPlan).filter(SearchPlan.job_id == job_id).first()
        assert plan is not None
        assert plan.filters is not None
        assert plan.filters.get("sort_by") == "most_liked"
        assert plan.filters.get("publish_time") == "week"
        assert plan.filters.get("content_type") == "image_text"


def test_extract_douyin_carousel_images_all_schemas():
    """Verify extract_douyin_carousel_images extracts URLs across all 5 Douyin schemas."""
    # Schema 1: image_post_info.images[*].display_image
    s1 = {
        "image_post_info": {
            "images": [
                {"display_image": {"url_list": ["https://p3.douyinpic.com/disp1.jpg"]}},
                {"display_image": {"url_list": ["https://p3.douyinpic.com/disp2.jpg"]}},
            ]
        }
    }
    assert extract_douyin_carousel_images(s1) == [
        "https://p3.douyinpic.com/disp1.jpg",
        "https://p3.douyinpic.com/disp2.jpg",
    ]

    # Schema 2: image_post_info.images[*].origin_image
    s2 = {
        "image_post_info": {
            "images": [
                {"origin_image": {"url_list": ["https://p3.douyinpic.com/orig1.jpg"]}},
            ]
        }
    }
    assert extract_douyin_carousel_images(s2) == ["https://p3.douyinpic.com/orig1.jpg"]

    # Schema 3: image_post_info.images[*].download_addr
    s3 = {
        "image_post_info": {
            "images": [
                {"download_addr": {"url_list": ["https://p3.douyinpic.com/dl1.jpg"]}},
            ]
        }
    }
    assert extract_douyin_carousel_images(s3) == ["https://p3.douyinpic.com/dl1.jpg"]

    # Schema 4: images[*].download_url_list
    s4 = {
        "images": [
            {"download_url_list": ["https://p3.douyinpic.com/imgdl1.jpg"]},
            {"download_url_list": ["https://p3.douyinpic.com/imgdl2.jpg"]},
        ]
    }
    assert extract_douyin_carousel_images(s4) == [
        "https://p3.douyinpic.com/imgdl1.jpg",
        "https://p3.douyinpic.com/imgdl2.jpg",
    ]

    # Schema 5: image_list[*].url_list
    s5 = {
        "image_list": [
            {"url_list": ["https://p3.douyinpic.com/list1.jpg"]},
        ]
    }
    assert extract_douyin_carousel_images(s5) == ["https://p3.douyinpic.com/list1.jpg"]


@pytest.mark.anyio
async def test_douyin_provider_listener_attached_before_navigation():
    """Verify DouyinSearchProvider attaches page.on('response') BEFORE navigating with page.goto()."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from backend.providers.douyin.provider import DouyinProvider

    events_order: list[str] = []
    response_handlers = []

    mock_page = MagicMock()
    mock_page.url = "https://www.douyin.com/"
    mock_page.is_closed.return_value = False

    def fake_on(event_name, handler):
        events_order.append(f"on_{event_name}")
        if event_name == "response":
            response_handlers.append(handler)

    def fake_remove_listener(event_name, handler):
        events_order.append(f"remove_{event_name}")
        if handler in response_handlers:
            response_handlers.remove(handler)

    mock_page.on = MagicMock(side_effect=fake_on)
    mock_page.remove_listener = MagicMock(side_effect=fake_remove_listener)

    async def fake_goto(url, **kwargs):
        events_order.append(f"goto_{url}")
        mock_page.url = url
        # Simulate network response fired right during page load
        mock_resp = MagicMock()
        mock_resp.url = "https://www.douyin.com/aweme/v1/web/search/item/?keyword=decor"
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "status_code": 0,
                "data": [
                    {
                        "type": 1,
                        "aweme_info": {
                            "aweme_id": "7410000000000000001",
                            "desc": "decor bàn học",
                            "statistics": {"digg_count": 50000},
                            "video": {"duration": 30000},
                        },
                    }
                ],
            }
        )
        for h in list(response_handlers):
            await h(mock_resp)

    mock_page.goto = AsyncMock(side_effect=fake_goto)
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.locator = MagicMock()

    mock_browser = MagicMock()
    mock_browser.lock = asyncio.Lock()
    mock_browser.base_url = "https://www.douyin.com"
    mock_browser.inspect = AsyncMock(return_value="page_available")
    mock_browser.ensure_open = AsyncMock(return_value=mock_page)
    mock_browser.ensure_headless_session = AsyncMock()
    mock_browser.release_blocked_page = AsyncMock()

    provider = DouyinProvider(browser=mock_browser)

    results = await provider.search("decor bàn học", limit=1, filters=SearchFilters())

    # Ensure page.on('response') happened BEFORE goto
    assert "on_response" in events_order
    assert any(e.startswith("goto_") for e in events_order)
    first_on_idx = events_order.index("on_response")
    first_goto_idx = next(i for i, e in enumerate(events_order) if e.startswith("goto_"))
    assert first_on_idx < first_goto_idx, f"Listener must be attached before goto: {events_order}"
    assert len(results) == 1
    assert results[0].platform_video_id == "7410000000000000001"


@pytest.mark.anyio
async def test_douyin_provider_consecutive_searches_always_navigate():
    """Verify that searching term B after term A always triggers page.goto even if already on /search."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from backend.providers.douyin.provider import DouyinProvider

    visited_urls: list[str] = []
    response_handlers = []

    mock_page = MagicMock()
    mock_page.url = "https://www.douyin.com/"
    mock_page.is_closed.return_value = False

    def fake_on(event_name, handler):
        if event_name == "response":
            response_handlers.append(handler)

    mock_page.on = MagicMock(side_effect=fake_on)
    mock_page.remove_listener = MagicMock()

    async def fake_goto(url, **kwargs):
        visited_urls.append(url)
        mock_page.url = url
        # Fire network response
        mock_resp = MagicMock()
        mock_resp.url = "https://www.douyin.com/aweme/v1/web/search/item/?keyword=test"
        mock_resp.status = 200
        mock_resp.json = AsyncMock(
            return_value={
                "status_code": 0,
                "data": [
                    {
                        "type": 1,
                        "aweme_info": {
                            "aweme_id": f"741{len(visited_urls):016d}",
                            "desc": f"search result for {url}",
                            "statistics": {"digg_count": 1000},
                            "video": {"duration": 15000},
                        },
                    }
                ],
            }
        )
        for h in list(response_handlers):
            await h(mock_resp)

    mock_page.goto = AsyncMock(side_effect=fake_goto)
    mock_page.wait_for_timeout = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.locator = MagicMock()

    mock_browser = MagicMock()
    mock_browser.lock = asyncio.Lock()
    mock_browser.base_url = "https://www.douyin.com"
    mock_browser.inspect = AsyncMock(return_value="page_available")
    mock_browser.ensure_open = AsyncMock(return_value=mock_page)
    mock_browser.ensure_headless_session = AsyncMock()
    mock_browser.release_blocked_page = AsyncMock()

    provider = DouyinProvider(browser=mock_browser)

    # Search 1: decor bàn học
    res1 = await provider.search("decor bàn học", limit=1, filters=SearchFilters())
    assert len(visited_urls) >= 1
    assert "decor" in visited_urls[0]
    assert len(res1) == 1

    # Search 2: bjd makeup (page.url is already on /search/decor...)
    mock_page.url = visited_urls[0]
    res2 = await provider.search("bjd makeup", limit=1, filters=SearchFilters())

    assert len(visited_urls) >= 2
    assert "bjd" in visited_urls[1], f"Consecutive search must navigate to new query URL: {visited_urls}"
    assert len(res2) == 1


@pytest.mark.anyio
async def test_search_service_defense_in_depth_post_filtering(sessions):
    """Verify SearchService.persist_results rejects candidates violating content_type and publish_time."""
    from datetime import datetime, timedelta, timezone

    from backend.models import SearchJob, SearchPlan
    from backend.schemas.contracts import VideoResult
    from backend.services.search_service import SearchService

    job_id = "job-defense-filter-1"
    with sessions() as db:
        job = SearchJob(
            id=job_id,
            original_query="decor bàn học",
            platforms=["douyin"],
            requested_limit=10,
            status="searching",
        )
        plan = SearchPlan(
            job_id=job_id,
            queries=["decor bàn học"],
            filters={
                "sort_by": "relevance",
                "publish_time": "day",
                "content_type": "video",
            },
        )
        db.add(job)
        db.add(plan)
        db.commit()

    service = SearchService(sessions=sessions)

    # Candidate 1: valid video within 2 hours
    valid_vid = VideoResult(
        platform="douyin",
        platform_video_id="7410000000000000091",
        url="https://www.douyin.com/video/7410000000000000091",
        title="decor bàn học siêu đẹp",
        media_type="video",
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
        like_count=5000,
    )

    # Candidate 2: invalid media_type (image_text when video requested)
    invalid_type = VideoResult(
        platform="douyin",
        platform_video_id="7410000000000000092",
        url="https://www.douyin.com/note/7410000000000000092",
        title="decor bàn học ảnh",
        media_type="image_text",
        published_at=datetime.now(timezone.utc) - timedelta(hours=2),
        like_count=5000,
    )

    # Candidate 3: invalid publish_time (30 days ago when 'day' requested)
    invalid_time = VideoResult(
        platform="douyin",
        platform_video_id="7410000000000000093",
        url="https://www.douyin.com/video/7410000000000000093",
        title="decor bàn học cũ",
        media_type="video",
        published_at=datetime.now(timezone.utc) - timedelta(days=30),
        like_count=5000,
    )

    await service.persist_results(
        job_id=job_id,
        platform="douyin",
        query="decor bàn học",
        results=[valid_vid, invalid_type, invalid_time],
        provider_limit=10,
    )

    with sessions() as db:
        updated_job = db.get(SearchJob, job_id)
        assert updated_job is not None
        assert updated_job.found_count == 3
        # Both invalid candidates must have been rejected
        assert updated_job.rejected_count == 2
        diag = updated_job.diagnostics or {}
        rejected_candidates = diag.get("rejected_candidates", [])
        assert len(rejected_candidates) == 2
        reasons = [c["reason"] for c in rejected_candidates]
        assert any("Định dạng không khớp" in r for r in reasons)
        assert any("Thời gian đăng không thỏa mãn" in r for r in reasons)
