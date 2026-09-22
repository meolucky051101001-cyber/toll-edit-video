from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.providers.douyin.parser import (
    apply_search_filters_and_sorting,
    parse_aweme_item,
)
from backend.providers.douyin.provider import DouyinProvider
from backend.schemas.contracts import SearchFilters, VideoResult


def _generate_mock_aweme_item(idx: int, aweme_type: int = 4, likes: int = 100, hours_ago: int = 1) -> dict:
    pub_ts = int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp())
    return {
        "aweme_info": {
            "aweme_id": f"71000000000000000{idx:03d}",
            "desc": f"Douyin test item {idx} #decor #home",
            "create_time": pub_ts,
            "aweme_type": aweme_type,
            "statistics": {
                "digg_count": likes,
                "comment_count": 10,
                "share_count": 5,
                "collect_count": 20,
                "play_count": 1000,
            },
            "author": {
                "nickname": f"Creator {idx}",
                "sec_uid": f"sec_user_{idx}",
            },
            "video": {
                "duration": 15000,
                "cover": {"url_list": [f"https://p3.douyinpic.com/cover_{idx}.jpg"]},
            },
            "images": [
                {"url_list": [f"https://p3.douyinpic.com/img_{idx}_1.jpg"]},
                {"url_list": [f"https://p3.douyinpic.com/img_{idx}_2.jpg"]},
            ] if aweme_type in (2, 68, 150) else None,
        }
    }


def test_aweme_type_2_recognition_as_note():
    """Confirms aweme_type=2 is properly recognized as an image note."""
    item = _generate_mock_aweme_item(1, aweme_type=2, likes=500)
    vr = parse_aweme_item(item)
    assert vr is not None
    assert vr.media_type == "image_text"
    assert "/note/" in str(vr.url)
    assert vr.like_count == 500
    assert len(vr.images) == 2


def test_aweme_type_2_note_fallback_cover():
    """Confirms aweme_type=2 without images array falls back to cover_obj while preserving image_text media_type."""
    raw = _generate_mock_aweme_item(2, aweme_type=2, likes=300)
    raw["aweme_info"]["images"] = None
    vr = parse_aweme_item(raw)
    assert vr is not None
    assert vr.media_type == "image_text"
    assert "/note/" in str(vr.url)
    assert vr.thumbnail_url == "https://p3.douyinpic.com/cover_2.jpg"


@pytest.mark.parametrize("limit", [20, 50, 200])
def test_apply_search_filters_and_sorting_limits(limit: int):
    """Verifies that apply_search_filters_and_sorting correctly returns requested limit without truncation."""
    items = [
        VideoResult(
            platform="douyin",
            platform_video_id=f"7100000000000000{i:04d}",
            url=f"https://www.douyin.com/video/7100000000000000{i:04d}",  # type: ignore[arg-type]
            title=f"Sample video {i}",
            like_count=i * 10,
            media_type="video",
            published_at=datetime.now(timezone.utc) - timedelta(hours=i),
        )
        for i in range(250)
    ]
    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")
    filtered = apply_search_filters_and_sorting(items, filters=filters, limit=limit)
    assert len(filtered) == limit
    # Verify sorted descending by likes
    for i in range(len(filtered) - 1):
        assert (filtered[i].like_count or 0) >= (filtered[i + 1].like_count or 0)


@pytest.mark.parametrize("limit", [20, 50, 200])
@pytest.mark.anyio
async def test_douyin_provider_search_does_not_truncate_at_5(limit: int):
    """P0 regression test: provider must not exit early at 5 results when limit=20, 50, or 200."""
    browser = MagicMock()
    browser.lock = AsyncMock()
    browser.ensure_headless_session = AsyncMock()
    browser.release_blocked_page = AsyncMock()
    browser.inspect = AsyncMock(return_value="page_available")

    page = AsyncMock()
    page.url = ""
    page.wait_for_selector = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.content = AsyncMock(return_value="<html></html>")
    page.locator = MagicMock()
    page.locator.return_value.evaluate_all = AsyncMock(return_value=[])
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.is_closed = MagicMock(return_value=False)
    page.remove_listener = MagicMock()
    browser.ensure_open = AsyncMock(return_value=page)
    browser.page = page

    provider = DouyinProvider(browser=browser)

    # Simulate intercepted network API responses containing 220 items
    intercepted_payload = {
        "data": [_generate_mock_aweme_item(i, aweme_type=4, likes=i * 10) for i in range(1, 230)]
    }

    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")

    response_mock = MagicMock()
    response_mock.url = "https://www.douyin.com/aweme/v1/web/search/item/"
    response_mock.status = 200
    response_mock.json = AsyncMock(return_value=intercepted_payload)

    handlers = []

    def on_listener(event, handler):
        if event == "response":
            handlers.append(handler)

    page.on = MagicMock(side_effect=on_listener)

    async def mock_goto(url, *args, **kwargs):
        page.url = url
        for h in handlers:
            await h(response_mock)

    page.goto = AsyncMock(side_effect=mock_goto)

    results = await provider.search("decor bàn học", limit=limit, filters=filters)

    assert len(results) == limit, f"Expected {limit} results, but got {len(results)} (check for premature min(limit, 5) exit!)"
    assert results[0].platform == "douyin"
