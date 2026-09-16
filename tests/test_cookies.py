import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.core.config import settings
from backend.models.video import Video
from backend.services.cookie_service import (
    cookie_dict_to_str,
    extract_cookies_from_auth_file,
    inspect_cookie_tokens,
    mask_cookie_preview,
    parse_cookie_str,
)
from backend.services.download.douyin_adapter import DouyinDownloadAdapter
from backend.services.download.xhs_adapter import XhsDownloadAdapter


def test_parse_cookie_str():
    raw = "a1=18f0a2bc; web_session=040069; webId=998877"
    tokens = parse_cookie_str(raw)
    assert tokens["a1"] == "18f0a2bc"
    assert tokens["web_session"] == "040069"
    assert tokens["webId"] == "998877"
    assert len(tokens) == 3

    assert parse_cookie_str("") == {}
    assert parse_cookie_str("   ") == {}


def test_mask_cookie_preview():
    raw = "a1=18f0a2bc1234567; web_session=040069abc; webId=998877; foo=bar"
    preview = mask_cookie_preview(raw)
    assert "..." in preview
    assert "+1 cookies" in preview


def test_inspect_cookie_tokens():
    xhs_good = "a1=xyz; web_session=12345; webId=abc"
    res = inspect_cookie_tokens(xhs_good, "xiaohongshu")
    assert res["has_cookie"] is True
    assert res["grade"] == "high_quality"
    assert "web_session" in res["detected_essential"]

    xhs_empty = ""
    res_empty = inspect_cookie_tokens(xhs_empty, "xiaohongshu")
    assert res_empty["has_cookie"] is False
    assert res_empty["grade"] == "missing"

    douyin_good = "ttwid=1%7Cabc; s_v_web_id=verify_123; sessionid=sess_999"
    res_dy = inspect_cookie_tokens(douyin_good, "douyin")
    assert res_dy["has_cookie"] is True
    assert res_dy["grade"] == "high_quality"


def test_extract_cookies_from_auth_file(tmp_path: Path):
    auth_file = tmp_path / "auth_state.json"
    data = {
        "cookies": [
            {"name": "a1", "value": "token_a1", "domain": ".xiaohongshu.com"},
            {"name": "web_session", "value": "sess_val", "domain": ".xiaohongshu.com"},
        ]
    }
    auth_file.write_text(json.dumps(data), encoding="utf-8")
    extracted = extract_cookies_from_auth_file(auth_file)
    assert "a1=token_a1" in extracted
    assert "web_session=sess_val" in extracted


def test_api_cookie_settings(client: TestClient, monkeypatch):
    # Save originals
    orig_xhs = settings.xhs_cookie
    orig_dy = settings.douyin_cookie
    saved_calls = []
    monkeypatch.setattr("backend.api.settings._save_env_values", lambda vals: saved_calls.append(vals))

    try:
        # Test GET /api/settings/cookies
        res = client.get("/api/settings/cookies")
        assert res.status_code == 200
        data = res.json()
        assert "xiaohongshu" in data
        assert "douyin" in data

        # Test POST /api/settings/cookies
        test_cookie = "a1=test_val; web_session=sess_val; webId=wid_val"
        res_post = client.post(
            "/api/settings/cookies",
            json={"xhs_cookie": test_cookie, "douyin_cookie": "ttwid=test_ttwid; sessionid=test_sess"},
        )
        assert res_post.status_code == 200
        updated = res_post.json()
        assert updated["xiaohongshu"]["has_cookie"] is True
        assert updated["douyin"]["has_cookie"] is True

        # Verify settings updated in runtime and saved was called without touching disk .env
        assert settings.xhs_cookie == test_cookie
        assert len(saved_calls) == 1
        assert "XHS_COOKIE" in saved_calls[0]
    finally:
        settings.xhs_cookie = orig_xhs
        settings.douyin_cookie = orig_dy


def test_xhs_adapter_uses_cookie(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = Video(
        id="vid-xhs-cookie",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp_xhs_cookie",
        search_query="test",
        search_job_id="job1",
    )

    monkeypatch.setattr(settings, "xhs_cookie", "a1=mock_a1; web_session=mock_sess")

    mock_resp = {
        "message": "获取小红书作品数据成功",
        "params": {"url": video.canonical_url},
        "data": {
            "作品ID": nid,
            "作品类型": "视频",
            "originVideoKey": "ORIGIN_KEY_1080P",
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/low_res.mp4"],
        },
    }

    async def mock_post(self, url, **kwargs):
        # Assert cookie was injected into payload
        assert kwargs["json"]["cookie"] == "a1=mock_a1; web_session=mock_sess"
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    import asyncio

    resolved = asyncio.run(adapter.resolve(video))
    # Assert originVideoKey prioritized for high-resolution uncompressed download
    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/ORIGIN_KEY_1080P"
    assert resolved.headers.get("Cookie") == "a1=mock_a1; web_session=mock_sess"


def test_douyin_adapter_uses_cookie_and_highest_bitrate(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = Video(
        id="vid-dy-cookie",
        platform="douyin",
        platform_video_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        fingerprint="fp_dy_cookie",
        search_query="test",
        search_job_id="job1",
    )

    monkeypatch.setattr(settings, "douyin_cookie", "ttwid=mock_ttwid; sessionid=mock_session")

    mock_resp = {
        "code": 200,
        "data": {
            "type": "video",
            "platform": "douyin",
            "video_id": aweme_id,
            "aweme_detail": {
                "aweme_id": aweme_id,
                "video": {
                    "bit_rate": [
                        {
                            "gear_name": "720p",
                            "bit_rate": 1000000,
                            "play_addr": {"url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=low"]},
                        },
                        {
                            "gear_name": "1080p",
                            "bit_rate": 5000000,
                            "play_addr": {"url_list": ["https://aweme.snssdk.com/aweme/v1/playwm/?video_id=high_1080p"]},
                        },
                    ]
                },
            },
            "video_data": {
                "nwm_video_url_HQ": "https://aweme.snssdk.com/aweme/v1/play/?video_id=default_hq",
            },
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    import asyncio

    resolved = asyncio.run(adapter.resolve(video))
    # Assert highest bitrate was picked and unwatermarked (playwm -> play)
    assert resolved.media_url == "https://aweme.snssdk.com/aweme/v1/play/?video_id=high_1080p"
    assert resolved.headers.get("Cookie") == "ttwid=mock_ttwid; sessionid=mock_session"
