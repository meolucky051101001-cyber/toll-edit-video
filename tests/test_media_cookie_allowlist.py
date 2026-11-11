import asyncio

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.core.config import settings
from backend.core.database import Base
from backend.models import Video
from backend.models.download_job import DownloadJob
from backend.schemas.downloads import ResolvedMedia
from backend.services.download.douyin_adapter import DouyinDownloadAdapter
from backend.services.download.media_hosts import cookie_allowed
from backend.services.download.service import DownloadService
from backend.services.download.xhs_adapter import XhsDownloadAdapter

ALLOWED_CASES = [
    ("douyin", "https://aweme.snssdk.com/aweme/v1/play/?video_id=x"),
    ("douyin", "https://v3-web.douyinvod.com/video.mp4"),
    ("douyin", "https://www.douyin.com/video/123"),
    ("douyin", "https://v26-web.douyinvod.com/x.mp4"),
    ("douyin", "https://p3-sign.bytecdn.cn/x.mp4"),
    ("douyin", "https://aweme.amemv.com/x.mp4"),
    ("douyin", "https://v1.zjcdn.com/x.mp4"),
    ("xiaohongshu", "https://sns-video-bd.xhscdn.com/ORIGIN_KEY"),
    ("xiaohongshu", "https://sns-img-bd.xhscdn.com/img.jpg"),
    ("xiaohongshu", "https://www.xiaohongshu.com/explore/abc"),
    ("xiaohongshu", "https://www.rednote.com/explore/abc"),
    ("xiaohongshu", "https://xhscdn.com/x.mp4"),
]

REJECTED_CASES = [
    ("douyin", "https://evil.example/steal.mp4"),
    ("douyin", "https://xhscdn.com.evil.example/steal.mp4"),
    ("douyin", "https://www.douyin.com.fake.site/steal.mp4"),
    ("douyin", "https://douyin.com.evil.invalid/steal.mp4"),
    ("douyin", "https://aweme.snssdk.com@evil.example/steal.mp4"),
    ("douyin", "https://evil.example/aweme.snssdk.com/steal.mp4"),
    ("douyin", "http://127.0.0.1/steal.mp4"),
    ("douyin", "http://169.254.169.254/latest/meta-data"),
    ("douyin", "ftp://aweme.snssdk.com/x.mp4"),
    ("douyin", "not a url"),
    ("douyin", ""),
    ("xiaohongshu", "https://evil.example/steal.mp4"),
    ("xiaohongshu", "https://xiaohongshu.com.phishing.org/steal.mp4"),
    ("xiaohongshu", "https://rednote.com.fake.site/steal.mp4"),
    ("xiaohongshu", "https://sns-video-bd.xhscdn.com@evil.example/steal.mp4"),
    ("tiktok", "https://www.tiktok.com/video/123"),
]


@pytest.mark.parametrize(("platform", "url"), ALLOWED_CASES)
def test_cookie_allowed_accepts_platform_cdns(platform, url):
    assert cookie_allowed(platform, url) is True


@pytest.mark.parametrize(("platform", "url"), REJECTED_CASES)
def test_cookie_allowed_rejects_foreign_hosts(platform, url):
    assert cookie_allowed(platform, url) is False


def _video(platform: str, vid: str, pid: str) -> Video:
    return Video(
        id=vid,
        platform=platform,
        platform_video_id=pid,
        url=f"https://www.{platform}.com/video/{pid}",
        canonical_url=f"https://www.{platform}.com/video/{pid}",
        fingerprint=f"fp_{vid}",
        search_query="test",
        search_job_id="job1",
    )


def test_douyin_adapter_withholds_cookie_for_foreign_media_url(monkeypatch):
    adapter = DouyinDownloadAdapter("http://127.0.0.1:5555")
    aweme_id = "7123456789012345678"
    video = _video("douyin", "vid-dy-evil", aweme_id)
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
                            "gear_name": "1080p",
                            "bit_rate": 5000000,
                            "play_addr": {"url_list": ["https://evil.example/steal.mp4"]},
                        }
                    ]
                },
            },
        },
    }

    async def mock_get(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.media_url == "https://evil.example/steal.mp4"
    assert "Cookie" not in resolved.headers


def test_xhs_adapter_withholds_cookie_for_foreign_media_url(monkeypatch):
    adapter = XhsDownloadAdapter("http://127.0.0.1:5556")
    nid = "66d3cb2b00000000200399e3"
    video = _video("xiaohongshu", "vid-xhs-evil", nid)
    monkeypatch.setattr(settings, "xhs_cookie", "a1=mock_a1; web_session=mock_sess")

    mock_resp = {
        "message": "获取小红书作品数据成功",
        "params": {"url": video.canonical_url},
        "data": {
            "作品ID": nid,
            "作品类型": "视频",
            "下载地址": ["https://evil.example/steal.mp4"],
        },
    }

    async def mock_post(self, url, **kwargs):
        return httpx.Response(200, json=mock_resp)

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    resolved = asyncio.run(adapter.resolve(video))
    assert resolved.media_url == "https://evil.example/steal.mp4"
    assert "Cookie" not in resolved.headers


@pytest.mark.anyio
async def test_download_stream_strips_credentials_for_foreign_initial_url(monkeypatch, tmp_path):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir()
    service = DownloadService(sessions, download_dir=dl_dir)
    service.temp_dir = dl_dir / ".temp"
    service.temp_dir.mkdir(exist_ok=True)

    with sessions() as db:
        db.add(_video("douyin", "vid-strip-1", "7352345678901234567"))
        db.add(DownloadJob(id="job-strip-1", video_id="vid-strip-1", platform="douyin", status="downloading"))
        db.commit()

    captured_requests = []

    class MockStreamClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        def build_request(self, method, url, headers=None):
            req = httpx.Request(method, url, headers=headers)
            captured_requests.append((str(url), dict(headers or {})))
            return req

        async def send(self, req, stream=False):
            async def mock_aiter(chunk_size=65536):
                yield b"fake_mp4_bytes_for_testing"

            resp = httpx.Response(
                200,
                headers={"Content-Length": "26", "Content-Type": "video/mp4"},
                request=req,
            )
            resp.aiter_bytes = mock_aiter
            return resp

    monkeypatch.setattr(
        "backend.services.download.service.create_safe_async_client", lambda **kw: MockStreamClient()
    )
    monkeypatch.setattr(
        "backend.services.download.service.is_safe_media_url_async", lambda u: asyncio.sleep(0, result=True)
    )

    resolved = ResolvedMedia(
        platform="douyin",
        video_id="7352345678901234567",
        media_url="https://evil.example/steal.mp4",
        headers={
            "User-Agent": "CustomUA",
            "Cookie": "secret_cookie=123",
            "Authorization": "Bearer secret_token",
            "Referer": "https://www.douyin.com/",
        },
        format="mp4",
        adapter_name="DouyinAdapter",
        adapter_version="1.0",
    )

    with sessions() as db:
        video = db.get(Video, "vid-strip-1")
        assert video is not None

    await service._download_stream("job-strip-1", resolved, video)

    assert len(captured_requests) >= 1
    for url, headers in captured_requests:
        assert "evil.example" in url
        lower = {k.lower(): v for k, v in headers.items()}
        assert "cookie" not in lower
        assert "authorization" not in lower