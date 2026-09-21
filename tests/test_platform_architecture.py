import pytest
from fastapi.testclient import TestClient

from backend.app.main import app
from backend.providers.capabilities import (
    list_all_capabilities,
)
from backend.providers.registry import get_provider
from backend.providers.tiktok.provider import TikTokSearchProvider
from backend.providers.youtube_shorts.provider import YouTubeShortsSearchProvider
from backend.schemas.contracts import SearchFilters


def test_platform_capabilities_registration():
    all_caps = list_all_capabilities()
    assert len(all_caps) >= 5

    caps_map = {c.id: c for c in all_caps}
    assert "xiaohongshu" in caps_map
    assert "douyin" in caps_map
    assert "tiktok" in caps_map
    assert "youtube_shorts" in caps_map
    assert "mock" in caps_map

    tt = caps_map["tiktok"]
    assert tt.display_name == "TikTok Global"
    assert tt.supports_video_download is False
    assert tt.is_enabled is False
    assert "tiktok.com" in tt.auth_domains

    yt = caps_map["youtube_shorts"]
    assert yt.is_enabled is False
    assert "youtube.com" in yt.auth_domains


def test_platform_registry_resolves_providers():
    p_mock = get_provider("douyin", use_mock=True)
    assert p_mock.__class__.__name__ == "MockProvider"

    p_tt = get_provider("tiktok", use_mock=True)
    assert isinstance(p_tt, TikTokSearchProvider)

    p_yt = get_provider("youtube_shorts", use_mock=True)
    assert isinstance(p_yt, YouTubeShortsSearchProvider)


@pytest.mark.anyio
async def test_tiktok_provider_search():
    provider = TikTokSearchProvider(is_mock=True)
    results = await provider.search("bàn phím cơ", 3, SearchFilters())

    assert len(results) == 3
    for r in results:
        assert r.platform == "tiktok"
        assert len(r.platform_video_id) == 19
        assert "tiktok.com/@" in str(r.url)
        assert "/video/" in str(r.url)
        assert r.duration_seconds is not None
        assert r.like_count is not None
        assert r.is_mock is True


@pytest.mark.anyio
async def test_youtube_shorts_provider_search():
    provider = YouTubeShortsSearchProvider(is_mock=True)
    results = await provider.search("unboxing cute", 3, SearchFilters())

    assert len(results) == 3
    for r in results:
        assert r.platform == "youtube_shorts"
        assert len(r.platform_video_id) == 11
        assert "youtube.com/shorts/" in str(r.url)
        assert r.duration_seconds is not None
        assert r.view_count is not None
        assert r.is_mock is True


def test_platforms_api_endpoints():
    with TestClient(app) as client:
        # 1. List all platforms
        res = client.get("/api/platforms")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] >= 5
        ids = [p["id"] for p in data["platforms"]]
        assert "tiktok" in ids
        assert "youtube_shorts" in ids

        # 2. Get specific platform
        res_tt = client.get("/api/platforms/tiktok")
        assert res_tt.status_code == 200
        assert res_tt.json()["id"] == "tiktok"

        # 3. Non-existent platform
        res_404 = client.get("/api/platforms/non_existent_platform")
        assert res_404.status_code == 404
