import asyncio

import httpx
import pytest

from backend.models.video import Video
from backend.services.download.base import DownloadAdapterError
from backend.services.download.douyin_adapter import DouyinDownloadAdapter
from backend.services.download.xhs_adapter import XhsDownloadAdapter


def test_xhs_adapter_resolve_success(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    token_url = f"https://www.rednote.com/discovery/item/{nid}?xsec_token=VALID_TOKEN"
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        share_url=token_url,
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "message": "获取小红书作品数据成功",
        "params": {"url": token_url},
        "data": {
            "作品ID": nid,
            "作品类型": "视频",
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/123/video.mp4"],
            "作者昵称": "TestAuthor",
        },
    }

    async def mock_post(self, url, **kwargs):
        assert kwargs["json"]["url"] == token_url
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.platform == "xiaohongshu"
    assert resolved.video_id == nid
    assert resolved.media_url == "https://sns-video-bd.xhscdn.com/stream/123/video.mp4"
    assert resolved.headers.get("Referer") == "https://www.xiaohongshu.com/"


def test_xhs_adapter_rejects_wrong_note_id(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    wrong_nid = "66d3cb2b00000000200399f9"
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "data": {
            "作品ID": wrong_nid,
            "作品类型": "视频",
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/123/video.mp4"],
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "mismatched_video_id"


def test_xhs_adapter_rejects_image_post(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "data": {
            "作品ID": nid,
            "作品类型": "图文",
            "下载地址": ["https://sns-img-bd.xhscdn.com/1.jpg"],
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "not_a_video"


def test_xhs_adapter_handles_offline_service(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id="66d3cb2b00000000200399e3",
        url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
        canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    async def mock_post(self, url, **kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "service_unavailable"


def test_douyin_adapter_resolve_success(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "type": "video",
            "platform": "douyin",
            "video_id": aweme_id,
            "video_data": {
                "nwm_video_url_HQ": "https://aweme.snssdk.com/aweme/v1/play/?video_id=123&ratio=1080p",
                "wm_video_url": "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=123",
            },
        },
    }

    async def mock_get(self, url, **kwargs):
        # Assert exact upstream route and query parameters
        assert str(url) == "http://127.0.0.1:5555/api/hybrid/video_data"
        assert kwargs["params"]["minimal"] == "true"
        assert kwargs["params"]["url"] == f"https://www.douyin.com/video/{aweme_id}"
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.platform == "douyin"
    assert resolved.video_id == aweme_id
    assert "https://aweme.snssdk.com/aweme/v1/play/" in resolved.media_url
    assert resolved.headers.get("Referer") == "https://www.douyin.com/"


def test_douyin_adapter_rejects_wrong_video_id(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "type": "video",
            "platform": "douyin",
            "video_id": "7123456789012345999",  # mismatched
            "video_data": {"nwm_video_url": "https://example.com/video.mp4"},
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "mismatched_video_id"


def test_douyin_adapter_rejects_image_post(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "type": "image",
            "platform": "douyin",
            "video_id": aweme_id,
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "not_a_video"


def test_douyin_adapter_rejects_missing_id(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id="7123456789012345678",
        url="https://www.douyin.com/video/7123456789012345678",
        canonical_url="https://www.douyin.com/video/7123456789012345678",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "type": "video",
            "platform": "douyin",
            # missing video_id
            "video_data": {"nwm_video_url": "https://example.com/video.mp4"},
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "invalid_response"


def test_douyin_adapter_rejects_missing_type(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id="7123456789012345678",
        url="https://www.douyin.com/video/7123456789012345678",
        canonical_url="https://www.douyin.com/video/7123456789012345678",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "video_id": "7123456789012345678",
            # missing type
            "video_data": {"nwm_video_url": "https://example.com/video.mp4"},
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "invalid_response"


def test_douyin_adapter_rejects_nested_aweme_detail_mismatch(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = Video(
        id="vid-2",
        platform="douyin",
        platform_video_id=aweme_id,
        url=f"https://www.douyin.com/video/{aweme_id}",
        canonical_url=f"https://www.douyin.com/video/{aweme_id}",
        fingerprint="fp2",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "code": 200,
        "data": {
            "type": "video",
            "video_id": aweme_id,
            "aweme_detail": {"aweme_id": "9999999999999999999"},  # nested mismatch
            "video_data": {"nwm_video_url": "https://example.com/video.mp4"},
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "mismatched_video_id"


def test_xhs_adapter_rejects_missing_id(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id="66d3cb2b00000000200399e3",
        url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
        canonical_url="https://www.xiaohongshu.com/explore/66d3cb2b00000000200399e3",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "data": {
            # missing 作品ID
            "作品类型": "视频",
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/123/video.mp4"],
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "invalid_response"


def test_xhs_adapter_rejects_missing_type(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = Video(
        id="vid-1",
        platform="xiaohongshu",
        platform_video_id=nid,
        url=f"https://www.xiaohongshu.com/explore/{nid}",
        canonical_url=f"https://www.xiaohongshu.com/explore/{nid}",
        fingerprint="fp1",
        search_query="test",
        search_job_id="job1",
    )

    mock_resp = {
        "data": {
            "作品ID": nid,
            # missing 作品类型
            "下载地址": ["https://sns-video-bd.xhscdn.com/stream/123/video.mp4"],
        }
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    with pytest.raises(DownloadAdapterError) as exc_info:
        asyncio.run(adapter.resolve(video))
    assert exc_info.value.code == "invalid_response"

