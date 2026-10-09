from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from backend.providers.xiaohongshu.provider import XiaohongshuProvider
from backend.schemas.contracts import SearchFilters


@pytest.mark.anyio
async def test_xhs_needs_detail_dynamic_budget_and_sorting():
    """Verify Xiaohongshu provider inspects subsequent candidates within dynamic detail_budget

    when early candidates fail, reaches quota, and sorts results by requested sort_by.
    """
    browser = MagicMock()
    browser.base_url = "https://www.rednote.com"
    browser.ensure_headless_session = AsyncMock()
    browser.release_blocked_page = AsyncMock()
    page = AsyncMock()
    direct_search_url = (
        f"https://www.rednote.com/search_result?keyword={quote('decor bàn học')}&source=web_explore_feed&type=51"
    )
    page.url = direct_search_url
    page.wait_for_selector = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.wheel = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()

    # 4 DOM candidates without title (forcing needs_detail)
    dom_candidates = [
        {
            "href": "https://www.rednote.com/explore/650000000000000000000001",
            "note_id": "650000000000000000000001",
            "cover": "https://img/1.jpg",
            "is_video": True,
            "token": "tok1",
            "title": None,
        },
        {
            "href": "https://www.rednote.com/explore/650000000000000000000002",
            "note_id": "650000000000000000000002",
            "cover": "https://img/2.jpg",
            "is_video": True,
            "token": "tok2",
            "title": None,
        },
        {
            "href": "https://www.rednote.com/explore/650000000000000000000003",
            "note_id": "650000000000000000000003",
            "cover": "https://img/3.jpg",
            "is_video": True,
            "token": "tok3",
            "title": None,
        },
        {
            "href": "https://www.rednote.com/explore/650000000000000000000004",
            "note_id": "650000000000000000000004",
            "cover": "https://img/4.jpg",
            "is_video": True,
            "token": "tok4",
            "title": None,
        },
    ]

    loc_obj = MagicMock()
    loc_obj.evaluate_all = AsyncMock(return_value=dom_candidates)
    loc_obj.count = AsyncMock(return_value=0)
    loc_obj.first = MagicMock()
    loc_obj.first.count = AsyncMock(return_value=0)
    loc_obj.first.is_visible = AsyncMock(return_value=False)
    loc_obj.filter = MagicMock(return_value=loc_obj)

    page.locator = MagicMock(return_value=loc_obj)
    page.is_closed = MagicMock(return_value=False)

    def mock_goto(url, *args, **kwargs):
        page.url = url

    page.goto = AsyncMock(side_effect=mock_goto)

    detail_page = AsyncMock()
    detail_page.wait_for_selector = AsyncMock()
    detail_page.locator = MagicMock(return_value=loc_obj)
    detail_page.close = AsyncMock()

    payload_c2 = {
        "has_video": True,
        "title": "Decor bàn học đẹp số 2",
        "like_text": "500",
        "poster": "https://img/c2.jpg",
        "author_name": "Author 2",
    }
    payload_c3 = {
        "has_video": True,
        "title": "Decor bàn học đẹp số 3",
        "like_text": "600",
        "poster": "https://img/c3.jpg",
        "author_name": "Author 3",
    }

    eval_calls: list[int] = []

    async def mock_detail_evaluate(*args, **kwargs):
        call_idx = len(eval_calls)
        eval_calls.append(call_idx)
        if call_idx == 0:
            return {}  # candidate 1 fails/empty
        elif call_idx == 1:
            return payload_c2  # candidate 2 succeeds
        else:
            return payload_c3  # candidate 3 succeeds

    detail_page.evaluate = AsyncMock(side_effect=mock_detail_evaluate)

    async def mock_detail_goto(link, *args, **kwargs):
        detail_page.url = link

    detail_page.goto = AsyncMock(side_effect=mock_detail_goto)

    context = AsyncMock()
    context.new_page = AsyncMock(return_value=detail_page)
    browser.context = context
    browser.ensure_open = AsyncMock(return_value=page)
    browser.inspect = AsyncMock(return_value="authenticated")
    browser.page = page

    provider = XiaohongshuProvider(browser=browser)
    setattr(provider, "gate", AsyncMock())

    filters = SearchFilters(sort_by="most_liked", publish_time="all", content_type="all")
    results = await provider.search("decor bàn học", limit=2, filters=filters)

    assert len(results) == 2
    # Verify sorting by most_liked: 600 > 500
    assert results[0].platform_video_id == "650000000000000000000003"
    assert results[0].like_count == 600
    assert results[1].platform_video_id == "650000000000000000000002"
    assert results[1].like_count == 500
    # Verified that detail page evaluation inspected beyond the failing candidate 1
    assert len(eval_calls) == 3
