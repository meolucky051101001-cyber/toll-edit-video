import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from backend.core.config import settings
from backend.models.video import Video
from backend.services.cookie_service import (
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
    # Ensure hermetic environment where browser session defaults to empty
    monkeypatch.setattr("backend.services.cookie_service.get_browser_session_cookie", lambda platform: "")

    try:
        # Test GET /api/settings/cookies
        res = client.get("/api/settings/cookies")
        assert res.status_code == 200
        data = res.json()
        assert "xiaohongshu" in data
        assert "douyin" in data

        # Test POST /api/settings/cookies with valid synthetic cookies that do NOT match DUMMY_SUBSTRINGS
        synth_xhs = "a1=synth_valid_a1_token_123; web_session=synth_valid_session_token_456; webId=synth_valid_webid_789"
        synth_dy = "ttwid=1%7Csynth_valid_ttwid_token; s_v_web_id=verify_synth_valid_svwid; sessionid=synth_valid_sessionid_789"
        res_post = client.post(
            "/api/settings/cookies",
            json={"xhs_cookie": synth_xhs, "douyin_cookie": synth_dy},
        )
        assert res_post.status_code == 200
        updated = res_post.json()
        assert updated["xiaohongshu"]["has_cookie"] is True
        assert updated["xiaohongshu"]["is_custom"] is True
        assert updated["douyin"]["has_cookie"] is True
        assert updated["douyin"]["is_custom"] is True

        # Verify settings updated in runtime and saved was called without touching disk .env
        assert settings.xhs_cookie == synth_xhs
        assert settings.douyin_cookie == synth_dy
        assert len(saved_calls) == 1
        assert "XHS_COOKIE" in saved_calls[0]
        assert "DOUYIN_COOKIE" in saved_calls[0]
    finally:
        settings.xhs_cookie = orig_xhs
        settings.douyin_cookie = orig_dy


def test_api_rejects_dummy_cookies_fallback_to_session(client: TestClient, monkeypatch):
    # Save originals
    orig_xhs = settings.xhs_cookie
    orig_dy = settings.douyin_cookie
    monkeypatch.setattr("backend.api.settings._save_env_values", lambda vals: None)

    # 1. When no browser session exists, submitting dummy cookies results in has_cookie=False and is_custom=False
    monkeypatch.setattr("backend.services.cookie_service.get_browser_session_cookie", lambda platform: "")
    try:
        dummy_xhs = "a1=test_val; web_session=sess_val; webId=wid_val"
        dummy_dy = "ttwid=test_ttwid; sessionid=test_sess"
        res = client.post(
            "/api/settings/cookies",
            json={"xhs_cookie": dummy_xhs, "douyin_cookie": dummy_dy},
        )
        assert res.status_code == 200
        data = res.json()
        # Even though settings was patched, is_dummy_cookie prevents dummy cookies from being used as active
        assert data["xiaohongshu"]["has_cookie"] is False
        assert data["xiaohongshu"]["is_custom"] is False
        assert data["douyin"]["has_cookie"] is False
        assert data["douyin"]["is_custom"] is False

        # 2. When authentic browser session exists, dummy input falls back to session
        mock_sessions = {
            "xiaohongshu": "a1=synth_browser_a1; web_session=synth_browser_sess; webId=synth_browser_wid",
            "douyin": "ttwid=1%7Csynth_browser_ttwid; s_v_web_id=verify_browser; sessionid=synth_browser_sess",
        }
        monkeypatch.setattr("backend.services.cookie_service.get_browser_session_cookie", lambda plat: mock_sessions.get(plat, ""))
        res_fallback = client.get("/api/settings/cookies")
        assert res_fallback.status_code == 200
        data_fb = res_fallback.json()
        assert data_fb["xiaohongshu"]["has_cookie"] is True
        assert data_fb["xiaohongshu"]["is_custom"] is False
        assert data_fb["xiaohongshu"]["has_browser_session"] is True
        assert data_fb["douyin"]["has_cookie"] is True
        assert data_fb["douyin"]["is_custom"] is False
        assert data_fb["douyin"]["has_browser_session"] is True
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


def test_extract_cookies_domain_filtering(tmp_path: Path):
    auth_file = tmp_path / "auth_state.json"
    data = {
        "cookies": [
            {"name": "sessionid", "value": "dy_sess_123", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "dy_ttwid_456", "domain": "www.douyin.com"},
            {"name": "", "value": "douyin.com", "domain": "www.douyin.com"},  # empty name
            {"name": "SID", "value": "google_val", "domain": ".google.com"},  # foreign domain
            {"name": "web_session", "value": "xhs_sess_789", "domain": ".xiaohongshu.com"},
        ]
    }
    auth_file.write_text(json.dumps(data), encoding="utf-8")

    # Platform = douyin should only extract Douyin cookies
    dy_extracted = extract_cookies_from_auth_file(auth_file, platform="douyin")
    assert "sessionid=dy_sess_123" in dy_extracted
    assert "ttwid=dy_ttwid_456" in dy_extracted
    assert "google_val" not in dy_extracted
    assert "xhs_sess_789" not in dy_extracted
    assert "=douyin.com" not in dy_extracted

    # Platform = xiaohongshu should only extract XHS cookies
    xhs_extracted = extract_cookies_from_auth_file(auth_file, platform="xiaohongshu")
    assert "web_session=xhs_sess_789" in xhs_extracted
    assert "sessionid" not in xhs_extracted
    assert "google_val" not in xhs_extracted


def test_clean_and_classify_cookie_string():
    from backend.services.cookie_service import clean_and_classify_cookie_string

    dy_raw = "ttwid=1%7Cabc; s_v_web_id=verify_xyz; sessionid=my_session; other=foo"
    res_dy = clean_and_classify_cookie_string(dy_raw)
    assert res_dy["platform"] == "douyin"
    assert res_dy["tokens"]["sessionid"] == "my_session"

    xhs_raw = "a1=token_a1; web_session=token_sess; webId=wid_123"
    res_xhs = clean_and_classify_cookie_string(xhs_raw)
    assert res_xhs["platform"] == "xiaohongshu"
    assert res_xhs["tokens"]["web_session"] == "token_sess"

    unknown_raw = "foo=bar; baz=qux"
    res_unk = clean_and_classify_cookie_string(unknown_raw)
    assert res_unk["platform"] == "unknown"


def test_api_quick_paste_cookies(client: TestClient, monkeypatch):
    orig_xhs = settings.xhs_cookie
    orig_dy = settings.douyin_cookie
    saved = []
    monkeypatch.setattr("backend.api.settings._save_env_values", lambda vals: saved.append(vals))

    try:
        # 1. Quick paste Douyin cookies
        dy_raw = "ttwid=1%7Cpaste_dy; s_v_web_id=verify_paste; sessionid=sess_paste_dy"
        res_dy = client.post("/api/settings/cookies/quick-paste", json={"raw_cookie": dy_raw})
        assert res_dy.status_code == 200
        data_dy = res_dy.json()
        assert data_dy["ok"] is True
        assert data_dy["platform"] == "douyin"
        assert settings.douyin_cookie == dy_raw

        # 2. Quick paste Xiaohongshu cookies
        xhs_raw = "a1=paste_xhs_a1; web_session=paste_xhs_sess; webId=paste_xhs_wid"
        res_xhs = client.post("/api/settings/cookies/quick-paste", json={"raw_cookie": xhs_raw})
        assert res_xhs.status_code == 200
        data_xhs = res_xhs.json()
        assert data_xhs["ok"] is True
        assert data_xhs["platform"] == "xiaohongshu"
        assert settings.xhs_cookie == xhs_raw

        # 3. Invalid / unknown string returns 400
        res_inv = client.post("/api/settings/cookies/quick-paste", json={"raw_cookie": "invalid_cookie_str"})
        assert res_inv.status_code == 400
    finally:
        settings.xhs_cookie = orig_xhs
        settings.douyin_cookie = orig_dy
