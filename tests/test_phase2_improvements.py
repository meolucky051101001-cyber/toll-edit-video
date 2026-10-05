import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

from backend.providers.douyin.parser import (
    extract_douyin_ssr_json,
    parse_search_response_json,
)
from backend.services.session_keeper import SessionKeepAliveService


def test_extract_douyin_ssr_json_from_render_data():
    sample_item = {
        "aweme_info": {
            "aweme_id": "7488991122334455667",
            "desc": "SSR Extracted Video #手帐",
            "author": {"nickname": "SSR User", "sec_uid": "MS4wLjABAAAA_ssr"},
            "statistics": {"digg_count": 54321, "comment_count": 123},
            "video": {"duration": 45000, "cover": {"url_list": ["https://cdn.example.com/c.jpg"]}},
        }
    }
    raw_data = {"search_data": [sample_item]}
    encoded_json = quote(json.dumps(raw_data))

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <script id="RENDER_DATA" type="application/json">{encoded_json}</script>
    </head>
    <body><div>Some HTML</div></body>
    </html>
    """

    extracted = extract_douyin_ssr_json(html)
    assert extracted is not None
    assert "data" in extracted
    assert len(extracted["data"]) == 1

    parsed = parse_search_response_json(extracted, limit=5)
    assert len(parsed) == 1
    assert parsed[0].platform_video_id == "7488991122334455667"
    assert parsed[0].author_name == "SSR User"
    assert parsed[0].like_count == 54321


def test_extract_douyin_ssr_json_from_router_data():
    sample_item = {
        "aweme_id": "7488991122334455888",
        "desc": "Router Data Note",
        "author": {"nickname": "Router User"},
        "statistics": {"digg_count": 8888},
        "images": [{"url_list": ["https://cdn.example.com/img1.jpg"]}],
    }
    html = f"""
    <script>
    window._ROUTER_DATA = {{"cards": [{json.dumps(sample_item)}]}};
    window.someOtherVar = true;
    </script>
    """

    extracted = extract_douyin_ssr_json(html)
    assert extracted is not None
    assert "data" in extracted
    parsed = parse_search_response_json(extracted, limit=5)
    assert len(parsed) == 1
    assert parsed[0].platform_video_id == "7488991122334455888"


def test_extract_douyin_ssr_json_none_on_invalid():
    assert extract_douyin_ssr_json("") is None
    assert extract_douyin_ssr_json("<html><body>No scripts</body></html>") is None


def test_session_keep_alive_service(tmp_path):
    douyin_dir = tmp_path / "douyin"
    douyin_dir.mkdir()
    auth_file = douyin_dir / "auth_state.json"
    auth_file.write_text(
        json.dumps({
            "cookies": [
                {"name": "sessionid", "value": "test_session_123", "domain": ".douyin.com"},
                {"name": "passport_csrf_token", "value": "csrf_abc", "domain": ".douyin.com"},
            ]
        }),
        encoding="utf-8",
    )

    service = SessionKeepAliveService(douyin_profile=douyin_dir, xhs_profile=tmp_path / "xhs")
    status = service.get_status()
    assert status["douyin"]["saved_cookies"] == 2
    assert status["douyin"]["has_saved_session_tokens"] is True
    assert status["douyin"]["has_auth_session"] is False
    assert status["douyin"]["status"] == "saved_unverified"
    assert status["xiaohongshu"]["has_auth_session"] is False

    # Test ping with mocked browser
    async def scenario():
        mock_browser = MagicMock()
        mock_browser.lock = asyncio.Lock()
        mock_page = MagicMock()
        mock_page.url = "https://www.douyin.com/search/test"
        mock_page.is_closed = MagicMock(return_value=False)
        mock_page.evaluate = AsyncMock(return_value={"ok": True, "status": 200})
        mock_browser.page = mock_page
        mock_browser.get_active_page.return_value = mock_page
        mock_browser.inspect = AsyncMock(return_value="authenticated")
        mock_browser.context = None

        ok = await service.ping_platform("douyin", mock_browser)
        assert ok is True
        assert service.last_heartbeat["douyin"] > 0
        mock_page.evaluate.assert_awaited_once()

    asyncio.run(scenario())
